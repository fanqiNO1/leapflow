"""Stage 4: Dependency Checker.

Checks declared_dependencies against what LeapFlow can provide.
Uses three classification sets:
  - satisfiable: LeapFlow natively provides this service
  - shimmable: Can be faked/shimmed with acceptable loss
  - blocking: Cannot be provided; blocks installation
"""

from __future__ import annotations

from typing import List

from leapflow.learning.compatibility.protocol import (
    DependencyFeasibility,
    PluginManifestInput,
    StageResult,
    Verdict,
)

# ═══════════════════════════════════════════════════════════════════════
# Known dependency classification patterns.
# Matching is case-insensitive and supports substring matching.
# ═══════════════════════════════════════════════════════════════════════

SATISFIABLE_DEPS: set[str] = {
    # Core runtime services LeapFlow provides
    "config",
    "event_bus",
    "registry",
    "approval_gate",
    "llm_provider",
    "memory_manager",
    "storage",
    "duckdb",
    "plugin_registry",
    "tool_registry",
    "signal_bus",
    "settings",
    "scheduler",
    "file_read_gate",
    "research_ledger",
    # Common npm/python packages that are runtime-satisfiable
    "node-fetch",
    "axios",
    "requests",
    "aiohttp",
    "httpx",
    "pydantic",
    "asyncio",
}

SHIMMABLE_DEPS: set[str] = {
    # Can be shimmed with thin wrappers or stubs
    "cordis",
    "cordis-context",
    "dsh-sdk",
    "dsh-config",
    "dsh-logger",
    "dsh-events",
    "dsh-metrics",
    "dsh-telemetry",
    "logger",
    "metrics",
    "telemetry",
}

BLOCKING_DEPS: set[str] = {
    # Cannot be provided — architecture-bound to DSH
    "cordis-scope",
    "dsh-scope-service",
    "dsh-session-persistence",
    "dsh-hooks-sdk",
    "dsh-agent-loop",
    "dsh-compaction",
    "dsh-identity",
    "dsh-workflow-engine",
    "cordis-lifecycle",
}


def _classify_dep(dep: str) -> DependencyFeasibility:
    """Classify a single dependency string."""
    dep_lower = dep.lower().strip()

    # Exact match first
    if dep_lower in SATISFIABLE_DEPS:
        return DependencyFeasibility.SATISFIABLE
    if dep_lower in SHIMMABLE_DEPS:
        return DependencyFeasibility.SHIMMABLE
    if dep_lower in BLOCKING_DEPS:
        return DependencyFeasibility.BLOCKING

    # Substring/prefix matching for common patterns
    for known in SATISFIABLE_DEPS:
        if known in dep_lower or dep_lower in known:
            return DependencyFeasibility.SATISFIABLE
    for known in SHIMMABLE_DEPS:
        if known in dep_lower or dep_lower in known:
            return DependencyFeasibility.SHIMMABLE
    for known in BLOCKING_DEPS:
        if known in dep_lower or dep_lower in known:
            return DependencyFeasibility.BLOCKING

    # Unknown deps default to satisfiable (benefit of the doubt for
    # external libs like npm packages or Python packages)
    return DependencyFeasibility.SATISFIABLE


class DependencyChecker:
    """Check declared dependencies for satisfiability within LeapFlow."""

    stage_name: str = "dependency_checker"

    def assess(
        self, manifest: PluginManifestInput, prior_results: List[StageResult]
    ) -> StageResult:
        """Classify each declared dependency and produce an aggregate verdict.

        - All satisfiable → COMPATIBLE (passed=True)
        - Some shimmable, none blocking → ADAPTABLE (passed=True)
        - Any blocking → INCOMPATIBLE (passed=False)
        """
        deps = manifest.declared_dependencies
        if not deps:
            return StageResult(
                stage_name=self.stage_name,
                passed=True,
                verdict=None,
                details="No dependencies declared; no conflicts",
                evidence={"dependencies": [], "classification": {}},
            )

        classification: dict[str, str] = {}
        blocking: list[str] = []
        shimmable: list[str] = []
        satisfiable: list[str] = []

        for dep in deps:
            feasibility = _classify_dep(dep)
            classification[dep] = feasibility.value
            if feasibility == DependencyFeasibility.BLOCKING:
                blocking.append(dep)
            elif feasibility == DependencyFeasibility.SHIMMABLE:
                shimmable.append(dep)
            else:
                satisfiable.append(dep)

        evidence = {
            "dependencies": deps,
            "classification": classification,
            "satisfiable": satisfiable,
            "shimmable": shimmable,
            "blocking": blocking,
        }

        if blocking:
            return StageResult(
                stage_name=self.stage_name,
                passed=False,
                verdict=Verdict.INCOMPATIBLE,
                details=(
                    f"Blocking dependencies cannot be satisfied: {blocking}. "
                    "These require DSH-specific runtime services not available in LeapFlow."
                ),
                evidence=evidence,
            )

        if shimmable:
            return StageResult(
                stage_name=self.stage_name,
                passed=True,
                verdict=Verdict.ADAPTABLE,
                details=(
                    f"Dependencies {shimmable} need shim layers; "
                    f"remaining {len(satisfiable)} are natively satisfiable"
                ),
                evidence=evidence,
            )

        return StageResult(
            stage_name=self.stage_name,
            passed=True,
            verdict=None,
            details=f"All {len(satisfiable)} dependencies are satisfiable",
            evidence=evidence,
        )
