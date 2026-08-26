"""Capability gap detection for plugin self-evolution.

The detector is intentionally side-effect free: it only turns structured runtime
evidence into a reviewable PluginProposal. Generation, approval, and install
remain separate steps owned by plugin governance.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from leapflow.domain.capability_requirement import CapabilityRequirement
from leapflow.domain.plugin_proposal import GapEvidence, PluginProposal, ProposedToolSpec, RiskLevel

_SAFE_IDENTIFIER = re.compile(r"[^a-z0-9_]+")


def _slug(value: str, *, fallback: str) -> str:
    text = str(value or "").strip().lower().replace("-", " ").replace(".", " ")
    text = _SAFE_IDENTIFIER.sub("_", text).strip("_")
    while "__" in text:
        text = text.replace("__", "_")
    return text or fallback


class CapabilityGapDetector:
    """Build plugin proposals from structured missing-capability evidence."""

    def proposal_from_unknown_tool(
        self,
        result: Mapping[str, Any],
        *,
        requested_capability: str = "",
    ) -> PluginProposal | None:
        """Create a proposal from ToolRegistry.unknown_result() payloads."""
        if result.get("error_type") != "unknown_tool":
            return None
        missing = str(result.get("original_tool_name") or "unknown_tool")
        summary = requested_capability.strip() or f"Provide the missing tool '{missing}'."
        tool_name = _slug(missing, fallback="generated_tool")
        plugin_id = _slug(f"{tool_name}_plugin", fallback="generated_tool_plugin")
        evidence = GapEvidence.create(
            "unknown_tool",
            f"Runtime attempted unknown tool '{missing}'.",
            confidence=0.82,
            metadata={
                "original_tool_name": missing,
                "suggestions": ",".join(str(item) for item in result.get("suggestions", [])[:5]),
                "recovery_hint": str(result.get("recovery_hint") or ""),
            },
        )
        proposed_tool = ProposedToolSpec(
            name=tool_name,
            description=summary,
            risk_level="read_only",
            mutates_state=False,
        )
        return PluginProposal.create(
            plugin_id=plugin_id,
            capability_summary=summary,
            gap_type="tool_plugin",
            risk_level="read_only",
            evidence=(evidence,),
            proposed_tools=(proposed_tool,),
        )

    def proposal_from_capability_request(
        self,
        requested_capability: str,
        *,
        plugin_id: str = "",
        proposed_tool_names: Sequence[str] = (),
        risk_level: RiskLevel = "read_only",
        evidence_summary: str = "",
    ) -> PluginProposal:
        """Create a proposal from an explicit user/operator capability request.

        This method does not classify free-form intent. The caller supplies the
        request as evidence, making it suitable for self-management tools and
        UI-driven review flows.
        """
        capability = str(requested_capability or "").strip()
        if not capability:
            raise ValueError("requested_capability is required")
        pid = _slug(plugin_id or f"{capability[:48]}_plugin", fallback="generated_plugin")
        names = tuple(proposed_tool_names) or (_slug(capability[:48], fallback="generated_tool"),)
        evidence = GapEvidence.create(
            "explicit_capability_request",
            evidence_summary or capability,
            confidence=0.9,
            metadata={"requested_capability": capability},
        )
        tools = tuple(
            ProposedToolSpec(
                name=_slug(name, fallback="generated_tool"),
                description=f"Implement capability: {capability}",
                risk_level=risk_level,
                mutates_state=risk_level in {"high", "mutating", "external"},
            )
            for name in names
        )
        return PluginProposal.create(
            plugin_id=pid,
            capability_summary=capability,
            gap_type="tool_plugin",
            risk_level=risk_level,
            evidence=(evidence,),
            proposed_tools=tools,
        )

    def requirements_from_tool_results(
        self,
        results: Sequence[Mapping[str, Any]],
        *,
        min_count: int = 1,
    ) -> tuple[CapabilityRequirement, ...]:
        """Aggregate unknown-tool evidence into reviewable capability needs.

        This is the observation-only bridge from failed tool calls to adaptive
        resolution. It creates no code, performs no install, and does not infer
        capability names from user text; it only reflects the structured
        ``original_tool_name`` emitted by the tool registry.
        """
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for result in results:
            if result.get("error_type") != "unknown_tool":
                continue
            key = str(result.get("original_tool_name") or "unknown_tool")
            buckets.setdefault(key, []).append(result)

        requirements: list[CapabilityRequirement] = []
        for key, bucket in sorted(buckets.items()):
            if len(bucket) < min_count:
                continue
            latest = bucket[-1]
            requirements.append(
                CapabilityRequirement.create(
                    _slug(key, fallback="generated_tool"),
                    "unknown_tool",
                    evidence=f"Runtime attempted unknown tool '{key}'.",
                    metadata={
                        "original_tool_name": key,
                        "occurrences": len(bucket),
                        "suggestions": ",".join(
                            str(item) for item in latest.get("suggestions", [])[:5]
                        ),
                        "recovery_hint": str(latest.get("recovery_hint") or ""),
                    },
                    requirement_id=f"req-unknown-tool-{_slug(key, fallback='generated_tool')}",
                )
            )
        return tuple(requirements)

    def proposals_from_tool_results(
        self,
        results: Sequence[Mapping[str, Any]],
        *,
        min_count: int = 1,
    ) -> tuple[PluginProposal, ...]:
        """Aggregate unknown-tool results into proposals by original tool name."""
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for result in results:
            if result.get("error_type") != "unknown_tool":
                continue
            key = str(result.get("original_tool_name") or "unknown_tool")
            buckets.setdefault(key, []).append(result)

        proposals: list[PluginProposal] = []
        for key, bucket in sorted(buckets.items()):
            if len(bucket) < min_count:
                continue
            proposal = self.proposal_from_unknown_tool(
                bucket[-1],
                requested_capability=f"Provide a tool compatible with repeated missing call '{key}'.",
            )
            if proposal is not None:
                proposals.append(proposal)
        return tuple(proposals)
