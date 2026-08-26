"""Protocol and data definitions for Plugin Compatibility Assessment Engine.

Defines the core domain types used across all assessment stages.
All types are frozen dataclasses to guarantee immutability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Verdict(Enum):
    """Final compatibility classification."""

    COMPATIBLE = "compatible"  # Direct install; no modification needed
    ADAPTABLE = "adaptable"  # Needs a thin adapter/shim (auto-generatable)
    PARTIAL = "partial"  # Subset of features usable; limitations documented
    INCOMPATIBLE = "incompatible"  # Targets a system layer LeapFlow doesn't expose


class PluggabilityStatus(Enum):
    """Whether a system layer is exposed as a plugin surface."""

    PLUGGABLE = "pluggable"
    ADAPTABLE = "adaptable"
    PARTIAL = "partial"
    NOT_PLUGGABLE = "not_pluggable"


class DependencyFeasibility(Enum):
    """Whether a required dependency can be satisfied."""

    SATISFIABLE = "satisfiable"  # LeapFlow provides this service
    SHIMMABLE = "shimmable"  # Can be faked/shimmed with acceptable loss
    BLOCKING = "blocking"  # Cannot be provided; blocks installation


class SecurityRisk(Enum):
    """Risk classification for plugin permissions."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class PluginManifestInput:
    """Unified input format for assessment — normalizes DSH package.json
    and LeapFlow PluginManifest into a common structure."""

    name: str
    version: str
    category: str
    declared_interfaces: list[str] = field(default_factory=list)
    declared_dependencies: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    execution_model: str = "async"
    permissions: list[str] = field(default_factory=list)
    source_language: str = "python"
    raw_manifest: dict[str, Any] = field(default_factory=dict)
    source_format: str = "leapflow"  # "leapflow" | "dsh"


@dataclass(frozen=True)
class AdapterSpec:
    """Specification for an auto-generated adapter when verdict is ADAPTABLE."""

    source_interface: str
    target_protocol: str
    bridge_type: str  # "json_rpc_bridge" | "protocol_wrapper" | "shim_layer"
    shim_methods: list[str] = field(default_factory=list)
    estimated_complexity: str = "low"  # "low" | "medium" | "high"


@dataclass(frozen=True)
class StageResult:
    """Result produced by a single assessment stage."""

    stage_name: str
    passed: bool
    verdict: Optional[Verdict] = None
    details: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompatibilityReport:
    """Complete assessment output — the single artifact produced by the pipeline."""

    manifest: PluginManifestInput
    stages: list[StageResult] = field(default_factory=list)
    final_verdict: Verdict = Verdict.INCOMPATIBLE
    target_protocol: Optional[str] = None
    rejection_reason: Optional[str] = None
    adaptation_notes: list[str] = field(default_factory=list)
    adapter_spec: Optional[AdapterSpec] = None

    def is_installable(self) -> bool:
        """Whether this plugin can be installed (with or without adaptation)."""
        return self.final_verdict in (
            Verdict.COMPATIBLE,
            Verdict.ADAPTABLE,
            Verdict.PARTIAL,
        )
