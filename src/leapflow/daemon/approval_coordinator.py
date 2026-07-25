"""Approval lifecycle coordinator extracted from RuntimeLeapService."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from leapflow.daemon.protocol import StreamChunk

logger = logging.getLogger(__name__)


class ApprovalCoordinator:
    """Manages daemon approval lifecycle: pending queue, resolution, TTL cleanup."""

    def __init__(self, ttl_s: float = 1800.0) -> None:
        self._approval_pending: dict[str, dict[str, Any]] = {}
        self._ttl_s = ttl_s

    def install_gate(self, ctx: Any, service: Any) -> None:
        """Install the daemon-mode approval gate on ctx.

        *service* is the owning RuntimeLeapService (needed by _DaemonApprovalGate
        to route approval requests back through the coordinator).
        """
        try:
            from leapflow.security.approval import SessionAwareGate
            from leapflow.security.actions import ActionDescriptor
            from leapflow.security.orchestrator import ApprovalOrchestrator
            from leapflow.tools.gateway_tool import set_gateway_approval_gate
            from leapflow.tools.registry_bootstrap import set_file_read_gate, set_file_write_gate
            from leapflow.tools.shell_tools import set_approval_gate

            existing = getattr(ctx, "_approval_orchestrator", None)
            gate = SessionAwareGate(_DaemonApprovalGate(self))
            orchestrator = ApprovalOrchestrator(
                gate,
                grants=getattr(existing, "grants", None),
                audit=getattr(existing, "audit", None),
            )
            ctx._approval_gate = gate
            ctx._approval_orchestrator = orchestrator
            set_approval_gate(orchestrator)
            set_gateway_approval_gate(orchestrator)

            class _FileReadGate:
                def __init__(self) -> None:
                    self.denial_message = ""

                async def check(
                    self,
                    path: str,
                    mode: str = "raw",
                    sensitivity_meta: dict | None = None,
                ) -> bool:
                    result = await orchestrator.evaluate(
                        ActionDescriptor.file_read(path, mode=mode, metadata=dict(sensitivity_meta or {}))
                    )
                    self.denial_message = result.denial_message if not result.approved else ""
                    return result.approved

            class _FileWriteGate:
                def __init__(self) -> None:
                    self.denial_message = ""

                async def check(
                    self,
                    path: str,
                    content: str,
                    mode: str = "overwrite",
                    sensitivity_meta: dict | None = None,
                ) -> bool:
                    result = await orchestrator.evaluate(
                        ActionDescriptor.file_write(path, content, mode=mode, metadata=dict(sensitivity_meta or {}))
                    )
                    self.denial_message = result.denial_message if not result.approved else ""
                    return result.approved

            set_file_read_gate(_FileReadGate())
            set_file_write_gate(_FileWriteGate())
            logger.debug("daemon approval gate installed")
        except Exception:
            logger.debug("daemon approval gate installation skipped", exc_info=True)

    async def request_approval(self, request: Any, route: "tuple[asyncio.Queue[StreamChunk], str] | None") -> str:
        """Block until approval decision; called from tool execution.

        *route* is the per-turn (queue, request_id) tuple from the ContextVar.
        """
        if route is None:
            return "deny"
        queue, active_request_id = route
        pending_id = str(getattr(request, "request_id", "") or uuid.uuid4().hex)
        request_id = active_request_id or pending_id
        payload = request.to_dict()
        payload["pending_id"] = pending_id
        payload["request_id"] = request_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._approval_pending[pending_id] = {
            "request": payload,
            "future": future,
            "queue": queue,
            "created_at": time.time(),
        }
        await queue.put(StreamChunk(
            request_id=request_id,
            content="Approval required",
            event_type="approval_request",
            metadata={"approval": payload, "request_id": request_id},
        ))
        timeout_s = 120.0
        if getattr(request, "expires_at", None):
            timeout_s = max(1.0, float(request.expires_at) - time.time())
        try:
            result = await asyncio.wait_for(future, timeout=timeout_s)
            return str(result.get("decision") or "deny")
        except TimeoutError:
            return "deny"
        finally:
            self._approval_pending.pop(pending_id, None)

    async def resolve(self, pending_id: str, decision: str, reason: str = "") -> dict[str, Any]:
        """Resolve a pending approval."""
        pending = self._approval_pending.get(pending_id)
        if pending is None:
            return {"ok": False, "error": f"Unknown approval request: {pending_id}"}
        future = pending.get("future")
        if not isinstance(future, asyncio.Future) or future.done():
            return {"ok": False, "error": f"Approval request is no longer pending: {pending_id}"}
        future.set_result({"decision": self._normalize_decision(decision), "reason": reason})
        return {"ok": True, "pending_id": pending_id, "decision": self._normalize_decision(decision)}

    async def cancel(self, pending_id: str, reason: str = "cancelled") -> dict[str, Any]:
        """Cancel a pending approval."""
        return await self.resolve(pending_id, "deny", reason=reason)

    def get_status(self) -> dict[str, Any]:
        """Return current approval queue status."""
        return {"pending": self._pending_payloads()}

    def pending_count(self) -> int:
        """Return the number of currently pending approvals."""
        return len(self._approval_pending)

    def deny_for_queue(self, queue: "asyncio.Queue[StreamChunk]", reason: str = "stream_closed") -> None:
        """Deny all pending approvals bound to a specific queue."""
        for pending_id, pending in list(self._approval_pending.items()):
            if pending.get("queue") is not queue:
                continue
            future = pending.get("future")
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_result({"decision": "deny", "reason": reason})
            self._approval_pending.pop(pending_id, None)

    def deny_for_request(self, request_id: str, reason: str = "turn_ended") -> None:
        """Deny all pending approvals bound to a specific request.

        Called when a turn ends (normally or exceptionally) to prevent
        orphaned approval futures from leaking memory indefinitely.
        """
        for pending_id, pending in list(self._approval_pending.items()):
            payload = pending.get("request") or {}
            if payload.get("request_id") != request_id:
                continue
            future = pending.get("future")
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_result({"decision": "deny", "reason": reason})
            self._approval_pending.pop(pending_id, None)

    def prune_stale(self, ttl_s: float | None = None) -> int:
        """Remove approvals older than TTL. Returns count removed."""
        if ttl_s is None:
            ttl_s = self._ttl_s
        now = time.time()
        pruned = 0
        for pending_id, pending in list(self._approval_pending.items()):
            created_at = pending.get("created_at", now)
            if (now - created_at) < ttl_s:
                continue
            future = pending.get("future")
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_result({"decision": "deny", "reason": "timeout"})
            self._approval_pending.pop(pending_id, None)
            pruned += 1
        if pruned:
            logger.info("daemon: pruned %d stale approval(s)", pruned)
        return pruned

    def _pending_payloads(self) -> list[dict[str, Any]]:
        return [dict(item.get("request") or {}) for item in self._approval_pending.values()]

    @staticmethod
    def _normalize_decision(decision: str) -> str:
        allowed = {
            "allow",
            "allow_once",
            "allow_session",
            "allow_always",
            "deny",
            "deny_always",
            "cancel_workflow",
        }
        value = str(decision or "deny").strip().lower()
        return value if value in allowed else "deny"


class _DaemonApprovalGate:
    """Approval gate that bridges daemon-side actions to thin clients."""

    def __init__(self, coordinator: ApprovalCoordinator) -> None:
        self._coordinator = coordinator

    async def request_approval(self, request: Any) -> Any:
        from leapflow.security.approval import ApprovalDecision

        # Import ContextVar from shared module (avoids circular dep with service).
        from leapflow.daemon.approval_route import approval_route as _approval_route

        route = _approval_route.get()
        decision = await self._coordinator.request_approval(request, route)
        try:
            return ApprovalDecision(decision)
        except ValueError:
            return ApprovalDecision.DENY
