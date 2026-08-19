"""Tests for durable plugin trust persistence (Fix D2).

Covers the DuckDB-backed ``PluginStatsStore`` round-trip, the
``_PersistingTrustLedger`` save-on-transition behavior, graceful degradation
when the store is unavailable or corrupt, and the process-global wiring in
``_wire_plugin_stats_sink`` / ``persist_plugin_trust_state``.

Hermetic: no network, no LLM, DuckDB writes confined to ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from leapflow.engine.session_factory import (
    _PersistingTrustLedger,
    _default_stats_db_path,
    _load_or_new_trust_ledger,
    _resolve_stats_store,
    _wire_plugin_stats_sink,
    persist_plugin_trust_state,
)
from leapflow.engine.turn_usage import TurnUsageTracker
from leapflow.learning.plugin_stats_store import PluginStatsStore
from leapflow.learning.plugin_trust import PluginTrustLedger, PluginTrustLevel


def _db(tmp_path: Path) -> Path:
    return tmp_path / "plugin_stats.duckdb"


class TestStoreRoundTrip:
    """Raw save/load contract of PluginStatsStore + PluginTrustLedger."""

    def test_save_new_ledger_load_round_trips_trust_levels(self, tmp_path: Path) -> None:
        """save → fresh store+ledger → load restores the exact trust levels."""
        store = PluginStatsStore(_db(tmp_path))
        ledger = PluginTrustLedger(candidate_at=2, verified_at=4, production_at=6)
        # Drive one plugin to VERIFIED and another to CANDIDATE.
        for _ in range(4):
            ledger.record_success("alpha")
        for _ in range(2):
            ledger.record_success("beta")
        assert ledger.level("alpha") is PluginTrustLevel.VERIFIED
        assert ledger.level("beta") is PluginTrustLevel.CANDIDATE

        assert store.save_trust_state(ledger.to_state()) is True

        # A brand-new store instance over the same file, and a brand-new ledger.
        reopened = PluginStatsStore(_db(tmp_path))
        state = reopened.load_trust_state()
        assert state is not None
        restored = PluginTrustLedger.load_state(state)

        assert restored.level("alpha") is PluginTrustLevel.VERIFIED
        assert restored.level("beta") is PluginTrustLevel.CANDIDATE

    def test_hard_failure_freeze_survives_round_trip(self, tmp_path: Path) -> None:
        """A frozen (hard-failed) plugin stays frozen after reload."""
        store = PluginStatsStore(_db(tmp_path))
        ledger = PluginTrustLedger(candidate_at=2)
        ledger.record_success("gamma")
        ledger.record_success("gamma")
        ledger.record_failure("gamma", hard=True)
        assert ledger.level("gamma") is PluginTrustLevel.DRAFT

        store.save_trust_state(ledger.to_state())
        restored = PluginTrustLedger.load_state(PluginStatsStore(_db(tmp_path)).load_trust_state())
        # Frozen plugins cannot re-accrue trust.
        for _ in range(5):
            restored.record_success("gamma")
        assert restored.level("gamma") is PluginTrustLevel.DRAFT


class TestGracefulDegradation:
    """Missing / unavailable / corrupt store must never crash callers."""

    def test_no_path_store_is_noop(self) -> None:
        """A store with no db_path returns falsy save / None load."""
        store = PluginStatsStore(None)
        assert store.save_trust_state({"levels": {}}) is False
        assert store.load_trust_state() is None

    def test_missing_state_loads_as_none(self, tmp_path: Path) -> None:
        """An initialized-but-empty store reports no state, not an error."""
        store = PluginStatsStore(_db(tmp_path))
        assert store.load_trust_state() is None

    def test_corrupt_state_degrades_to_fresh_ledger(self, tmp_path: Path) -> None:
        """Invalid JSON in the store is ignored; loader yields a DRAFT ledger."""
        db_path = _db(tmp_path)
        # Write a syntactically invalid state_json directly into the table.
        from leapflow.storage.duckdb_connect import connect

        conn = connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS plugin_trust_state (
                    key TEXT PRIMARY KEY DEFAULT 'singleton',
                    state_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO plugin_trust_state (key, state_json) "
                "VALUES ('singleton', ?)",
                ["{not valid json"],
            )
        finally:
            conn.close()

        store = PluginStatsStore(db_path)
        # load_trust_state swallows JSONDecodeError and returns None.
        assert store.load_trust_state() is None
        # The higher-level loader then produces a usable, empty ledger.
        ledger = _load_or_new_trust_ledger(store)
        assert isinstance(ledger, _PersistingTrustLedger)
        assert ledger.level("anything") is PluginTrustLevel.DRAFT

    def test_persisting_ledger_without_store_never_raises(self) -> None:
        """Transitions on a store-less persisting ledger are safe no-ops."""
        ledger = _PersistingTrustLedger(candidate_at=1, store=None)
        ledger.record_success("x")  # would trigger a flush if a store existed
        assert ledger.level("x") is PluginTrustLevel.CANDIDATE


class TestPersistingLedgerFlush:
    """_PersistingTrustLedger auto-flushes on level transitions only."""

    def test_flush_on_promotion_transition(self, tmp_path: Path) -> None:
        """Reaching a new trust level writes through to the store immediately."""
        store = PluginStatsStore(_db(tmp_path))
        ledger = _PersistingTrustLedger(candidate_at=2, store=store)
        ledger.record_success("p")  # streak 1 — no transition, no write yet
        assert PluginStatsStore(_db(tmp_path)).load_trust_state() is None
        ledger.record_success("p")  # streak 2 — promote to CANDIDATE → flush

        state = PluginStatsStore(_db(tmp_path)).load_trust_state()
        assert state is not None
        restored = PluginTrustLedger.load_state(state)
        assert restored.level("p") is PluginTrustLevel.CANDIDATE

    def test_flush_on_hard_freeze(self, tmp_path: Path) -> None:
        """A hard failure flushes even though the reported level stays DRAFT."""
        store = PluginStatsStore(_db(tmp_path))
        ledger = _PersistingTrustLedger(store=store)
        ledger.record_failure("q", hard=True)
        state = PluginStatsStore(_db(tmp_path)).load_trust_state()
        assert state is not None
        assert "q" in state.get("frozen", [])

    def test_load_state_returns_persisting_subclass(self, tmp_path: Path) -> None:
        """classmethod load_state on the subclass yields the subclass type."""
        store = PluginStatsStore(_db(tmp_path))
        seed = _PersistingTrustLedger(candidate_at=1, store=store)
        seed.record_success("r")
        restored = _load_or_new_trust_ledger(store)
        assert isinstance(restored, _PersistingTrustLedger)
        assert restored.level("r") is PluginTrustLevel.CANDIDATE


class TestPathDerivation:
    """Profile-scoped path derivation uses existing layout APIs."""

    def test_resolve_store_honors_explicit_path(self, tmp_path: Path) -> None:
        store = _resolve_stats_store(_db(tmp_path))
        assert isinstance(store, PluginStatsStore)

    def test_default_path_is_plugin_stats_beside_profile_dbs(self) -> None:
        """When derivable, the default path is plugin_stats.duckdb in db_dir."""
        path = _default_stats_db_path()
        if path is None:
            pytest.skip("No profile layout reachable in this environment")
        assert path.name == "plugin_stats.duckdb"
        # Sits in the same directory family as the other profile DuckDB stores.
        assert path.parent.name == "db"


class TestSinkWiringPersistence:
    """End-to-end: wiring restores state and persist_plugin_trust_state writes."""

    @pytest.fixture
    def reset_singletons(self, tmp_path: Path):
        """Isolate the process-global advisor + store around each test."""
        import leapflow.engine.session_factory as sf
        from leapflow.learning import plugin_advisor as pa

        saved_advisor = pa._default_advisor
        saved_store = sf._DEFAULT_STATS_STORE
        pa._default_advisor = None
        sf._DEFAULT_STATS_STORE = None
        try:
            yield sf, pa
        finally:
            pa._default_advisor = saved_advisor
            sf._DEFAULT_STATS_STORE = saved_store

    def test_wire_persist_reload_round_trip(self, tmp_path: Path, reset_singletons) -> None:
        """Wire a sink with an explicit db, earn trust, persist, reload it back."""
        sf, pa = reset_singletons
        db_path = _db(tmp_path)

        tracker = TurnUsageTracker()
        sf._wire_plugin_stats_sink(tracker, db_path=db_path)

        advisor = pa.get_default_advisor()
        assert advisor is not None
        ledger = advisor._trust_ledger
        # Earn trust directly on the wired ledger, then flush via the public API.
        for _ in range(60):
            ledger.record_success("wired_plugin")
        assert ledger.level("wired_plugin") is PluginTrustLevel.PRODUCTION

        assert persist_plugin_trust_state() is True

        # A fresh store instance over the same file sees the persisted state.
        state = PluginStatsStore(db_path).load_trust_state()
        assert state is not None
        restored = PluginTrustLedger.load_state(state)
        assert restored.level("wired_plugin") is PluginTrustLevel.PRODUCTION

    def test_second_wire_reuses_existing_advisor(self, tmp_path: Path, reset_singletons) -> None:
        """A subsequent wiring reuses the singleton and just attaches the sink."""
        sf, pa = reset_singletons
        first = TurnUsageTracker()
        sf._wire_plugin_stats_sink(first, db_path=_db(tmp_path))
        advisor_before = pa.get_default_advisor()

        second = TurnUsageTracker()
        sf._wire_plugin_stats_sink(second, db_path=_db(tmp_path))
        assert pa.get_default_advisor() is advisor_before

    def test_persist_without_store_returns_false(self, reset_singletons) -> None:
        """With no wired store, persist is a safe no-op returning False."""
        sf, pa = reset_singletons
        assert sf._DEFAULT_STATS_STORE is None
        assert persist_plugin_trust_state() is False
