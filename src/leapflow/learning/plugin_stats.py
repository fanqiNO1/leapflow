"""Per-plugin usage statistics accumulator.

Receives forwarded (tool_name, ok, duration_ms) from TurnUsageTracker
and maintains bounded, rolling statistics per tool. Cross-turn data survives
turn resets because PluginUsageTracker is session-scoped (or engine-scoped),
not turn-scoped.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Dict, Optional

from leapflow.learning.plugin_trust import PluginTrustLedger


@dataclass(frozen=True, slots=True)
class PluginUsageSample:
    """Single recorded tool execution sample."""

    timestamp: float
    ok: bool
    duration_ms: float


@dataclass
class PluginStats:
    """Aggregated stats for a single plugin."""

    total_calls: int
    successes: int
    failures: int
    avg_duration_ms: float
    error_rate: float
    p95_duration_ms: float


class PluginUsageTracker:
    """Cross-turn accumulator. Bounded memory via deque(maxlen=N)."""

    def __init__(self, max_samples_per_tool: int = 500) -> None:
        self._max_samples = max(1, int(max_samples_per_tool))
        self._samples: Dict[str, deque[PluginUsageSample]] = defaultdict(
            self._make_deque
        )
        self._trust_ledger: Optional[PluginTrustLedger] = None
        # Lazy reverse index: tool_name → plugin_id
        self._tool_to_plugin: Optional[Dict[str, str]] = None
        self._registry_version: int = -1

    def _make_deque(self) -> deque[PluginUsageSample]:
        return deque(maxlen=self._max_samples)

    def set_trust_ledger(self, ledger: PluginTrustLedger) -> None:
        """Inject the trust ledger for automatic trust forwarding."""
        self._trust_ledger = ledger

    def record(self, tool_name: str, ok: bool, duration_ms: float) -> None:
        """Called by TurnUsageTracker forward. Must be fast (<1μs hot path)."""
        sample = PluginUsageSample(time.time(), ok, duration_ms)
        self._samples[tool_name].append(sample)
        # Forward to trust ledger
        if self._trust_ledger is not None:
            plugin_id = self._resolve_plugin_id(tool_name)
            if plugin_id:
                if ok:
                    self._trust_ledger.record_success(plugin_id)
                else:
                    self._trust_ledger.record_failure(plugin_id)

    def stats_for_plugin(self, plugin_id: str) -> Optional[PluginStats]:
        """Aggregate stats across all tools owned by a plugin."""
        tool_names = self._tools_for_plugin(plugin_id)
        if not tool_names:
            return None

        all_samples: list[PluginUsageSample] = []
        for tool_name in tool_names:
            if tool_name in self._samples:
                all_samples.extend(self._samples[tool_name])

        if not all_samples:
            return None

        total = len(all_samples)
        successes = sum(1 for s in all_samples if s.ok)
        failures = total - successes
        durations = [s.duration_ms for s in all_samples]
        avg_duration = sum(durations) / total if total else 0.0
        error_rate = failures / total if total else 0.0

        # p95 duration
        sorted_durations = sorted(durations)
        p95_idx = min(int(total * 0.95), total - 1)
        p95_duration = sorted_durations[p95_idx] if sorted_durations else 0.0

        return PluginStats(
            total_calls=total,
            successes=successes,
            failures=failures,
            avg_duration_ms=round(avg_duration, 2),
            error_rate=round(error_rate, 4),
            p95_duration_ms=round(p95_duration, 2),
        )

    def _resolve_plugin_id(self, tool_name: str) -> Optional[str]:
        """Map tool_name → plugin_id (lazy-built reverse index)."""
        index = self._get_reverse_index()
        return index.get(tool_name)

    def _tools_for_plugin(self, plugin_id: str) -> list[str]:
        """Return tool names belonging to the given plugin."""
        index = self._get_reverse_index()
        return [name for name, pid in index.items() if pid == plugin_id]

    def _get_reverse_index(self) -> Dict[str, str]:
        """Build/cache reverse index from tool_name → plugin_id."""
        try:
            from leapflow.tools import get_registry
            reg = get_registry()
            version = getattr(reg, "_version", 0)
            if self._tool_to_plugin is not None and self._registry_version == version:
                return self._tool_to_plugin
            # Rebuild
            mapping: Dict[str, str] = {}
            for pid, plugin in reg.plugins.items():
                for tool_meta in plugin.tools:
                    mapping[tool_meta.name] = pid
            self._tool_to_plugin = mapping
            self._registry_version = version
            return mapping
        except (ImportError, RuntimeError, AttributeError):
            return self._tool_to_plugin or {}
