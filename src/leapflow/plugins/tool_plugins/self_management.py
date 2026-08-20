"""Self-Management plugin — lets the Agent introspect and manage its own plugin composition.

This is the Phase 2.4 Self-Modification MVP. It exposes twelve tools:

Read-only governance (no approval needed):
    - plugin_list     : list all registered plugins across Tool/Gateway/LLM subsystems
    - plugin_status   : detailed info about one plugin (tools, deps, fiber state, generation)
    - plugin_versions : inspect recorded profile plugin versions and the active pointer
    - plugin_propose  : create a side-effect-free proposal from capability-gap evidence
    - assess_compatibility : assess foreign plugin manifest compatibility with LeapFlow

Generation (no approval needed — produces validated code without installing):
    - plugin_generate : describe a capability need; the LLM produces conformant
                        plugin code and it is rigorously validated. The validated
                        code is returned; installation is a separate, gated step.

State-mutating (REQUIRES approval — routed through the plugin_approval_gate):
    - plugin_install  : write validated code (from plugin_generate) or a
                        marketplace payload into the profile-scoped plugins
                        directory and load it dynamically. This mutates the
                        filesystem and the live registry.
    - plugin_rollback : restore a recorded source snapshot and hot-reload it
    - plugin_reload   : hot-reload a plugin
    - plugin_disable  : dispose a plugin's fiber (removes its tools)
    - plugin_remove   : terminally remove a plugin and optionally delete source
    - plugin_enable   : re-enable a previously disabled plugin

Concurrency note: plugin_install, plugin_disable, plugin_reload, and plugin_enable
operate at the process-global registry level; changes affect all sessions in this
daemon, not just the current conversation. In-flight turns keep using their
per-turn handler snapshot so they finish safely; only NEW turns started after the
change see the new plugin set.

Approval note: In non-daemon (in-process CLI) mode, no plugin_approval_gate is
installed, so mutation tools will always fail-closed. Self-modification is
available only in daemon mode where the ApprovalCoordinator wires the gate.

LLM co-evolution note: plugin_generate depends on an optional llm_provider that
is wired via bind_runtime. If unavailable (e.g. no LLM credentials configured or
the container has not propagated one yet), the tool reports the missing
dependency instead of pretending to have generated code.

Design principle: this is the Agent's window into its own architecture. It must
be transparent (introspection is free) but safe (mutation requires explicit
approval, and self-modification is classified HIGH risk with no permanent grants).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from leapflow.plugins.protocol import ToolMetadata

logger = logging.getLogger(__name__)


class SelfManagementPlugin:
    """ToolPlugin exposing the Agent's own plugin management surface."""

    def __init__(self) -> None:
        self._plugin_approval_gate: Any = None
        # Optional: an LLM provider (leapflow.llm.LLMProvider-like) used by
        # plugin_generate. Wired opportunistically via bind_runtime — the tool
        # degrades gracefully when it is absent so introspection and mutation
        # paths never break because generation is offline.
        self._llm_provider: Any = None
        # Opt-in switch for LLM-driven plugin generation. Wired from
        # Settings.plugin_generation_enabled by the daemon; defaults to False
        # so an unattended profile cannot spend tokens synthesizing plugins.
        self._plugin_generation_enabled: bool = False
        # Profile-scoped directory where plugin_install writes plugin code and
        # loads it dynamically. Injected via bind_runtime by the daemon
        # approval coordinator (derived from ProfileLayout). None -> resolved
        # lazily from the active profile layout so in-process CLI mode still
        # installs into a profile-scoped path rather than the package dir.
        self._plugin_install_dir: Optional[str] = None
        # Optional MarketplaceClient used by the marketplace_name install branch.
        # None when no marketplace is configured; the branch then returns a
        # structured error.
        self._marketplace_client: Any = None
        # Hex-encoded Ed25519 public keys trusted to sign marketplace plugins.
        # When non-empty, marketplace installs require a valid signature.
        self._trusted_pubkeys: set[str] = set()
        # Optional persistent store for PluginProposal review queue. When not
        # injected, it is resolved lazily from ProfileLayout.plugin_proposals_path.
        self._plugin_proposal_store: Any = None
        # Optional version store; lazily resolved from ProfileLayout.plugin_versions_dir.
        self._plugin_version_store: Any = None

    @property
    def plugin_id(self) -> str:
        return "self_management"

    @property
    def category(self) -> str:
        return "system"

    @property
    def dependencies(self) -> list[str]:
        return [
            "plugin_approval_gate",
            "llm_provider",
            "plugin_generation_enabled",
            "plugin_install_dir",
            "marketplace_client",
            "marketplace_trusted_pubkeys",
            "plugin_proposal_store",
            "plugin_version_store",
        ]

    def bind_runtime(self, **deps: Any) -> None:
        if "plugin_approval_gate" in deps:
            self._plugin_approval_gate = deps["plugin_approval_gate"]
        if "llm_provider" in deps:
            self._llm_provider = deps["llm_provider"]
        if "plugin_generation_enabled" in deps:
            self._plugin_generation_enabled = bool(deps["plugin_generation_enabled"])
        if "plugin_install_dir" in deps:
            value = deps["plugin_install_dir"]
            self._plugin_install_dir = str(value) if value else None
        if "marketplace_client" in deps:
            self._marketplace_client = deps["marketplace_client"]
        if "marketplace_trusted_pubkeys" in deps:
            raw = deps["marketplace_trusted_pubkeys"] or ()
            self._trusted_pubkeys = {str(k).strip() for k in raw if str(k).strip()}
        if "plugin_proposal_store" in deps:
            self._plugin_proposal_store = deps["plugin_proposal_store"]
        if "plugin_version_store" in deps:
            self._plugin_version_store = deps["plugin_version_store"]

    # ── Read-only introspection ────────────────────────────

    async def _plugin_list_handler(self, **kwargs: Any) -> Dict[str, Any]:
        """List all registered plugins across Tool/Gateway/LLM subsystems."""
        from leapflow.plugins import get_registry, get_scoped_registry

        try:
            reg = get_registry()
            scoped = get_scoped_registry()

            plugins_info: list[dict[str, Any]] = []
            for plugin_id, plugin in reg.plugins.items():
                fiber = scoped.get_fiber(plugin_id)
                plugins_info.append({
                    "plugin_id": plugin_id,
                    "category": plugin.category,
                    "tool_count": len(plugin.tools),
                    "state": fiber.state.value if fiber else "unmanaged",
                    "generation": fiber.generation if fiber else None,
                })

            # Cross-subsystem: Gateway adapters
            gateway_adapters: list[dict[str, Any]] = []
            try:
                from leapflow.gateway.adapters import BUILTIN_PLUGINS
                for bp in BUILTIN_PLUGINS:
                    gateway_adapters.append({
                        "platform_id": bp.platform_id,
                        "display_name": bp.display_name,
                        "subsystem": "gateway",
                    })
            except (ImportError, AttributeError):
                pass

            # Cross-subsystem: LLM providers
            llm_providers: list[dict[str, Any]] = []
            try:
                from leapflow.llm.provider_registry import (
                    get_default_registry as get_llm_registry,
                )
                llm_reg = get_llm_registry()
                for plugin_meta in llm_reg.list_plugins():
                    llm_providers.append({
                        "provider_id": plugin_meta.get("provider_id", "unknown"),
                        "display_name": plugin_meta.get("display_name", ""),
                        "subsystem": "llm",
                    })
            except (ImportError, AttributeError, RuntimeError):
                pass

            return {
                "ok": True,
                "subsystem": "tools",
                "plugin_count": len(plugins_info),
                "plugins": plugins_info,
                "categories": sorted(reg.categories),
                # Cross-subsystem introspection (additive)
                "gateway_adapters": gateway_adapters,
                "llm_providers": llm_providers,
                "total_count": len(plugins_info) + len(gateway_adapters) + len(llm_providers),
                "capability_report": self._build_capability_report(
                    reg,
                    scoped,
                    plugins_info,
                    gateway_adapters,
                    llm_providers,
                ),
            }
        except (RuntimeError, AttributeError) as exc:
            logger.warning("plugin_list failed: %s", exc, exc_info=True)
            return {"ok": False, "error": f"plugin_list failed: {exc}"}

    def _build_capability_report(
        self,
        reg: Any,
        scoped: Any,
        plugins_info: list[dict[str, Any]],
        gateway_adapters: list[dict[str, Any]],
        llm_providers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build a live, evidence-backed capability report for self-questions."""
        tool_categories: dict[str, dict[str, Any]] = {}
        self_management_tools: list[str] = []
        mutation_tools: list[str] = []
        approval_required_tools: list[str] = []
        read_only_tools: list[str] = []

        for tool in reg.all_metadata:
            metadata = dict(tool.x_leapflow or {})
            category = str(metadata.get("category") or "general")
            bucket = tool_categories.setdefault(
                category,
                {
                    "tool_count": 0,
                    "tools": [],
                    "approval_required_count": 0,
                    "mutating_count": 0,
                },
            )
            bucket["tool_count"] += 1
            bucket["tools"].append(tool.name)
            if bool(tool.mutates_state):
                mutation_tools.append(tool.name)
                bucket["mutating_count"] += 1
            else:
                read_only_tools.append(tool.name)
            if metadata.get("requires_approval") is True:
                approval_required_tools.append(tool.name)
                bucket["approval_required_count"] += 1
            if tool.name.startswith("plugin_") or tool.name == "assess_compatibility":
                self_management_tools.append(tool.name)

        for bucket in tool_categories.values():
            bucket["tools"] = sorted(bucket["tools"])

        profile_layout = self._profile_layout_or_none()
        install_dir = self._safe_install_dir()
        dependency_state = {
            "approval_gate_bound": self._plugin_approval_gate is not None,
            "llm_provider_bound": self._llm_provider is not None,
            "plugin_generation_enabled": self._plugin_generation_enabled,
            "plugin_install_dir": install_dir,
            "marketplace_configured": self._marketplace_client is not None,
            "trusted_marketplace_pubkeys": len(self._trusted_pubkeys),
            "proposal_store_available": (
                self._plugin_proposal_store is not None or profile_layout is not None
            ),
            "version_store_available": (
                self._plugin_version_store is not None or profile_layout is not None
            ),
        }
        limitations = self._capability_limitations(dependency_state)

        return {
            "source": "live_runtime_registry",
            "registry": {
                "version": reg.version,
                "plugin_count": len(plugins_info),
                "tool_count": len(reg.tool_handlers),
                "fiber_count": len(scoped.fibers),
                "categories": sorted(tool_categories),
            },
            "plugins_supported": {
                "supported": "self_management" in reg.plugins,
                "evidence_tools": sorted(self_management_tools),
                "profile_installs": bool(install_dir),
                "hot_reload": "plugin_reload" in self_management_tools,
                "versioning": "plugin_versions" in self_management_tools
                and "plugin_rollback" in self_management_tools,
                "compatibility_assessment": "assess_compatibility" in self_management_tools,
            },
            "self_evolution": {
                "proposal_flow": "plugin_propose" in self_management_tools,
                "generation_tool": "plugin_generate" in self_management_tools,
                "generation_ready": self._plugin_generation_enabled and self._llm_provider is not None,
                "install_tool": "plugin_install" in self_management_tools,
                "rollback_tool": "plugin_rollback" in self_management_tools,
                "behavior_test_gate": True,
            },
            "runtime_dependencies": dependency_state,
            "tool_categories": dict(sorted(tool_categories.items())),
            "read_only_tool_count": len(read_only_tools),
            "mutation_tool_count": len(mutation_tools),
            "approval_required_tools": sorted(approval_required_tools),
            "gateway_adapter_count": len(gateway_adapters),
            "llm_provider_count": len(llm_providers),
            "limitations": limitations,
            "answering_guidance": [
                "Use this live report as the evidence source for questions about LeapFlow capabilities.",
                (
                    "State configuration-dependent capabilities as available only when "
                    "their dependency flags are ready."
                ),
                "If this report is unavailable, say that live capability verification failed instead of guessing.",
            ],
        }

    def _profile_layout_or_none(self) -> Any:
        try:
            from leapflow.config import get_settings

            return getattr(get_settings(), "profile_layout", None)
        except (RuntimeError, AttributeError, ImportError):
            return None

    def _safe_install_dir(self) -> str:
        try:
            return str(self._resolve_install_dir())
        except (RuntimeError, AttributeError, ImportError):
            return ""

    @staticmethod
    def _capability_limitations(dependency_state: dict[str, Any]) -> list[str]:
        limitations: list[str] = []
        if not dependency_state["approval_gate_bound"]:
            limitations.append("Mutation tools fail closed until plugin_approval_gate is bound.")
        if not dependency_state["llm_provider_bound"]:
            limitations.append("plugin_generate cannot run until an LLM provider is bound.")
        if not dependency_state["plugin_generation_enabled"]:
            limitations.append("plugin_generate is disabled by configuration.")
        if not dependency_state["marketplace_configured"]:
            limitations.append("Marketplace installs require a configured marketplace client.")
        if not dependency_state["proposal_store_available"]:
            limitations.append("Plugin proposals require a profile layout or injected proposal store.")
        if not dependency_state["version_store_available"]:
            limitations.append("Plugin versioning requires a profile layout or injected version store.")
        return limitations

    async def _plugin_status_handler(self, plugin_id: str, **kwargs: Any) -> Dict[str, Any]:
        """Detailed information about a specific plugin."""
        from leapflow.plugins import get_registry, get_scoped_registry

        try:
            reg = get_registry()
            plugin = reg.get_plugin(plugin_id)
            if plugin is None:
                return {"ok": False, "error": f"Plugin '{plugin_id}' not registered"}

            scoped = get_scoped_registry()
            fiber = scoped.get_fiber(plugin_id)

            response = {
                "ok": True,
                "plugin_id": plugin_id,
                "category": plugin.category,
                "dependencies": list(plugin.dependencies),
                "tools": [
                    {"name": t.name, "description": t.description}
                    for t in plugin.tools
                ],
                "fiber": {
                    "state": fiber.state.value if fiber else "unmanaged",
                    "generation": fiber.generation if fiber else None,
                },
            }

            # Learning-driven trust and recommendation (purely additive)
            try:
                from leapflow.learning.plugin_advisor import get_default_advisor
                advisor = get_default_advisor()
                if advisor is not None:
                    trust = advisor._trust_ledger.level(plugin_id)
                    response["trust_level"] = trust.name
                    rec = advisor.recommend(plugin_id)
                    if rec is not None:
                        response["recommendation"] = {
                            "action": rec.action,
                            "reason": rec.reason,
                            "confidence": rec.confidence,
                        }
            except (ImportError, AttributeError, RuntimeError):
                pass  # Learning integration not wired — degrade gracefully

            return response
        except (RuntimeError, AttributeError) as exc:
            logger.warning("plugin_status failed for %s: %s", plugin_id, exc, exc_info=True)
            return {"ok": False, "error": f"plugin_status failed: {exc}"}

    # ── Generation (produces code, does NOT install) ──────────

    async def _plugin_propose_handler(
        self,
        requested_capability: str,
        plugin_id: str = "",
        proposed_tools: list[str] | None = None,
        test_cases: list[dict[str, Any]] | None = None,
        risk_level: str = "read_only",
        evidence: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Create a side-effect-free plugin proposal from explicit evidence."""
        try:
            from leapflow.learning.capability_gap_detector import CapabilityGapDetector
        except ImportError as exc:
            return {"ok": False, "error": f"Capability gap detector unavailable: {exc}"}

        detector = CapabilityGapDetector()
        try:
            proposal = None
            if evidence and evidence.get("error_type") == "unknown_tool":
                proposal = detector.proposal_from_unknown_tool(
                    evidence,
                    requested_capability=requested_capability,
                )
            if proposal is None:
                proposal = detector.proposal_from_capability_request(
                    requested_capability,
                    plugin_id=plugin_id,
                    proposed_tool_names=tuple(proposed_tools or ()),
                    risk_level=risk_level,  # type: ignore[arg-type]
                    evidence_summary=str((evidence or {}).get("summary") or ""),
                )
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"Proposal failed: {exc}"}

        if test_cases:
            try:
                from leapflow.domain.plugin_proposal import BehaviorTestCase, PluginProposal
                parsed_tests = tuple(
                    BehaviorTestCase.create(
                        str(item.get("tool_name") or ""),
                        arguments=dict(item.get("arguments") or {}),
                        expected_subset=dict(item.get("expected_subset") or {}),
                        description=str(item.get("description") or ""),
                    )
                    for item in test_cases
                    if isinstance(item, dict)
                )
                proposal = PluginProposal(
                    proposal_id=proposal.proposal_id,
                    plugin_id=proposal.plugin_id,
                    capability_summary=proposal.capability_summary,
                    gap_type=proposal.gap_type,
                    risk_level=proposal.risk_level,
                    status=proposal.status,
                    evidence=proposal.evidence,
                    proposed_tools=proposal.proposed_tools,
                    test_cases=parsed_tests,
                    created_at=proposal.created_at,
                )
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": f"Proposal test case parsing failed: {exc}"}

        try:
            stored = self._proposal_store().save(proposal)
        except (RuntimeError, OSError, ValueError, AttributeError) as exc:
            return {"ok": False, "error": f"Proposal persistence failed: {exc}"}

        return {
            "ok": True,
            "action": "propose",
            "proposal": stored.to_dict(),
            "next_actions": [
                "Review proposal fields and risk level.",
                "If acceptable, call plugin_generate with proposal_id to preserve review metadata.",
                "Install generated code separately with plugin_install(proposal_id=...) after validation and approval.",
            ],
        }

    async def _plugin_generate_handler(
        self, plugin_id: str = "", description: str = "", proposal_id: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """Generate a new plugin via LLM and validate it. Returns validated code (does NOT install).

        This is the LLM co-evolution entry point: describe a capability need,
        the LLM generates conformant plugin code, and it's rigorously validated.
        Installation is a SEPARATE approval-gated step (plugin_install).
        """
        if proposal_id:
            proposal = self._proposal_store().get(proposal_id)
            if proposal is None:
                return {"ok": False, "error": f"Plugin proposal '{proposal_id}' not found"}
            plugin_id = plugin_id or proposal.plugin_id
            description = description or proposal.capability_summary
        if not plugin_id or not description:
            return {"ok": False, "error": "plugin_id and description are required unless proposal_id is provided"}

        if not self._plugin_generation_enabled:
            return {
                "ok": False,
                "error": (
                    "Plugin generation is disabled. "
                    "Set plugin_generation_enabled=true in config to opt in."
                ),
            }

        try:
            from leapflow.learning.plugin_generator import PluginGenerator, PluginGenerationRequest
        except ImportError as exc:
            return {"ok": False, "error": f"Generation module unavailable: {exc}"}

        if self._llm_provider is None:
            return {
                "ok": False,
                "error": (
                    "No LLM provider available for plugin generation. "
                    "Wire an llm_provider into self_management via bind_runtime "
                    "(requires daemon-mode with LLM credentials configured)."
                ),
            }

        try:
            generator = PluginGenerator(llm_provider=self._llm_provider)
            request = PluginGenerationRequest(plugin_id=plugin_id, description=description)
            result = await generator.generate_and_validate(request)
            if proposal_id:
                result["proposal_id"] = proposal_id
                if result.get("ok"):
                    self._proposal_store().update_status(proposal_id, "review")
            return result
        except (AttributeError, RuntimeError) as exc:
            return {"ok": False, "error": f"Generation failed: {exc}"}

    # ── Compatibility assessment (read-only) ─────────────────

    async def _assess_compatibility_handler(self, manifest: dict = None, **kwargs: Any) -> Dict[str, Any]:
        """Assess whether a foreign plugin manifest is compatible with LeapFlow."""
        if manifest is None:
            manifest = kwargs.get("manifest")
        if not manifest or not isinstance(manifest, dict):
            return {"ok": False, "error": "manifest parameter is required (dict)"}

        try:
            from leapflow.learning.compatibility import assess_plugin

            report = assess_plugin(manifest)
            return {
                "ok": True,
                "final_verdict": report.final_verdict.value,
                "is_installable": report.is_installable(),
                "target_protocol": report.target_protocol,
                "rejection_reason": report.rejection_reason,
                "adaptation_notes": report.adaptation_notes,
                "adapter_spec": {
                    "source_interface": report.adapter_spec.source_interface,
                    "target_protocol": report.adapter_spec.target_protocol,
                    "bridge_type": report.adapter_spec.bridge_type,
                    "shim_methods": report.adapter_spec.shim_methods,
                    "estimated_complexity": report.adapter_spec.estimated_complexity,
                } if report.adapter_spec else None,
                "stages": [
                    {
                        "stage_name": s.stage_name,
                        "passed": s.passed,
                        "verdict": s.verdict.value if s.verdict else None,
                        "details": s.details,
                    }
                    for s in report.stages
                ],
                "manifest_name": report.manifest.name,
                "manifest_version": report.manifest.version,
            }
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("assess_compatibility failed: %s", exc, exc_info=True)
            return {"ok": False, "error": f"Assessment failed: {exc}"}

    # ── State-mutating (requires approval) ─────────────────

    async def _plugin_install_handler(
        self, plugin_id: str = "", code: str = "", marketplace_name: str = "", proposal_id: str = "", version_label: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """Install a plugin from validated code or marketplace, then load it. REQUIRES approval.

        Two modes:
        - code: install directly from a validated code string (from plugin_generate)
        - marketplace_name: install from the configured marketplace

        Installed code is written into the profile-scoped plugins directory
        (ProfileLayout.plugins_dir) and loaded dynamically — never into the
        read-only Python package directory. Before a plugin is made live it is
        smoke-tested in an isolated subprocess (SandboxHost). Any failure path
        rolls back cleanly: no half-initialized fiber and no orphaned file.
        """
        proposal = None
        if proposal_id:
            proposal = self._proposal_store().get(proposal_id)
            if proposal is None:
                return {"ok": False, "error": f"Plugin proposal '{proposal_id}' not found"}
            plugin_id = plugin_id or proposal.plugin_id
        if not plugin_id:
            return {"ok": False, "error": "plugin_id is required unless proposal_id is provided"}

        approved, denial = await self._check_approval("install", plugin_id, proposal_id=proposal_id)
        if not approved:
            return {"ok": False, "error": denial, "requires_approval": True}

        from leapflow.plugins import get_registry

        # R1: reject a duplicate plugin_id BEFORE creating any fiber or writing
        # any file, so a re-install cannot leave a half-initialized fiber.
        if get_registry().get_plugin(plugin_id) is not None:
            return {
                "ok": False,
                "error": (
                    f"Plugin '{plugin_id}' is already registered; "
                    "use plugin_reload or choose a new id"
                ),
            }

        if code and marketplace_name:
            return {"ok": False, "error": "Provide either code or marketplace_name, not both"}

        try:
            if code:
                result = await self._install_from_code(plugin_id, code, proposal=proposal, version_label=version_label)
            elif marketplace_name:
                # Run compatibility gate for marketplace installs (BLOCKING)
                result = await self._install_from_marketplace_with_gate(plugin_id, marketplace_name)
            else:
                return {"ok": False, "error": "Must provide either code or marketplace_name"}
            if proposal_id:
                result["proposal_id"] = proposal_id
                if result.get("ok"):
                    self._proposal_store().update_status(proposal_id, "approved")
            return result
        except (ImportError, AttributeError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("plugin_install failed for %s: %s", plugin_id, exc, exc_info=True)
            return {"ok": False, "error": f"Install failed: {exc}"}

    def _resolve_install_dir(self) -> "Path":
        """Resolve the profile-scoped directory for installed plugin code.

        Precedence: the injected ``plugin_install_dir`` (from bind_runtime) ->
        the active ``ProfileLayout.plugins_dir`` -> a plugins dir under the data
        root. Always profile-scoped; never the Python package directory.
        """
        from pathlib import Path

        if self._plugin_install_dir:
            return Path(self._plugin_install_dir)
        from leapflow.config import get_settings

        settings = get_settings()
        profile_layout = getattr(settings, "profile_layout", None)
        if profile_layout is not None:
            return profile_layout.plugins_dir
        return Path(settings.layout.root) / "plugins"

    def _proposal_store(self) -> Any:
        """Resolve the profile-scoped proposal store."""
        if self._plugin_proposal_store is not None:
            return self._plugin_proposal_store
        from leapflow.config import get_settings
        from leapflow.storage.plugin_proposal_store import JsonPluginProposalStore

        settings = get_settings()
        profile_layout = getattr(settings, "profile_layout", None)
        if profile_layout is None:
            raise RuntimeError("profile_layout is required for plugin proposal storage")
        self._plugin_proposal_store = JsonPluginProposalStore(profile_layout.plugin_proposals_path)
        return self._plugin_proposal_store

    def _version_store(self) -> Any:
        """Resolve the profile-scoped plugin version store."""
        if self._plugin_version_store is not None:
            return self._plugin_version_store
        from leapflow.config import get_settings
        from leapflow.storage.plugin_version_store import PluginVersionStore

        settings = get_settings()
        profile_layout = getattr(settings, "profile_layout", None)
        if profile_layout is None:
            raise RuntimeError("profile_layout is required for plugin version storage")
        self._plugin_version_store = PluginVersionStore(profile_layout.plugin_versions_dir)
        return self._plugin_version_store

    async def _install_from_code(
        self, plugin_id: str, code: str, *, proposal: Any = None, version_label: str = ""
    ) -> Dict[str, Any]:
        """Re-validate, write to the profile dir, smoke test, then load in-process."""
        from leapflow.learning.plugin_generator import PluginValidator

        validator = PluginValidator()
        vresult = await validator.validate(plugin_id, code)
        if not vresult.ok:
            return {
                "ok": False,
                "error": f"Code failed re-validation at stage '{vresult.stage}': {vresult.error}",
            }

        install_dir = self._resolve_install_dir()
        install_dir.mkdir(parents=True, exist_ok=True)
        target = install_dir / f"{plugin_id}.py"
        target.write_text(code)

        # D3: real subprocess smoke test before the plugin is made live.
        smoke_ok, smoke_err = await self._sandbox_smoke_test(plugin_id, install_dir)
        if not smoke_ok:
            self._safe_unlink(target)
            return {"ok": False, "error": smoke_err}

        result = self._register_inprocess(plugin_id, plugin_id, target)
        if not result.get("ok"):
            return result
        if proposal is not None and getattr(proposal, "test_cases", ()):
            ok, error, observations = await self._run_behavior_tests_for_plugin(
                plugin_id, tuple(getattr(proposal, "test_cases", ()) or ())
            )
            result["behavior_tests"] = observations
            if not ok:
                from leapflow.plugins import get_scoped_registry

                scoped = get_scoped_registry()
                try:
                    scoped.dispose_plugin(plugin_id, prune_metadata=True)
                except KeyError:
                    pass
                self._safe_unlink(target)
                return {"ok": False, "error": f"Behavior tests failed: {error}"}
        try:
            version_info = self._version_store().record_source(
                plugin_id,
                target,
                version=version_label,
                metadata={"source": "plugin_install", "proposal_id": getattr(proposal, "proposal_id", "")},
            )
            result["version"] = version_info.get("version", "")
        except (RuntimeError, OSError, ValueError, AttributeError) as exc:
            logger.debug("plugin version recording skipped for %s: %s", plugin_id, exc, exc_info=True)
        return result

    async def _install_from_marketplace_with_gate(
        self, plugin_id: str, marketplace_name: str
    ) -> Dict[str, Any]:
        """Install from marketplace with compatibility gate pre-check.

        Runs assess_plugin() on the resolved manifest before attempting install.
        If verdict is INCOMPATIBLE → returns structured error without install.
        If ADAPTABLE → includes adaptation_notes alongside the install result.
        """
        client = self._marketplace_client
        if client is None:
            return {
                "ok": False,
                "error": (
                    "Marketplace not configured "
                    "(set plugin_marketplace_root or plugin_marketplace_url)"
                ),
            }

        # Resolve manifest for compatibility check
        try:
            manifest_data = client.resolve_manifest(marketplace_name)
        except (OSError, ValueError, RuntimeError, AttributeError):
            manifest_data = None

        compatibility_notes: list[str] = []
        if manifest_data and isinstance(manifest_data, dict):
            try:
                from leapflow.learning.compatibility import assess_plugin

                report = assess_plugin(manifest_data)
                if not report.is_installable():
                    return {
                        "ok": False,
                        "error": (
                            f"Compatibility gate: plugin '{marketplace_name}' is INCOMPATIBLE "
                            f"with LeapFlow. Reason: {report.rejection_reason}"
                        ),
                        "verdict": report.final_verdict.value,
                        "rejection_reason": report.rejection_reason,
                    }
                if report.adaptation_notes:
                    compatibility_notes = list(report.adaptation_notes)
            except (ImportError, AttributeError, TypeError, ValueError):
                pass  # Degrade gracefully — proceed without gate

        result = await self._install_from_marketplace(plugin_id, marketplace_name)
        if compatibility_notes and result.get("ok"):
            result["compatibility_notes"] = compatibility_notes
        return result

    async def _install_from_marketplace(self, plugin_id: str, marketplace_name: str) -> Dict[str, Any]:
        """Install via the configured MarketplaceClient with verification + smoke test."""
        from pathlib import Path

        client = self._marketplace_client
        if client is None:
            return {
                "ok": False,
                "error": (
                    "Marketplace not configured "
                    "(set plugin_marketplace_root or plugin_marketplace_url)"
                ),
            }

        try:
            result = client.install(
                marketplace_name,
                verify=True,
                trusted_pubkeys=(self._trusted_pubkeys or None),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return {"ok": False, "error": f"Marketplace install failed: {exc}"}

        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "Marketplace install failed")}

        installed_path = Path(str(result["installed_path"]))
        module_name = installed_path.stem
        requires_sandbox = bool(result.get("requires_sandbox"))

        smoke_ok, smoke_err = await self._sandbox_smoke_test(module_name, installed_path.parent)
        if not smoke_ok:
            self._safe_unlink(installed_path)
            return {"ok": False, "error": smoke_err}

        if requires_sandbox:
            return await self._register_sandboxed(plugin_id, module_name, installed_path)
        return self._register_inprocess(plugin_id, module_name, installed_path)

    async def _sandbox_smoke_test(
        self, module_name: str, install_dir: "Path", *, timeout_s: float = 15.0
    ) -> tuple[bool, str]:
        """Load the module in a sandbox worker and invoke its first tool once.

        Returns (ok, error). A host-level failure (worker crash/timeout/comm
        error, signalled by an empty ``error_type``) fails the test. A tool
        that raises but is caught at the isolation boundary (non-empty
        ``error_type``) still counts as success: the module loaded and the
        handler is invocable, which is all the smoke test asserts.
        """
        import os

        from leapflow.plugins.sandbox.sandbox_host import SandboxHost

        host = SandboxHost(module_name, invoke_timeout_s=timeout_s)
        # The worker imports the plugin by module name; make the install dir
        # importable for the child process during startup only.
        started = False
        original_pp = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = os.pathsep.join(
            [str(install_dir)] + ([original_pp] if original_pp else [])
        )
        try:
            await host.start()
            started = True
        except (OSError, RuntimeError, ValueError) as exc:
            return False, f"Sandbox smoke test error: {exc}"
        finally:
            if original_pp is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = original_pp
        if not started:
            return False, "Sandbox smoke test failed: worker did not start"

        try:
            if not await host.ping():
                return False, "Sandbox smoke test failed: worker did not respond"
            tool_names = await host.list_tools()
            if not tool_names:
                return False, (
                    "Sandbox smoke test failed: plugin exposed no tools "
                    "(likely failed to import in isolation)"
                )
            resp = await host.invoke(tool_names[0], {})
            if not resp.ok and not resp.error_type:
                return False, f"Sandbox smoke test failed: {resp.error}"
            return True, ""
        finally:
            try:
                await host.stop()
            except (OSError, RuntimeError):
                pass

    def _register_inprocess(
        self, plugin_id: str, module_name: str, target: "Path"
    ) -> Dict[str, Any]:
        """Dynamically load the installed module and register it on the registry.

        On any failure the fiber is disposed, the module removed from
        ``sys.modules``, and the written file deleted — no partial state remains.
        """
        import sys

        from leapflow.plugins import get_registry, get_scoped_registry

        new_plugin, load_err = self._load_from_path(module_name, target)
        if new_plugin is None:
            self._safe_unlink(target)
            return {"ok": False, "error": load_err}

        reg = get_registry()
        scoped = get_scoped_registry()
        fiber = scoped.create_fiber(plugin_id)
        try:
            scoped.scoped_register(new_plugin, fiber)
            fiber.activate()
            installed_tools = reg.publish_plugin_tools(new_plugin)
        except (RuntimeError, ValueError, AttributeError, TypeError) as exc:
            self._rollback_fiber(scoped, plugin_id, fiber)
            sys.modules.pop(module_name, None)
            self._safe_unlink(target)
            return {"ok": False, "error": f"Registration failed: {exc}"}

        return {
            "ok": True,
            "action": "install",
            "plugin_id": plugin_id,
            "installed_tools": installed_tools,
            "state": fiber.state.value,
        }

    async def _register_sandboxed(
        self, plugin_id: str, module_name: str, installed_path: "Path"
    ) -> Dict[str, Any]:
        """Register a marketplace plugin that must run isolated in a subprocess.

        The untrusted code is never imported in-process: tool names come from
        the sandbox worker and every handler proxies to it via
        SandboxedToolPlugin. The worker is stopped when the fiber is disposed.
        """
        import os

        from leapflow.plugins import get_registry, get_scoped_registry
        from leapflow.plugins.protocol import ToolMetadata
        from leapflow.plugins.sandbox.sandbox_host import SandboxHost, SandboxedToolPlugin

        install_dir = installed_path.parent
        host = SandboxHost(module_name)
        started = False
        original_pp = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = os.pathsep.join(
            [str(install_dir)] + ([original_pp] if original_pp else [])
        )
        try:
            await host.start()
            started = True
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "error": f"Sandbox start failed: {exc}"}
        finally:
            if original_pp is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = original_pp
        if not started:
            return {"ok": False, "error": "Sandbox start failed"}

        tool_names = await host.list_tools()
        if not tool_names:
            await host.stop()
            self._safe_unlink(installed_path)
            return {"ok": False, "error": "Sandboxed plugin exposed no tools"}

        metadatas = [
            ToolMetadata(
                name=name,
                description=f"Sandboxed marketplace tool '{name}' from plugin '{plugin_id}'.",
                parameters_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
                handler=self._noop_handler,
                x_leapflow={"category": "marketplace", "risk_level": "high"},
                mutates_state=True,
            )
            for name in tool_names
        ]
        sandboxed = SandboxedToolPlugin(plugin_id, "marketplace", metadatas, host)

        reg = get_registry()
        scoped = get_scoped_registry()
        fiber = scoped.create_fiber(plugin_id)
        try:
            scoped.scoped_register(sandboxed, fiber)
            fiber.activate()
            installed_tools = reg.publish_plugin_tools(sandboxed)
            # Stop the worker subprocess when the fiber is disposed.
            fiber.scope.effect(lambda h=host: self._schedule_host_stop(h))
        except (RuntimeError, ValueError, AttributeError, TypeError) as exc:
            self._rollback_fiber(scoped, plugin_id, fiber)
            await host.stop()
            self._safe_unlink(installed_path)
            return {"ok": False, "error": f"Sandboxed registration failed: {exc}"}

        return {
            "ok": True,
            "action": "install",
            "plugin_id": plugin_id,
            "installed_tools": installed_tools,
            "state": fiber.state.value,
            "sandboxed": True,
        }

    @staticmethod
    async def _noop_handler(**kwargs: Any) -> Dict[str, Any]:
        """Placeholder handler replaced by SandboxedToolPlugin's proxy at wrap time."""
        return {"ok": False, "error": "handler not bound"}

    def _load_from_path(self, module_name: str, path: "Path") -> "tuple[Any, str]":
        """Load a plugin module from a file path and register it in sys.modules.

        Registering under ``module_name`` (which becomes the plugin class's
        ``__module__``) lets the scoped registry's reload() find it later via
        ``importlib.reload(sys.modules[module_name])`` — file-path modules keep
        a valid loader spec, so reload/enable work for installed plugins.

        Returns (plugin_obj, "") on success or (None, error) on failure.
        """
        import importlib.util
        import sys

        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                return None, f"Cannot create import spec for {path}"
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - importing installed plugin code can raise anything
            sys.modules.pop(module_name, None)
            return None, f"Failed to load installed module: {exc}"

        plugin_obj = getattr(module, "plugin", None)
        if plugin_obj is None:
            sys.modules.pop(module_name, None)
            return None, "Installed module has no 'plugin' attribute"
        try:
            setattr(plugin_obj, "__leapflow_plugin_path__", str(path))
        except Exception:
            logger.debug("Cannot attach plugin source path metadata for %s", module_name, exc_info=True)
        return plugin_obj, ""

    @staticmethod
    def _rollback_fiber(scoped: Any, plugin_id: str, fiber: Any) -> None:
        """Dispose a fiber and drop it from the scoped registry (rollback path)."""
        from leapflow.domain.plugin_fiber import FiberState

        try:
            if fiber.state == FiberState.ACTIVE:
                fiber.begin_unload()
            if fiber.state != FiberState.DISPOSED:
                fiber.dispose()
        except (RuntimeError, ValueError, AttributeError):
            pass
        scoped._fibers.pop(plugin_id, None)

    @staticmethod
    def _schedule_host_stop(host: Any) -> None:
        """Best-effort async shutdown of a sandbox worker on fiber disposal."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(host.stop())

    @staticmethod
    def _safe_unlink(path: "Path") -> None:
        """Remove a written plugin file, ignoring absence/IO errors."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _active_snapshot_path(self, plugin_id: str) -> "Path | None":
        """Return the active version snapshot path, if one is recorded."""
        try:
            active = self._version_store().active(plugin_id)
        except (RuntimeError, OSError, ValueError, AttributeError):
            return None
        if not isinstance(active, dict):
            return None
        raw_path = str(active.get("snapshot_path") or "")
        if not raw_path:
            return None
        path = Path(raw_path)
        return path if path.exists() else None

    def _active_proposal_tests(self, plugin_id: str) -> tuple[str, tuple[Any, ...], str]:
        """Return behavior tests linked to the plugin's active proposal, if any."""
        try:
            active = self._version_store().active(plugin_id)
        except (RuntimeError, OSError, ValueError, AttributeError) as exc:
            logger.debug("Cannot read active plugin version for %s: %s", plugin_id, exc, exc_info=True)
            return "", (), ""
        if not isinstance(active, dict):
            return "", (), ""
        metadata = active.get("metadata")
        if not isinstance(metadata, dict):
            return "", (), ""
        proposal_id = str(metadata.get("proposal_id") or "")
        if not proposal_id:
            return "", (), ""
        try:
            proposal = self._proposal_store().get(proposal_id)
        except (RuntimeError, OSError, ValueError, AttributeError) as exc:
            return proposal_id, (), f"Plugin proposal '{proposal_id}' unavailable for behavior tests: {exc}"
        if proposal is None:
            return proposal_id, (), f"Plugin proposal '{proposal_id}' not found for behavior tests"
        return proposal_id, tuple(getattr(proposal, "test_cases", ()) or ()), ""

    async def _run_behavior_tests_for_plugin(
        self, plugin_id: str, test_cases: tuple[Any, ...]
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """Execute behavior tests against the currently registered plugin instance."""
        if not test_cases:
            return True, "", []
        from leapflow.learning.plugin_behavior_tests import run_plugin_behavior_tests
        from leapflow.plugins import get_registry

        plugin = get_registry().get_plugin(plugin_id)
        if plugin is None:
            return False, f"Plugin '{plugin_id}' is not registered for behavior tests", []
        return await run_plugin_behavior_tests(plugin, test_cases)

    def _restore_plugin_source(
        self,
        plugin_id: str,
        source_path: "Path | None",
        snapshot_path: "Path | None",
    ) -> str:
        """Restore a previous source snapshot and reload it; return an error string on failure."""
        if source_path is None or snapshot_path is None:
            return "no previous source snapshot is available"
        try:
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(snapshot_path.read_bytes())
            from leapflow.plugins import reload_plugin

            reload_plugin(plugin_id)
            return ""
        except (OSError, RuntimeError, KeyError, AttributeError) as exc:
            logger.warning("plugin rollback after failed behavior tests failed: %s", exc, exc_info=True)
            return str(exc)

    async def _plugin_versions_handler(self, plugin_id: str, **kwargs: Any) -> Dict[str, Any]:
        """List recorded versions and the active pointer for a profile plugin."""
        try:
            store = self._version_store()
            return {
                "ok": True,
                "plugin_id": plugin_id,
                "active": store.active(plugin_id),
                "versions": store.versions(plugin_id),
            }
        except (RuntimeError, OSError, ValueError, AttributeError) as exc:
            return {"ok": False, "error": f"Version query failed: {exc}"}

    async def _plugin_rollback_handler(self, plugin_id: str, version: str, **kwargs: Any) -> Dict[str, Any]:
        """Rollback a profile plugin to a recorded source snapshot and reload it."""
        approved, denial = await self._check_approval("rollback", plugin_id)
        if not approved:
            return {"ok": False, "error": denial, "requires_approval": True}
        try:
            from leapflow.plugins import reload_plugin

            target = self._resolve_install_dir() / f"{plugin_id}.py"
            entry = self._version_store().rollback(plugin_id, version, target)
            fiber = reload_plugin(plugin_id)
            return {
                "ok": True,
                "action": "rollback",
                "plugin_id": plugin_id,
                "version": entry.get("version", version),
                "state": fiber.state.value,
                "new_generation": fiber.generation,
            }
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        except (RuntimeError, OSError, AttributeError) as exc:
            logger.warning("plugin_rollback failed: %s", exc, exc_info=True)
            return {"ok": False, "error": f"Rollback failed: {exc}"}

    async def _plugin_enable_handler(self, plugin_id: str, **kwargs: Any) -> Dict[str, Any]:
        """Re-enable a previously disabled plugin. REQUIRES approval.

        This calls reload_plugin internally, which re-imports the module
        and registers a fresh instance with a new fiber.
        """
        if plugin_id == "self_management":
            return {"ok": False, "error": "Cannot enable self_management (already active)"}

        approved, denial = await self._check_approval("enable", plugin_id)
        if not approved:
            return {"ok": False, "error": denial, "requires_approval": True}

        try:
            from leapflow.plugins import reload_plugin
            new_fiber = reload_plugin(plugin_id)
            return {
                "ok": True,
                "action": "enable",
                "plugin_id": plugin_id,
                "new_generation": new_fiber.generation,
                "state": new_fiber.state.value,
            }
        except KeyError:
            return {"ok": False, "error": f"Plugin '{plugin_id}' not found in scoped registry"}
        except RuntimeError as exc:
            return {"ok": False, "error": f"Enable failed: {exc}"}

    async def _check_approval(
        self, action: str, plugin_id: str, *, proposal_id: str = ""
    ) -> tuple[bool, str]:
        """Consult the plugin approval gate. Returns (approved, denial_message).

        Progressive Trust: PRODUCTION-level plugins get auto-approved for
        'reload' (which is idempotent). 'disable' and 'enable' always require
        human approval regardless of trust level.
        """
        # Progressive Trust: auto-approve reload for PRODUCTION-level plugins
        if action == "reload":
            try:
                from leapflow.learning.plugin_advisor import get_default_advisor

                advisor = get_default_advisor()
                if advisor is not None:
                    trust = advisor._trust_ledger.level(plugin_id)
                    if trust.name == "PRODUCTION":
                        logger.info(
                            "Auto-approving '%s' on plugin '%s' (trust: PRODUCTION)",
                            action,
                            plugin_id,
                        )
                        return True, ""
            except (ImportError, AttributeError, RuntimeError):
                pass  # Learning not wired — fall through to gate

        # Standard gate check
        if self._plugin_approval_gate is None:
            # No gate installed: for safety, deny mutation
            return False, (
                f"Plugin action '{action}' on '{plugin_id}' blocked: "
                "no approval gate configured. Configure a plugin_approval_gate "
                "in the daemon approval coordinator to enable self-modification."
            )
        try:
            from leapflow.security.actions import ActionDescriptor
            descriptor = ActionDescriptor.platform_action(
                "plugin_management",
                action,
                {"plugin_id": plugin_id},
                metadata={
                    "effect": "write",
                    "risk_level": "high",
                    "category": "self_modification",
                    "proposal_id": proposal_id,
                },
            )
            result = await self._plugin_approval_gate.evaluate(descriptor)
            if getattr(result, "approved", False):
                return True, ""
            message = str(
                getattr(result, "denial_message", "")
                or f"Plugin action '{action}' on '{plugin_id}' requires approval (denied)"
            )
            return False, message
        except (ImportError, AttributeError, RuntimeError) as exc:
            logger.warning("approval check failed: %s", exc, exc_info=True)
            return False, f"Plugin action '{action}' blocked: approval check error"

    async def _plugin_reload_handler(self, plugin_id: str, version_label: str = "", **kwargs: Any) -> Dict[str, Any]:
        """Hot-reload a plugin. REQUIRES approval."""
        approved, denial = await self._check_approval("reload", plugin_id)
        if not approved:
            return {"ok": False, "error": denial, "requires_approval": True}

        try:
            from leapflow.plugins import get_scoped_registry, reload_plugin

            scoped = get_scoped_registry()
            source_path = scoped.get_plugin_file(plugin_id)
            previous_snapshot = self._active_snapshot_path(plugin_id)
            proposal_id, test_cases, test_error = self._active_proposal_tests(plugin_id)
            if test_error:
                return {"ok": False, "error": test_error}

            new_fiber = reload_plugin(plugin_id)
            behavior_observations: list[dict[str, Any]] = []
            if test_cases:
                ok, error, behavior_observations = await self._run_behavior_tests_for_plugin(
                    plugin_id, test_cases
                )
                if not ok:
                    restore_error = self._restore_plugin_source(
                        plugin_id, source_path, previous_snapshot
                    )
                    response: Dict[str, Any] = {
                        "ok": False,
                        "error": f"Behavior tests failed: {error}",
                        "plugin_id": plugin_id,
                        "proposal_id": proposal_id,
                        "behavior_tests": behavior_observations,
                        "rolled_back": restore_error == "",
                    }
                    if restore_error:
                        response["rollback_error"] = restore_error
                    return response

            version = ""
            if version_label:
                source_path = scoped.get_plugin_file(plugin_id)
                if source_path is not None:
                    version_info = self._version_store().record_source(
                        plugin_id,
                        source_path,
                        version=version_label,
                        metadata={"source": "plugin_reload", "proposal_id": proposal_id},
                    )
                    version = str(version_info.get("version") or "")
            response = {
                "ok": True,
                "action": "reload",
                "plugin_id": plugin_id,
                "new_generation": new_fiber.generation,
                "state": new_fiber.state.value,
                "version": version,
            }
            if behavior_observations:
                response["proposal_id"] = proposal_id
                response["behavior_tests"] = behavior_observations
            return response
        except KeyError:
            return {"ok": False, "error": f"Plugin '{plugin_id}' not scoped-registered"}
        except RuntimeError as exc:
            return {"ok": False, "error": f"Reload failed: {exc}"}

    async def _plugin_disable_handler(self, plugin_id: str, **kwargs: Any) -> Dict[str, Any]:
        """Disable a plugin by disposing its fiber. REQUIRES approval.

        Note: this removes the plugin's tools from the registry until process restart
        or explicit re-enable (not yet implemented).
        """
        # Protect against self-destruction
        if plugin_id == "self_management":
            return {
                "ok": False,
                "error": "Cannot disable self_management plugin (would remove this tool)",
            }

        approved, denial = await self._check_approval("disable", plugin_id)
        if not approved:
            return {"ok": False, "error": denial, "requires_approval": True}

        try:
            from leapflow.plugins import get_scoped_registry
            scoped = get_scoped_registry()
            fiber = scoped.dispose_plugin(plugin_id)

            return {
                "ok": True,
                "action": "disable",
                "plugin_id": plugin_id,
                "state": fiber.state.value,
            }
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        except (RuntimeError, AttributeError) as exc:
            logger.warning("plugin_disable failed: %s", exc, exc_info=True)
            return {"ok": False, "error": f"Disable failed: {exc}"}

    async def _plugin_remove_handler(
        self, plugin_id: str, delete_source: bool = True, **kwargs: Any
    ) -> Dict[str, Any]:
        """Terminally remove a plugin: dispose fiber, unregister tools, delete source."""
        if plugin_id == "self_management":
            return {
                "ok": False,
                "error": "Cannot remove self_management plugin (would remove this tool)",
            }

        approved, denial = await self._check_approval("remove", plugin_id)
        if not approved:
            return {"ok": False, "error": denial, "requires_approval": True}

        try:
            import sys

            from leapflow.plugins import get_scoped_registry

            scoped = get_scoped_registry()
            source_path = scoped.get_plugin_file(plugin_id)
            module_path = scoped.get_plugin_module(plugin_id)
            fiber = scoped.dispose_plugin(plugin_id, prune_metadata=True)
            if module_path:
                sys.modules.pop(module_path, None)
            source_deleted = False
            if delete_source:
                target = source_path or (self._resolve_install_dir() / f"{plugin_id}.py")
                if target.exists():
                    target.unlink()
                    source_deleted = True
            return {
                "ok": True,
                "action": "remove",
                "plugin_id": plugin_id,
                "state": fiber.state.value,
                "source_path": str(source_path or ""),
                "source_deleted": source_deleted,
            }
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        except (RuntimeError, AttributeError, OSError) as exc:
            logger.warning("plugin_remove failed: %s", exc, exc_info=True)
            return {"ok": False, "error": f"Remove failed: {exc}"}

    # ── Tool metadata ──────────────────────────────────────

    @property
    def tools(self) -> list[ToolMetadata]:
        return [
            ToolMetadata(
                name="plugin_list",
                description=(
                    "List the live plugin registry and cross-subsystem capability evidence. "
                    "Use this before answering questions about whether LeapFlow supports plugins, "
                    "self-evolution, plugin installation, hot reload, versioning, or other runtime capabilities."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                handler=self._plugin_list_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "read_only",
                    "schema_cost": "low",
                    "requires_approval": False,
                    "summary": "list live plugins and self capability evidence",
                },
            ),
            ToolMetadata(
                name="plugin_status",
                description=(
                    "Get detailed status of a specific plugin: its declared category, "
                    "runtime dependencies, contributed tools, and fiber lifecycle state."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "The plugin identifier (e.g. 'file_ops', 'web_access').",
                        },
                    },
                    "required": ["plugin_id"],
                },
                handler=self._plugin_status_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "read_only",
                    "schema_cost": "low",
                    "requires_approval": False,
                    "summary": "inspect one plugin's details",
                },
            ),
            ToolMetadata(
                name="plugin_versions",
                description="List recorded source versions and active pointer for a profile-scoped plugin.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "The plugin identifier to inspect.",
                        },
                    },
                    "required": ["plugin_id"],
                },
                handler=self._plugin_versions_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "read_only",
                    "schema_cost": "low",
                    "requires_approval": False,
                    "summary": "list plugin source versions",
                },
            ),
            ToolMetadata(
                name="plugin_propose",
                description=(
                    "Create a side-effect-free PluginProposal from explicit capability-gap evidence. "
                    "Use this before plugin_generate when a missing capability should be reviewed. "
                    "Does not call an LLM, write files, or install anything."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "requested_capability": {
                            "type": "string",
                            "description": "Capability the plugin should provide.",
                        },
                        "plugin_id": {
                            "type": "string",
                            "description": "Optional proposed plugin id; auto-derived when omitted.",
                        },
                        "proposed_tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional proposed tool names.",
                        },
                        "test_cases": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Optional behavior tests: {tool_name, arguments, expected_subset}.",
                        },
                        "risk_level": {
                            "type": "string",
                            "enum": ["read_only", "low", "medium", "high", "mutating", "external"],
                            "description": "Risk classification for the proposed plugin.",
                        },
                        "evidence": {
                            "type": "object",
                            "description": "Optional structured evidence such as an unknown_tool result.",
                        },
                    },
                    "required": ["requested_capability"],
                },
                handler=self._plugin_propose_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "read_only",
                    "schema_cost": "medium",
                    "requires_approval": False,
                    "effect_scope": "none",
                    "idempotency_scope": "turn",
                    "summary": "create a reviewable plugin proposal without side effects",
                },
            ),
            ToolMetadata(
                name="assess_compatibility",
                description=(
                    "Assess whether a foreign plugin manifest is compatible with "
                    "LeapFlow's plugin architecture. Returns a structured compatibility "
                    "report with verdict (COMPATIBLE/ADAPTABLE/PARTIAL/INCOMPATIBLE), "
                    "target protocol mapping, and adaptation notes."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "manifest": {
                            "type": "object",
                            "description": "The plugin manifest to assess (LeapFlow or DSH format).",
                        },
                    },
                    "required": ["manifest"],
                },
                handler=self._assess_compatibility_handler,
                x_leapflow={
                    "category": "plugin_management",
                    "risk_level": "none",
                    "schema_cost": "low",
                    "requires_approval": False,
                    "effect": "read",
                    "summary": "assess foreign plugin manifest compatibility",
                },
                mutates_state=False,
            ),
            ToolMetadata(
                name="plugin_generate",
                description=(
                    "Generate a new ToolPlugin from a natural-language capability "
                    "description. The LLM produces code that conforms to the "
                    "ToolPlugin Protocol; it is then rigorously validated "
                    "(syntax, structure, import, protocol conformance). The "
                    "isolated sandbox smoke test runs later, at install-time. "
                    "Returns the validated code but DOES NOT install it — "
                    "installation is a separate approval-gated step via plugin_install."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "Identifier for the new plugin and profile-scoped module filename.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Natural-language description of the capability the plugin should provide.",
                        },
                        "proposal_id": {
                            "type": "string",
                            "description": "Optional PluginProposal id to generate from; fills plugin_id/description when omitted.",
                        },
                    },
                    "required": [],
                },
                handler=self._plugin_generate_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "medium",
                    "schema_cost": "medium",
                    "requires_approval": False,
                    "effect_scope": "none",
                    "idempotency_scope": "turn",
                    "summary": "generate a new plugin (produces code only, no install)",
                },
            ),
            ToolMetadata(
                name="plugin_install",
                description=(
                    "Install a plugin either from validated code (produced by "
                    "plugin_generate) or from the configured marketplace, then "
                    "load it into the live registry. Writes to the profile-scoped "
                    "plugins directory (never the read-only package dir), "
                    "re-validates code, and runs an isolated sandbox smoke test "
                    "before the plugin is made live. REQUIRES APPROVAL — this "
                    "mutates the filesystem and the process-global plugin registry."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "Identifier of the plugin to install.",
                        },
                        "code": {
                            "type": "string",
                            "description": "Validated plugin source code (typically from plugin_generate). Mutually exclusive with marketplace_name.",
                        },
                        "marketplace_name": {
                            "type": "string",
                            "description": "Marketplace entry name to install from. Mutually exclusive with code.",
                        },
                        "proposal_id": {
                            "type": "string",
                            "description": "Optional PluginProposal id to link into approval metadata and mark approved on success.",
                        },
                        "version_label": {
                            "type": "string",
                            "description": "Optional version id to record for code installs.",
                        },
                    },
                    "required": [],
                },
                handler=self._plugin_install_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "high",
                    "schema_cost": "medium",
                    "requires_approval": True,
                    "effect_scope": "persistent",
                    "idempotency_scope": "session",
                    "summary": "install a plugin from validated code or marketplace (approval required)",
                },
                mutates_state=True,
            ),
            ToolMetadata(
                name="plugin_rollback",
                description=(
                    "Rollback a profile-scoped plugin to a recorded source version and reload it. "
                    "REQUIRES APPROVAL."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "The plugin identifier to rollback.",
                        },
                        "version": {
                            "type": "string",
                            "description": "Recorded version id to restore.",
                        },
                    },
                    "required": ["plugin_id", "version"],
                },
                handler=self._plugin_rollback_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "high",
                    "schema_cost": "medium",
                    "requires_approval": True,
                    "effect_scope": "persistent",
                    "idempotency_scope": "session",
                    "summary": "rollback a plugin to a recorded version (approval required)",
                },
                mutates_state=True,
            ),
            ToolMetadata(
                name="plugin_reload",
                description=(
                    "Hot-reload a plugin at runtime. Disposes the old plugin fiber, "
                    "re-imports its module, and registers a fresh instance. Existing "
                    "in-flight turns are unaffected (snapshot isolation). "
                    "REQUIRES APPROVAL — this is a self-modification action."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "The plugin identifier to reload.",
                        },
                        "version_label": {
                            "type": "string",
                            "description": "Optional version id to record after reload.",
                        },
                    },
                    "required": ["plugin_id"],
                },
                handler=self._plugin_reload_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "high",
                    "schema_cost": "medium",
                    "requires_approval": True,
                    "effect_scope": "local",
                    "idempotency_scope": "turn",
                    "summary": "hot-reload a plugin (approval required)",
                },
                mutates_state=True,
            ),
            ToolMetadata(
                name="plugin_disable",
                description=(
                    "Disable a plugin by disposing its fiber, removing its tools "
                    "from the runtime registry. Cannot disable self_management itself. "
                    "REQUIRES APPROVAL — this is a self-modification action."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "The plugin identifier to disable.",
                        },
                    },
                    "required": ["plugin_id"],
                },
                handler=self._plugin_disable_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "high",
                    "schema_cost": "medium",
                    "requires_approval": True,
                    "effect_scope": "local",
                    "idempotency_scope": "session",
                    "summary": "disable a plugin (approval required)",
                },
                mutates_state=True,
            ),
            ToolMetadata(
                name="plugin_remove",
                description=(
                    "Terminally remove a plugin: dispose its fiber, unregister its tools, "
                    "remove reload metadata, and optionally delete its profile-scoped source file. "
                    "Cannot remove self_management itself. REQUIRES APPROVAL."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "The plugin identifier to remove.",
                        },
                        "delete_source": {
                            "type": "boolean",
                            "description": "Delete the profile-scoped source file as part of removal (default true).",
                        },
                    },
                    "required": ["plugin_id"],
                },
                handler=self._plugin_remove_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "high",
                    "schema_cost": "medium",
                    "requires_approval": True,
                    "effect_scope": "persistent",
                    "idempotency_scope": "session",
                    "summary": "remove a plugin and optionally delete its source (approval required)",
                },
                mutates_state=True,
            ),
            ToolMetadata(
                name="plugin_enable",
                description=(
                    "Re-enable a previously disabled plugin by reloading its module "
                    "and registering a fresh instance. REQUIRES APPROVAL."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "The plugin identifier to re-enable.",
                        },
                    },
                    "required": ["plugin_id"],
                },
                handler=self._plugin_enable_handler,
                x_leapflow={
                    "category": "system",
                    "risk_level": "high",
                    "schema_cost": "medium",
                    "requires_approval": True,
                    "effect_scope": "local",
                    "idempotency_scope": "turn",
                    "summary": "re-enable a disabled plugin (approval required)",
                },
                mutates_state=True,
            ),
        ]


plugin = SelfManagementPlugin()
