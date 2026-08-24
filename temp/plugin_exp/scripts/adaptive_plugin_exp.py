#!/usr/bin/env python3
"""Deterministic adaptive plugin experiment.

This experiment is deliberately narrower than the old lifecycle harness. It
validates the adaptive decision chain that sits above plugin lifecycle mechanics:

    environment fingerprint -> capability requirement -> candidate resolution
    -> transparent rejection/selection evidence -> declarative orchestration plan

No LLM calls, network access, daemon process, or plugin installation occurs here.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

EXP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EXP_ROOT.parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from leapflow.analysis.environment_probe import EnvironmentProbe
from leapflow.domain.capability_requirement import CapabilityRequirement
from leapflow.domain.platform import Capability, PlatformID, PlatformManifest
from leapflow.learning.plugin_stats import PluginUsageTracker
from leapflow.learning.plugin_trust import PluginTrustLedger
from leapflow.plugins.capability_plan import CapabilityPlan
from leapflow.plugins.capability_resolver import (
    CapabilityCandidate,
    CapabilityResolver,
    ResolverContext,
)
from leapflow.storage.capability_plan_store import JsonCapabilityPlanStore


def _report_path() -> Path:
    out = EXP_ROOT / "reports"
    out.mkdir(parents=True, exist_ok=True)
    return out / f"{time.strftime('%Y%m%d-%H%M%S')}-adaptive-plugin.json"


def _manifest(*caps: Capability) -> PlatformManifest:
    return PlatformManifest(
        platform_id=PlatformID.DARWIN_15,
        os_version="15.0",
        capabilities=frozenset(caps),
    )


def _build_context() -> ResolverContext:
    workspace = EXP_ROOT / "workspaces" / "python_project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='adaptive-demo'\n", encoding="utf-8")

    env = EnvironmentProbe(workspace_markers=("pyproject.toml", "package.json")).probe(
        platform_manifest=_manifest(Capability.FILE_OPS),
        workspace_root=workspace,
    )

    trust = PluginTrustLedger(candidate_at=1, verified_at=2, production_at=3)
    for _ in range(3):
        trust.record_success("json_stable")
    trust.record_success("json_draft")

    usage = PluginUsageTracker()
    usage._get_reverse_index = lambda: {
        "json_stable_pretty": "json_stable",
        "json_draft_pretty": "json_draft",
        "shell_json_pretty": "json_shell",
        "json_read": "json_reader",
        "json_report": "json_reporter",
    }
    for _ in range(8):
        usage.record("json_stable_pretty", True, 6.0)
    for _ in range(6):
        usage.record("json_draft_pretty", True, 8.0)
    for _ in range(4):
        usage.record("json_draft_pretty", False, 8.0)
    return ResolverContext(environment=env, trust_ledger=trust, usage_tracker=usage)


def _candidates() -> tuple[CapabilityCandidate, ...]:
    return (
        CapabilityCandidate(
            plugin_id="json_draft",
            tool_name="json_draft_pretty",
            provides_capabilities=("json.pretty",),
        ),
        CapabilityCandidate(
            plugin_id="json_stable",
            tool_name="json_stable_pretty",
            provides_capabilities=("json.pretty",),
        ),
        CapabilityCandidate(
            plugin_id="json_shell",
            tool_name="shell_json_pretty",
            provides_capabilities=("json.pretty",),
            requires_platform_capabilities=("shell.exec",),
            risk_level="external",
            requires_approval=True,
            mutates_state=True,
        ),
        CapabilityCandidate(
            plugin_id="json_reader",
            tool_name="json_read",
            provides_capabilities=("json.read",),
        ),
        CapabilityCandidate(
            plugin_id="json_reporter",
            tool_name="json_report",
            provides_capabilities=("json.report",),
            requires_capabilities=("json.read",),
        ),
    )


def run() -> dict[str, object]:
    context = _build_context()
    resolver = CapabilityResolver()
    candidates = _candidates()
    pretty_req = CapabilityRequirement.create(
        "json.pretty",
        "explicit_request",
        evidence="Need to pretty print JSON with the best available plugin.",
        requirement_id="req-json-pretty",
    )
    report_req = CapabilityRequirement.create(
        "json.report",
        "task_contract",
        evidence="Need a report that depends on reading JSON first.",
        requirement_id="req-json-report",
    )

    pretty_resolution = resolver.resolve_one(pretty_req, candidates, context)
    report_resolution = resolver.resolve_one(report_req, candidates, context)
    # Add the provider that satisfies json_report's declared dependency, proving
    # the plan can order a provider selected for dependency support before the
    # final task-facing tool.
    provider_resolution = resolver.resolve_one(
        CapabilityRequirement.create(
            "json.read",
            "task_contract",
            requirement_id="req-json-read",
        ),
        candidates,
        context,
    )
    selected_scores = tuple(
        score
        for score in (
            provider_resolution.selected,
            pretty_resolution.selected,
            report_resolution.selected,
        )
        if score is not None
    )
    plan = CapabilityPlan.from_scores(selected_scores, plan_id="plan-json-adaptive")

    excluded_shell = next(
        c for c in pretty_resolution.candidates
        if c.candidate.plugin_id == "json_shell"
    )
    ok = (
        pretty_resolution.selected is not None
        and pretty_resolution.selected.candidate.plugin_id == "json_stable"
        and any("missing platform capabilities" in r for r in excluded_shell.exclusion_reasons)
        and plan.executable
        and [s.tool_name for s in plan.steps].index("json_read")
        < [s.tool_name for s in plan.steps].index("json_report")
    )

    payload = {
        "ok": ok,
        "environment": context.environment.to_dict(),
        "requirements": [pretty_req.to_dict(), report_req.to_dict()],
        "resolutions": [pretty_resolution.to_dict(), report_resolution.to_dict()],
        "plan": plan.to_dict(),
        "checks": {
            "selected_stable_plugin": pretty_resolution.selected.candidate.plugin_id
            if pretty_resolution.selected else "",
            "shell_candidate_excluded": list(excluded_shell.exclusion_reasons),
            "plan_order": [s.tool_name for s in plan.steps],
        },
    }
    store = JsonCapabilityPlanStore(EXP_ROOT / "work" / "capability_plans.json")
    payload["stored_record"] = store.add_record(
        environment=payload["environment"],
        requirements=payload["requirements"],
        resolutions=payload["resolutions"],
        plan=payload["plan"],
        source="temp_plugin_exp",
        record_id="adaptive-demo-latest",
    )
    return payload


def main() -> int:
    payload = run()
    path = _report_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": payload["ok"], "report": str(path)}, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
