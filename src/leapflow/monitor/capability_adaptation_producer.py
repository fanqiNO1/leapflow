"""Monitor producer for adaptive capability decision visibility."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from leapflow.monitor.types import Evidence, Finding, ProducerContext, Severity, SuggestedAction

logger = logging.getLogger(__name__)


class CapabilityAdaptationProducer:
    """Emit findings from stored capability resolution / plan records."""

    domain = "capability_adaptation"

    async def observe(self, ctx: ProducerContext) -> Sequence[Finding]:
        store = self._resolve_store(ctx)
        if store is None:
            return ()
        latest = store.latest()
        if not latest:
            return ()
        plan = latest.get("plan") or {}
        executable = bool(plan.get("executable"))
        severity = Severity.INFO if executable else Severity.NOTABLE
        selected = self._selected_tools(latest)
        missing = plan.get("missing_dependencies") or []
        evidence = [
            Evidence(kind="record", label="record_id", value=str(latest.get("record_id") or "")),
            Evidence(kind="metric", label="selected_tools", value=", ".join(selected) or "-"),
        ]
        if missing:
            evidence.append(Evidence(kind="metric", label="missing_dependencies", value=str(len(missing))))
        return (
            Finding(
                watch_id=ctx.spec.watch_id or self.domain,
                domain=self.domain,
                title="Adaptive plugin capability decision recorded",
                summary=(
                    "Latest capability plan is executable."
                    if executable else
                    "Latest capability plan has unresolved dependencies."
                ),
                severity=severity,
                tags=("capability_adaptation", "plugin_plan"),
                evidence=tuple(evidence),
                suggested_actions=(
                    SuggestedAction(
                        name="plugin_plan",
                        label="Inspect plugin plan",
                        kind="intent",
                        params={"latest": True},
                    ),
                ),
                dedup_key=f"capability_plan:{latest.get('record_id') or plan.get('plan_id') or 'latest'}",
            ),
        )

    def _resolve_store(self, ctx: ProducerContext):
        services = getattr(ctx, "services", None)
        store = getattr(services, "capability_plan_store", None) if services is not None else None
        if store is not None:
            return store
        try:
            from leapflow.config import get_settings
            from leapflow.storage.capability_plan_store import JsonCapabilityPlanStore

            profile_layout = getattr(get_settings(), "profile_layout", None)
            if profile_layout is None:
                return None
            return JsonCapabilityPlanStore(Path(profile_layout.capability_plans_path))
        except (ImportError, RuntimeError, AttributeError, OSError) as exc:
            logger.debug("capability adaptation store unavailable: %s", exc, exc_info=True)
            return None

    @staticmethod
    def _selected_tools(record: dict) -> tuple[str, ...]:
        tools: list[str] = []
        for resolution in record.get("resolutions") or []:
            selected = resolution.get("selected") or {}
            candidate = selected.get("candidate") or {}
            tool_name = str(candidate.get("tool_name") or "")
            if tool_name:
                tools.append(tool_name)
        return tuple(tools)


__all__ = ["CapabilityAdaptationProducer"]
