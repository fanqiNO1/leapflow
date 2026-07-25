"""Daemon isolation tests — memory session scoping and approval queue hygiene.

Validates Phase 0.1/0.2 fixes:
- EpisodicMemoryProvider & SemanticMemoryProvider honour session_scope on search
- MemoryManager.prefetch() transparently passes session_scope
- _deny_pending_for_request() cleans orphaned approvals on turn end
- _prune_stale_approvals() enforces TTL-based cleanup
- No accumulation across multiple turns
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from leapflow.memory import (
    EpisodicMemoryProvider,
    MemoryEntry,
    MemoryKind,
    MemoryManager,
    MemoryQuery,
    SemanticMemoryProvider,
    SignalDomain,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_entry(content: str, *, kind: MemoryKind = MemoryKind.OBSERVATION) -> MemoryEntry:
    """Create a fresh MemoryEntry with deterministic content."""
    return MemoryEntry(kind=kind, domain=SignalDomain.SYSTEM, content=content)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Memory Session Isolation
# ═══════════════════════════════════════════════════════════════════════════


class TestEpisodicMemorySessionIsolation:
    """EpisodicMemoryProvider respects session_scope on search."""

    @pytest.mark.asyncio
    async def test_episodic_memory_session_isolation(self) -> None:
        """Entries from session A must not appear when searching with session B scope."""
        provider = EpisodicMemoryProvider(ttl=600.0)
        await provider.initialize()

        # Insert entries for two different sessions
        await provider.insert(_make_entry("alpha secret"), session_id="sess-a")
        await provider.insert(_make_entry("beta secret"), session_id="sess-b")

        # Search scoped to session A
        results_a = await provider.search(
            MemoryQuery(keywords=["secret"], session_scope="sess-a")
        )
        assert len(results_a) == 1
        assert "alpha" in results_a[0].content

        # Search scoped to session B
        results_b = await provider.search(
            MemoryQuery(keywords=["secret"], session_scope="sess-b")
        )
        assert len(results_b) == 1
        assert "beta" in results_b[0].content

        await provider.shutdown()


class TestSemanticMemorySessionIsolation:
    """SemanticMemoryProvider respects session_scope on search (DuckDB :memory:)."""

    @pytest.mark.asyncio
    async def test_semantic_memory_session_isolation(self, tmp_path: Path) -> None:
        """Entries from session A must not appear when searching with session B scope."""
        db_path = tmp_path / "isolation_test.duckdb"
        provider = SemanticMemoryProvider(source=db_path)
        await provider.initialize()

        await provider.insert(_make_entry("gamma data"), session_id="sess-x")
        await provider.insert(_make_entry("delta data"), session_id="sess-y")

        results_x = await provider.search(
            MemoryQuery(keywords=["data"], session_scope="sess-x")
        )
        assert len(results_x) == 1
        assert "gamma" in results_x[0].content

        results_y = await provider.search(
            MemoryQuery(keywords=["data"], session_scope="sess-y")
        )
        assert len(results_y) == 1
        assert "delta" in results_y[0].content

        await provider.shutdown()


class TestMemoryManagerScopePassthrough:
    """MemoryManager.prefetch() correctly passes session_scope down to providers."""

    @pytest.mark.asyncio
    async def test_memory_manager_scope_passthrough(self) -> None:
        """prefetch with session_scope only returns entries from that session."""
        manager = MemoryManager()
        episodic = EpisodicMemoryProvider(ttl=600.0)
        manager.add_provider(episodic)
        await manager.initialize_all()

        await manager.insert(_make_entry("memo alpha"), session_id="s1")
        await manager.insert(_make_entry("memo beta"), session_id="s2")

        results = await manager.prefetch("memo", session_scope="s1")
        contents = [e.content for e in results]
        assert any("alpha" in c for c in contents)
        assert not any("beta" in c for c in contents)

        await manager.shutdown_all()


class TestMemoryNoScopeReturnsAll:
    """Without session_scope, all entries are returned (backward compatibility)."""

    @pytest.mark.asyncio
    async def test_memory_no_scope_returns_all(self) -> None:
        """Omitting session_scope must return entries from all sessions."""
        provider = EpisodicMemoryProvider(ttl=600.0)
        await provider.initialize()

        await provider.insert(_make_entry("item one"), session_id="sess-1")
        await provider.insert(_make_entry("item two"), session_id="sess-2")
        await provider.insert(_make_entry("item three"), session_id="")

        # No session_scope — should return all 3
        results = await provider.search(
            MemoryQuery(keywords=["item"], session_scope="")
        )
        assert len(results) == 3

        await provider.shutdown()


# ═══════════════════════════════════════════════════════════════════════════
# 2. Approval Queue Isolation
# ═══════════════════════════════════════════════════════════════════════════


def _make_service_stub() -> Any:
    """Create a minimal RuntimeLeapService-like stub with approval infrastructure."""
    from leapflow.daemon.approval_coordinator import ApprovalCoordinator
    from leapflow.daemon.service import RuntimeLeapService

    settings = MagicMock()
    settings.daemon_max_concurrent_turns = 1
    settings.daemon_request_ledger_ttl_s = 600.0
    settings.daemon_request_ledger_max_entries = 128
    settings.daemon_approval_ttl_s = 1800.0
    settings.profile_dir = Path("/tmp/fake-profile")
    settings.runtime_dir = Path("/tmp/fake-runtime")
    settings.data_dir = Path("/tmp/fake-data")
    settings.host_backend = "mock"
    settings.enable_reentry = False

    svc = object.__new__(RuntimeLeapService)
    # Wire the coordinator that _deny_pending_for_request / _prune_stale_approvals delegate to
    svc._approval_coordinator = ApprovalCoordinator(ttl_s=1800.0)
    # Expose _approval_pending as a convenience alias for tests that inspect state directly
    svc._approval_pending = svc._approval_coordinator._approval_pending
    return svc


class TestApprovalDenyOnRequestEnd:
    """_deny_pending_for_request cleans pending entries for the given request_id."""

    @pytest.mark.asyncio
    async def test_approval_deny_on_request_end(self) -> None:
        """Pending approvals for a completed turn are denied and removed."""
        svc = _make_service_stub()
        loop = asyncio.get_running_loop()

        # Create two pending approvals for the same request_id
        future_a = loop.create_future()
        future_b = loop.create_future()
        svc._approval_pending["pa-1"] = {
            "request": {"request_id": "req-001"},
            "future": future_a,
            "created_at": time.time(),
        }
        svc._approval_pending["pa-2"] = {
            "request": {"request_id": "req-001"},
            "future": future_b,
            "created_at": time.time(),
        }

        svc._deny_pending_for_request("req-001", reason="turn_ended")

        assert len(svc._approval_pending) == 0
        assert future_a.result() == {"decision": "deny", "reason": "turn_ended"}
        assert future_b.result() == {"decision": "deny", "reason": "turn_ended"}


class TestApprovalPruneStale:
    """_prune_stale_approvals removes entries exceeding TTL."""

    @pytest.mark.asyncio
    async def test_approval_prune_stale(self) -> None:
        """Expired approval entries are pruned."""
        svc = _make_service_stub()
        loop = asyncio.get_running_loop()

        expired_future = loop.create_future()
        svc._approval_pending["stale-1"] = {
            "request": {"request_id": "old-req"},
            "future": expired_future,
            "created_at": time.time() - 7200,  # 2 hours ago — well beyond 1800s TTL
        }

        pruned = svc._prune_stale_approvals()

        assert pruned == 1
        assert len(svc._approval_pending) == 0
        assert expired_future.result() == {"decision": "deny", "reason": "timeout"}


class TestApprovalPruneRespectsTTL:
    """_prune_stale_approvals does not remove entries within TTL."""

    @pytest.mark.asyncio
    async def test_approval_prune_respects_ttl(self) -> None:
        """Fresh approval entries must NOT be pruned."""
        svc = _make_service_stub()
        loop = asyncio.get_running_loop()

        fresh_future = loop.create_future()
        svc._approval_pending["fresh-1"] = {
            "request": {"request_id": "new-req"},
            "future": fresh_future,
            "created_at": time.time() - 10,  # 10 seconds ago — well within TTL
        }

        pruned = svc._prune_stale_approvals()

        assert pruned == 0
        assert len(svc._approval_pending) == 1
        assert not fresh_future.done()


class TestApprovalMultipleTurnsIsolation:
    """Pending approvals from different request_ids do not interfere."""

    @pytest.mark.asyncio
    async def test_approval_multiple_turns_isolation(self) -> None:
        """Denying one request_id leaves other request_ids untouched."""
        svc = _make_service_stub()
        loop = asyncio.get_running_loop()

        future_a = loop.create_future()
        future_b = loop.create_future()
        svc._approval_pending["pa-a"] = {
            "request": {"request_id": "req-A"},
            "future": future_a,
            "created_at": time.time(),
        }
        svc._approval_pending["pa-b"] = {
            "request": {"request_id": "req-B"},
            "future": future_b,
            "created_at": time.time(),
        }

        # Deny only req-A
        svc._deny_pending_for_request("req-A")

        assert "pa-a" not in svc._approval_pending
        assert "pa-b" in svc._approval_pending
        assert future_a.done()
        assert not future_b.done()


# ═══════════════════════════════════════════════════════════════════════════
# 3. Long-running Stability
# ═══════════════════════════════════════════════════════════════════════════


class TestApprovalNoAccumulation:
    """Multiple turn endings must not accumulate stale approval entries."""

    @pytest.mark.asyncio
    async def test_approval_no_accumulation(self) -> None:
        """After N turns each creating and cleaning approvals, dict stays empty."""
        svc = _make_service_stub()
        loop = asyncio.get_running_loop()

        for i in range(50):
            request_id = f"req-{i:04d}"
            future = loop.create_future()
            svc._approval_pending[f"pa-{i}"] = {
                "request": {"request_id": request_id},
                "future": future,
                "created_at": time.time(),
            }
            # Simulate turn end
            svc._deny_pending_for_request(request_id)

        # After 50 turns, nothing should remain
        assert len(svc._approval_pending) == 0
