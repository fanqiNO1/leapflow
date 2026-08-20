"""Assessment pipeline orchestrator.

Entry point for the Plugin Compatibility Assessment Engine.
Runs stages 1-6 sequentially, short-circuits on INCOMPATIBLE,
and synthesizes a final CompatibilityReport via the verdict module.

Stages:
  1. ManifestParser — parse and normalize raw manifest
  2. CategoryResolver — look up category in pluggability taxonomy
  3. InterfaceAnalyzer — check declared interfaces against protocol requirements
  4. DependencyChecker — classify dependencies for satisfiability
  5. ExecutionModelAnalyzer — check execution model and language compatibility
  6. SecurityClassifier — assess permissions and recommend isolation
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from leapflow.learning.compatibility.protocol import (
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

    # ── Stage 0: Normalize file-path inputs into a raw manifest dict ──
    # A Path object or a path-like string is read from disk as JSON,
    # then flows through the standard dict path below. A string that is
    # not path-like is an unsupported input format.
    if isinstance(manifest, (str, Path)):
        loaded = _load_manifest_from_path(manifest)
        if isinstance(loaded, CompatibilityReport):
            return loaded
        manifest = loaded

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

    # ── Stages 3-6: Deep analysis ───────────────────────────────────
    # Lazy imports to avoid circular deps and keep module importable standalone
    from leapflow.learning.compatibility.stages.dependency_checker import (
        DependencyChecker,
    )
    from leapflow.learning.compatibility.stages.execution_model import (
        ExecutionModelAnalyzer,
    )
    from leapflow.learning.compatibility.stages.interface_analyzer import (
        InterfaceAnalyzer,
    )
    from leapflow.learning.compatibility.stages.security_classifier import (
        SecurityClassifier,
    )
    from leapflow.learning.compatibility.verdict import synthesize_verdict

    # Stage 3: Interface analysis
    interface_result = InterfaceAnalyzer().assess(parsed_manifest, stages)
    stages.append(interface_result)
    if interface_result.verdict == Verdict.INCOMPATIBLE:
        return CompatibilityReport(
            manifest=parsed_manifest,
            stages=stages,
            final_verdict=Verdict.INCOMPATIBLE,
            target_protocol=category_result.evidence.get("target_protocol"),
            rejection_reason=interface_result.details,
        )

    # Stage 4: Dependency check
    dep_result = DependencyChecker().assess(parsed_manifest, stages)
    stages.append(dep_result)
    if dep_result.verdict == Verdict.INCOMPATIBLE:
        return CompatibilityReport(
            manifest=parsed_manifest,
            stages=stages,
            final_verdict=Verdict.INCOMPATIBLE,
            target_protocol=category_result.evidence.get("target_protocol"),
            rejection_reason=dep_result.details,
        )

    # Stage 5: Execution model analysis
    exec_result = ExecutionModelAnalyzer().assess(parsed_manifest, stages)
    stages.append(exec_result)
    # Execution model never produces INCOMPATIBLE, but defensive check
    if exec_result.verdict == Verdict.INCOMPATIBLE:
        return CompatibilityReport(
            manifest=parsed_manifest,
            stages=stages,
            final_verdict=Verdict.INCOMPATIBLE,
            target_protocol=category_result.evidence.get("target_protocol"),
            rejection_reason=exec_result.details,
        )

    # Stage 6: Security classification
    security_result = SecurityClassifier().assess(parsed_manifest, stages)
    stages.append(security_result)
    if security_result.verdict == Verdict.INCOMPATIBLE:
        return CompatibilityReport(
            manifest=parsed_manifest,
            stages=stages,
            final_verdict=Verdict.INCOMPATIBLE,
            target_protocol=category_result.evidence.get("target_protocol"),
            rejection_reason=security_result.details,
        )

    # ── Final verdict synthesis ──────────────────────────────────────
    return synthesize_verdict(parsed_manifest, stages)


def _incompatible_report(reason: str) -> CompatibilityReport:
    """Build an INCOMPATIBLE report for a manifest-loading failure.

    Used before Stage 1 when a file-path input cannot be resolved into a
    usable manifest dict (bad format, missing file, invalid JSON).
    """
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
                details=reason,
            )
        ],
        final_verdict=Verdict.INCOMPATIBLE,
        rejection_reason=reason,
    )


def _load_manifest_from_path(
    source: Union[str, Path],
) -> Union[dict, CompatibilityReport]:
    """Resolve a file-path input into a raw manifest dict.

    Args:
        source: A ``Path`` object, or a path-like string (ends with ``.json``
                or starts with ``/`` or ``./``). Non-path-like strings are
                treated as an unsupported input format.

    Returns:
        The parsed manifest dict on success, or an INCOMPATIBLE
        CompatibilityReport describing why the file could not be loaded.
    """
    if isinstance(source, str):
        looks_like_path = (
            source.endswith(".json")
            or source.startswith("/")
            or source.startswith("./")
        )
        if not looks_like_path:
            preview = source if len(source) <= 64 else source[:64] + "..."
            return _incompatible_report(
                f"unsupported manifest format: expected a dict, a "
                f"PluginManifestInput, or a path-like string, got string "
                f"'{preview}'"
            )
        path = Path(source)
    else:
        path = source

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _incompatible_report(f"Manifest file not found: {path}")
    except (OSError, UnicodeDecodeError) as exc:
        return _incompatible_report(
            f"Failed to read manifest file {path}: {exc}"
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return _incompatible_report(
            f"Invalid JSON in manifest file {path}: {exc}"
        )

    if not isinstance(data, dict):
        return _incompatible_report(
            f"Manifest file {path} must contain a JSON object, got "
            f"{type(data).__name__}"
        )

    return data
