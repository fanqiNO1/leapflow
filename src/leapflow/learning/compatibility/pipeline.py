"""Assessment pipeline orchestrator.

Entry point for the Plugin Compatibility Assessment Engine (P0).
Runs stages 1 (ManifestParser) and 2 (CategoryResolver) sequentially,
short-circuits on INCOMPATIBLE, and synthesizes a CompatibilityReport.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from leapflow.learning.compatibility.protocol import (
    AdapterSpec,
    CompatibilityReport,
    PluginManifestInput,
    StageResult,
    Verdict,
)
from leapflow.learning.compatibility.stages.category_resolver import CategoryResolver
from leapflow.learning.compatibility.stages.manifest_parser import ManifestParser


def assess_plugin(
    manifest: Union[dict, str, Path, PluginManifestInput],
) -> CompatibilityReport:
    """Assess a foreign plugin for LeapFlow compatibility.

    Args:
        manifest: Either a raw manifest dict (LeapFlow or DSH format),
                  a path string to a manifest file, a Path object,
                  or a pre-parsed PluginManifestInput.

    Returns:
        CompatibilityReport with final_verdict and stage results.
    """
    stages: list[StageResult] = []
    parser = ManifestParser()
    resolver = CategoryResolver()

    # ── Stage 1: Parse manifest ──────────────────────────────────────
    if isinstance(manifest, PluginManifestInput):
        parsed_manifest = manifest
        parse_result = parser.assess(parsed_manifest, [])
        if not parse_result.passed:
            return CompatibilityReport(
                manifest=parsed_manifest,
                stages=[parse_result],
                final_verdict=Verdict.INCOMPATIBLE,
                target_protocol=None,
                rejection_reason=parse_result.details,
                adaptation_notes=[],
                adapter_spec=None,
            )
    elif isinstance(manifest, dict):
        parse_result = ManifestParser.parse_raw(manifest)
        if not parse_result.passed:
            return CompatibilityReport(
                manifest=PluginManifestInput(
                    name=manifest.get("name", "<unknown>"),
                    version=manifest.get("version", "0.0.0"),
                    category="",
                    raw_manifest=manifest,
                ),
                stages=[parse_result],
                final_verdict=Verdict.INCOMPATIBLE,
                rejection_reason=parse_result.details,
            )
        parsed_manifest = parse_result.evidence["manifest"]
    elif isinstance(manifest, (str, Path)):
        # File path support — P0 only reads JSON/dict manifests
        # For P0, treat string as a path that should be a dict
        return CompatibilityReport(
            manifest=PluginManifestInput(
                name="<unknown>",
                version="0.0.0",
                category="",
                raw_manifest={},
            ),
            stages=[
                StageResult(
                    stage_name="manifest_parser",
                    passed=False,
                    details="File path manifest loading not yet implemented (P0 accepts dict or PluginManifestInput only)",
                )
            ],
            final_verdict=Verdict.INCOMPATIBLE,
            rejection_reason="File path manifest loading not yet implemented",
        )
    else:
        return CompatibilityReport(
            manifest=PluginManifestInput(
                name="<unknown>",
                version="0.0.0",
                category="",
                raw_manifest={},
            ),
            stages=[
                StageResult(
                    stage_name="manifest_parser",
                    passed=False,
                    details=f"Unsupported manifest type: {type(manifest).__name__}",
                )
            ],
            final_verdict=Verdict.INCOMPATIBLE,
            rejection_reason=f"Unsupported manifest type: {type(manifest).__name__}",
        )

    stages.append(parse_result)

    # ── Stage 2: Category resolution ────────────────────────────────
    category_result = resolver.assess(parsed_manifest, stages)
    stages.append(category_result)

    # Short-circuit on INCOMPATIBLE
    if category_result.verdict == Verdict.INCOMPATIBLE:
        return CompatibilityReport(
            manifest=parsed_manifest,
            stages=stages,
            final_verdict=Verdict.INCOMPATIBLE,
            target_protocol=None,
            rejection_reason=category_result.details,
        )

    # ── Synthesize report from stage results ─────────────────────────
    target_protocol = category_result.evidence.get("target_protocol")
    final_verdict = category_result.verdict or Verdict.COMPATIBLE

    # Build adaptation notes
    adaptation_notes: list[str] = []
    if final_verdict == Verdict.ADAPTABLE:
        adaptation_notes.append(category_result.details)

    # Generate adapter spec for ADAPTABLE verdicts
    adapter_spec: AdapterSpec | None = None
    if final_verdict == Verdict.ADAPTABLE and target_protocol:
        adapter_spec = AdapterSpec(
            source_interface=parsed_manifest.category,
            target_protocol=target_protocol,
            bridge_type="json_rpc_bridge" if parsed_manifest.source_language == "typescript" else "protocol_wrapper",
            shim_methods=[],
            estimated_complexity="low",
        )

    return CompatibilityReport(
        manifest=parsed_manifest,
        stages=stages,
        final_verdict=final_verdict,
        target_protocol=target_protocol,
        adaptation_notes=adaptation_notes,
        adapter_spec=adapter_spec,
    )
