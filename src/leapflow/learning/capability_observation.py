"""Structured capability observations for adaptive plugin evolution.

The observation layer is intentionally side-effect free. It accepts structured
runtime evidence (currently unknown-tool results) and turns it into capability
requirements that a separate governance loop may review, plan, and mutate from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from leapflow.domain.capability_requirement import CapabilityRequirement
from leapflow.domain.environment_fingerprint import EnvironmentFingerprint
from leapflow.learning.capability_gap_detector import CapabilityGapDetector


@dataclass(frozen=True)
class CapabilityObservation:
    """One structured runtime signal relevant to plugin adaptation."""

    observed_at: float
    result: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"observed_at": self.observed_at, "result": dict(self.result)}


@dataclass
class CapabilityObservationBuffer:
    """Collect structured tool evidence and derive reviewable requirements."""

    detector: CapabilityGapDetector = field(default_factory=CapabilityGapDetector)
    _observations: list[CapabilityObservation] = field(default_factory=list)

    def add_result(self, result: Mapping[str, Any] | None) -> bool:
        """Record a structured tool result when it represents a capability gap."""
        if not self._is_supported_signal(result):
            return False
        self._observations.append(
            CapabilityObservation(observed_at=time.time(), result=dict(result or {}))
        )
        return True

    def extend_results(self, results: Sequence[Mapping[str, Any]]) -> int:
        """Record multiple tool results and return how many were accepted."""
        return sum(1 for result in results if self.add_result(result))

    def requirements(self, *, min_count: int = 1) -> tuple[CapabilityRequirement, ...]:
        """Return requirements derived from buffered structured evidence."""
        return self.detector.requirements_from_tool_results(
            tuple(observation.result for observation in self._observations),
            min_count=min_count,
        )

    def observations(self) -> tuple[CapabilityObservation, ...]:
        """Return an immutable snapshot of collected observations."""
        return tuple(self._observations)

    def clear(self) -> None:
        """Drop all buffered observations."""
        self._observations.clear()

    @staticmethod
    def _is_supported_signal(result: Mapping[str, Any] | None) -> bool:
        return isinstance(result, Mapping) and result.get("error_type") == "unknown_tool"


class CapabilityObservationService:
    """Bridge turn-local observations into durable, cross-turn requirements."""

    def __init__(self, store: Any, *, detector: CapabilityGapDetector | None = None) -> None:
        self._store = store
        self._detector = detector or CapabilityGapDetector()

    def observe_result(
        self,
        result: Mapping[str, Any] | None,
        *,
        environment: EnvironmentFingerprint | Mapping[str, Any] | None = None,
        source: str = "runtime",
        session_id: str = "",
        turn_id: str = "",
        workspace_root: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Persist one structured observation, returning the stored record."""
        if not CapabilityObservationBuffer._is_supported_signal(result):
            return None
        env_payload = (
            environment.to_dict()
            if isinstance(environment, EnvironmentFingerprint)
            else dict(environment or {})
        )
        return self._store.add_observation(
            result=dict(result or {}),
            environment=env_payload,
            source=source,
            session_id=session_id,
            turn_id=turn_id,
            workspace_root=workspace_root,
            metadata=dict(metadata or {}),
        )

    def flush_buffer(
        self,
        buffer: CapabilityObservationBuffer,
        *,
        environment: EnvironmentFingerprint | Mapping[str, Any] | None = None,
        source: str = "runtime",
        session_id: str = "",
        turn_id: str = "",
        workspace_root: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Persist every observation in a turn-local buffer."""
        records: list[dict[str, Any]] = []
        for observation in buffer.observations():
            record = self.observe_result(
                observation.result,
                environment=environment,
                source=source,
                session_id=session_id,
                turn_id=turn_id,
                workspace_root=workspace_root,
                metadata=metadata,
            )
            if record is not None:
                records.append(record)
        return tuple(records)

    def requirements(
        self, *, min_count: int = 1, limit: int = 50
    ) -> tuple[CapabilityRequirement, ...]:
        """Aggregate durable observations into reviewable requirements."""
        results = [
            record.get("result") or {}
            for record in self._store.unresolved(min_count=min_count, limit=limit)
        ]
        return self._detector.requirements_from_tool_results(results, min_count=1)


__all__ = [
    "CapabilityObservation",
    "CapabilityObservationBuffer",
    "CapabilityObservationService",
]
