"""R6 — daemon runtime lifecycle: start, report, restart, stop, recover from stale state.

Lifecycle is only meaningful across processes: a PID file, a Unix socket and a
metadata file are all real artefacts on disk, and "restart" means the old process
is gone and a new one owns them. A stale socket left by a crashed daemon must not
block the next start, and version reporting must describe the process actually
answering — not the client asking.

Scope note: inbound gateway signal classification is *not* here. The gateway RPCs
are not implemented in this daemon phase, so a journey could only assert the
NotImplementedError; normalization, SNR filtering, trigger policy and
self-message filtering are already covered where they run, in the mock layer
(``test_feishu_event_normalizer.py``, ``test_gateway_consumer_loop.py``,
``test_trigger_policy.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from leapflow.daemon.client import DaemonUnavailableError
from leapflow.daemon.lifecycle import DaemonInfo, cleanup_stale
from leapflow.daemon._transport import get_transport
from tests._harness.cassette_proxy import answer, scripted
from tests._harness.journey import Journey, JourneyFactory
from tests._harness.leapd import await_for, start_leapd

# Journey metadata read by tools/impact.py (see test_r1_conversation.py).
SUBJECT_PATHS = (
    "src/leapflow/daemon/",
    "src/leapflow/layout.py",
    "src/leapflow/cli/commands/daemon.py",
)

# No LLM semantics: process lifecycle, stale runtime files, and session resume.
# A live run would spend tokens for no extra signal.
LIVE_SIGNAL = False

SESSION = "r6-lifecycle"


async def _turn(client: Any, message: str, workspace: str) -> list[Any]:
    """Run one turn and return its stream events."""
    events: list[Any] = []
    async for event in client.engine_chat(
        message, session_id=SESSION, workspace_root=workspace
    ):
        events.append(event)
    return events


async def _status_or_none(client: Any) -> dict[str, Any] | None:
    """Return daemon.status, tolerating the short restart reconnect window."""
    try:
        return await client.status()
    except DaemonUnavailableError:
        return None


async def _resume_or_none(client: Any) -> dict[str, Any] | None:
    """Return session_resume, tolerating the short restart reconnect window."""
    try:
        return await client.session_resume(SESSION)
    except DaemonUnavailableError:
        return None


async def _history_or_none(client: Any) -> dict[str, Any] | None:
    """Return session_history, tolerating the short restart reconnect window."""
    try:
        return await client.session_history(session_id=SESSION)
    except DaemonUnavailableError:
        return None


@pytest.mark.asyncio
async def test_r6_daemon_lifecycle(journeys: JourneyFactory) -> None:
    """The daemon starts, reports itself, serves work, stops, and recovers cleanly."""
    journey = journeys(
        "r6_lifecycle",
        script=scripted(answer("Still here.")),
        deadline_s=120.0,
        # One turn; the rest is lifecycle.
        max_llm_calls=6,
        max_llm_tokens=80_000,
    )
    workspace = str(journey.workspace("life"))
    client = journey.client()

    with journey.phase("running: lifecycle artefacts exist and agree with each other"):
        info = journey.daemon.info()
        assert info.is_running, "the daemon reports itself as not running"
        assert info.is_healthy, "the socket exists but is not answering"
        assert journey.daemon.sock_path.exists(), "no Unix socket on disk"

        status = await await_for(
            lambda: _status_or_none(client),
            timeout_s=30.0,
            what="daemon.status to respond after startup",
        )
        assert status["pid"] == info.pid, (
            f"status() reports pid {status['pid']} but the pid file says {info.pid}"
        )

    with journey.phase("identity: the daemon describes its own runtime, not the client's"):
        status = await client.status()
        assert status["runtime_version"], "the daemon reported no version"
        assert status["runtime_executable"], "the daemon reported no executable"
        assert str(journey.daemon.data_dir) in status["profile_dir"], (
            f"the daemon is serving {status['profile_dir']}, not this journey's "
            f"profile under {journey.daemon.data_dir}"
        )
        assert status["runtime_dir"] == str(journey.daemon.runtime_dir)

    with journey.phase("serving: a turn works, proving this is more than a socket"):
        events = await _turn(client, "Are you there?", workspace)
        assert not [event for event in events if event.type == "error"]

    with journey.phase("stop: shutdown removes the process and its runtime files"):
        old_pid = journey.daemon.info().pid
        try:
            await client.shutdown()
        except DaemonUnavailableError:
            # A daemon that closes the socket while replying is a valid shutdown.
            pass
        gone = await await_for(
            lambda: _not_running(journey),
            timeout_s=30.0,
            what="the daemon process to exit",
        )
        assert gone, f"daemon pid {old_pid} is still running after shutdown"

    with journey.phase("stale state: leftover files do not block the next start"):
        # Simulate the crash case: runtime files present, no process behind them.
        journey.daemon.runtime_dir.mkdir(parents=True, exist_ok=True)
        (journey.daemon.runtime_dir / "leapd.pid").write_text("999999", encoding="utf-8")
        sock_path = get_transport().readiness_path(journey.daemon.runtime_dir)
        sock_path.touch(exist_ok=True)

        stale = DaemonInfo.discover(journey.daemon.runtime_dir)
        assert not stale.is_healthy, "a stale socket was reported as healthy"

        removed = cleanup_stale(journey.daemon.runtime_dir)
        assert removed, "stale runtime files were not cleaned up"
        assert not (journey.daemon.runtime_dir / "leapd.pid").exists()

    with journey.phase("restart: a fresh daemon takes over the same profile"):
        restarted = start_leapd(
            root=journey.daemon.data_dir.parent,
            llm_base_url=journey.proxy.base_url,
            llm_model=journey.daemon.env["LEAPFLOW_LLM_MODEL"],
            profile=journey.daemon.profile,
        )
        journey.daemon.process = restarted.process
        try:
            assert restarted.info().is_healthy, "the replacement daemon never became healthy"
            fresh_client = restarted.client()
            status = await await_for(
                lambda: _status_or_none(fresh_client),
                timeout_s=30.0,
                what="daemon.status to respond after restart",
            )
            assert status["pid"] != old_pid, (
                "the replacement daemon reports the dead process' pid"
            )
            assert status["runtime_dir"] == str(journey.daemon.runtime_dir), (
                "the replacement daemon is not serving the same profile runtime"
            )

            with journey.phase("continuity: a prior session is resumable after restart"):
                # A fresh daemon holds no live session, so history is only reachable
                # the way a user reaches it: by resuming explicitly (`leap --resume`).
                resumed = await await_for(
                    lambda: _resume_or_none(fresh_client),
                    timeout_s=30.0,
                    what="session.resume to respond after restart",
                )
                assert resumed.get("found") is True, (
                    f"session {SESSION!r} was not recoverable after a restart: {resumed}"
                )
                assert resumed.get("session_id") == SESSION, (
                    f"resume returned a different session than asked for: {resumed}"
                )
                history = await await_for(
                    lambda: _history_or_none(fresh_client),
                    timeout_s=30.0,
                    what="session.history to respond after restart",
                )
                blob = str(history.get("messages") or [])
                assert "Are you there?" in blob, (
                    "the conversation recorded before the restart did not survive it"
                )
        finally:
            restarted.stop()

    journey.finish()


async def _not_running(journey: Journey) -> bool:
    """True once no process owns the daemon's runtime directory."""
    return not DaemonInfo.discover(journey.daemon.runtime_dir).is_running
