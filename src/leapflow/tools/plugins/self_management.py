"""Self-Management plugin — lets the Agent introspect and manage its own plugin composition.

This is the Phase 2.4 Self-Modification MVP. It exposes seven tools:

Read-only introspection (no approval needed):
    - plugin_list    : list all registered plugins across Tool/Gateway/LLM subsystems
    - plugin_status  : detailed info about one plugin (tools, deps, fiber state, generation)

Generation (no approval needed — produces validated code without installing):
    - plugin_generate: describe a capability need; the LLM produces conformant
                       plugin code and it is rigorously validated. The validated
                       code is returned; installation is a separate, gated step.

State-mutating (REQUIRES approval — routed through the plugin_approval_gate):
    - plugin_install : write validated code (from plugin_generate) or a
                       marketplace payload into the plugins package and load it.
                       This mutates the filesystem and the live registry.
    - plugin_reload  : hot-reload a plugin
    - plugin_disable : dispose a plugin's fiber (removes its tools)
    - plugin_enable  : re-enable a previously disabled plugin

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
from typing import Any, Dict, List, Optional

from leapflow.tools.protocol import ToolMetadata, ToolPlugin

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

    @property
    def plugin_id(self) -> str:
        return "self_management"

    @property
    def category(self) -> str:
        return "system"

    @property
    def dependencies(self) -> list[str]:
        return ["plugin_approval_gate", "llm_provider"]

    def bind_runtime(self, **deps: Any) -> None:
        if "plugin_approval_gate" in deps:
            self._plugin_approval_gate = deps["plugin_approval_gate"]
        if "llm_provider" in deps:
            self._llm_provider = deps["llm_provider"]

    # ── Read-only introspection ────────────────────────────

    async def _plugin_list_handler(self, **kwargs: Any) -> Dict[str, Any]:
        """List all registered plugins across Tool/Gateway/LLM subsystems."""
        from leapflow.tools import get_registry, get_scoped_registry

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
            }
        except (RuntimeError, AttributeError) as exc:
            logger.warning("plugin_list failed: %s", exc, exc_info=True)
            return {"ok": False, "error": f"plugin_list failed: {exc}"}

    async def _plugin_status_handler(self, plugin_id: str, **kwargs: Any) -> Dict[str, Any]:
        """Detailed information about a specific plugin."""
        from leapflow.tools import get_registry, get_scoped_registry

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

    async def _plugin_generate_handler(self, plugin_id: str, description: str, **kwargs: Any) -> Dict[str, Any]:
        """Generate a new plugin via LLM and validate it. Returns validated code (does NOT install).

        This is the LLM co-evolution entry point: describe a capability need,
        the LLM generates conformant plugin code, and it's rigorously validated.
        Installation is a SEPARATE approval-gated step (plugin_install).
        """
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
            return result
        except (AttributeError, RuntimeError) as exc:
            return {"ok": False, "error": f"Generation failed: {exc}"}

    # ── State-mutating (requires approval) ─────────────────

    async def _plugin_install_handler(
        self, plugin_id: str, code: str = "", marketplace_name: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """Install a plugin from validated code or marketplace, then load it. REQUIRES approval.

        Two modes:
        - code: install directly from a validated code string (from plugin_generate)
        - marketplace_name: install from the configured marketplace
        """
        approved, denial = await self._check_approval("install", plugin_id)
        if not approved:
            return {"ok": False, "error": denial, "requires_approval": True}

        try:
            from pathlib import Path
            import leapflow.tools.plugins as plugins_pkg

            plugins_dir = Path(plugins_pkg.__file__).parent
            target = plugins_dir / f"{plugin_id}.py"

            if code:
                # Re-validate before writing (defense in depth)
                from leapflow.learning.plugin_generator import PluginValidator

                validator = PluginValidator()
                vresult = await validator.validate(plugin_id, code)
                if not vresult.ok:
                    return {
                        "ok": False,
                        "error": f"Code failed re-validation at stage '{vresult.stage}': {vresult.error}",
                    }
                target.write_text(code)
            elif marketplace_name:
                return {
                    "ok": False,
                    "error": (
                        "Marketplace install requires a configured MarketplaceClient "
                        "(not wired in prototype)"
                    ),
                }
            else:
                return {"ok": False, "error": "Must provide either code or marketplace_name"}

            # Register + load the freshly-installed plugin
            import importlib
            from leapflow.tools import get_registry, get_scoped_registry

            module_path = f"leapflow.tools.plugins.{plugin_id}"
            mod = importlib.import_module(module_path)
            new_plugin = getattr(mod, "plugin", None)
            if new_plugin is None:
                return {"ok": False, "error": f"Installed module has no 'plugin' attribute"}

            reg = get_registry()
            scoped = get_scoped_registry()
            fiber = scoped.create_fiber(plugin_id)
            scoped.scoped_register(new_plugin, fiber)
            fiber.activate()

            # Add tools to the assembled registry
            for tool in new_plugin.tools:
                reg._tool_handlers[tool.name] = tool.handler
                reg._tool_handlers[f"gp_{tool.name}"] = tool.handler
                reg._tool_definitions.append(tool.to_openai_schema())
                reg._all_metadata.append(tool)
            reg.notify_mutation()

            return {
                "ok": True,
                "action": "install",
                "plugin_id": plugin_id,
                "installed_tools": [t.name for t in new_plugin.tools],
                "state": fiber.state.value,
            }
        except (ImportError, AttributeError, OSError, RuntimeError) as exc:
            return {"ok": False, "error": f"Install failed: {exc}"}

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
            from leapflow.tools import reload_plugin
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

    async def _check_approval(self, action: str, plugin_id: str) -> tuple[bool, str]:
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

    async def _plugin_reload_handler(self, plugin_id: str, **kwargs: Any) -> Dict[str, Any]:
        """Hot-reload a plugin. REQUIRES approval."""
        approved, denial = await self._check_approval("reload", plugin_id)
        if not approved:
            return {"ok": False, "error": denial, "requires_approval": True}

        try:
            from leapflow.tools import reload_plugin
            new_fiber = reload_plugin(plugin_id)
            return {
                "ok": True,
                "action": "reload",
                "plugin_id": plugin_id,
                "new_generation": new_fiber.generation,
                "state": new_fiber.state.value,
            }
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
            from leapflow.tools import get_scoped_registry
            scoped = get_scoped_registry()
            fiber = scoped.get_fiber(plugin_id)
            if fiber is None:
                return {"ok": False, "error": f"Plugin '{plugin_id}' has no fiber"}
            if fiber.state.value == "disposed":
                return {"ok": False, "error": f"Plugin '{plugin_id}' already disposed"}

            from leapflow.domain.plugin_fiber import FiberState
            if fiber.state == FiberState.ACTIVE:
                fiber.begin_unload()
            fiber.dispose()

            return {
                "ok": True,
                "action": "disable",
                "plugin_id": plugin_id,
                "state": fiber.state.value,
            }
        except (RuntimeError, AttributeError) as exc:
            logger.warning("plugin_disable failed: %s", exc, exc_info=True)
            return {"ok": False, "error": f"Disable failed: {exc}"}

    # ── Tool metadata ──────────────────────────────────────

    @property
    def tools(self) -> list[ToolMetadata]:
        return [
            ToolMetadata(
                name="plugin_list",
                description=(
                    "List all registered plugins in the tool subsystem with their "
                    "category, tool count, fiber state, and generation. Useful for "
                    "the Agent to observe its own composition."
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
                    "summary": "list all registered plugins",
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
                name="plugin_generate",
                description=(
                    "Generate a new ToolPlugin from a natural-language capability "
                    "description. The LLM produces code that conforms to the "
                    "ToolPlugin Protocol; it is then rigorously validated "
                    "(syntax, structure, import, protocol conformance, sandbox). "
                    "Returns the validated code but DOES NOT install it — "
                    "installation is a separate approval-gated step via plugin_install."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "plugin_id": {
                            "type": "string",
                            "description": "Identifier for the new plugin (module name under leapflow.tools.plugins).",
                        },
                        "description": {
                            "type": "string",
                            "description": "Natural-language description of the capability the plugin should provide.",
                        },
                    },
                    "required": ["plugin_id", "description"],
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
                    "load it into the live registry. Writes to the plugins "
                    "package directory and re-validates code before persisting. "
                    "REQUIRES APPROVAL — this mutates the filesystem and the "
                    "process-global plugin registry."
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
                    },
                    "required": ["plugin_id"],
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
