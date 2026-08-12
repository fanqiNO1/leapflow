"""Tests for the Darwin platform adapter.

Verifies that each port exposed by DarwinPerceptionAdapter and
DarwinExecutionAdapter (src/leapflow/platform/adapters/darwin.py) returns
the expected values when driven against a real cua-driver, including:

- perception: fs subscription, UI tree parsing, clipboard, screenshots,
  and event streaming
- execution: file operations with undo/backup bookkeeping, UI actions,
  app launch/activate, intents, shell, input, and multi-step rollback

The whole module is skipped when the cua-driver binary is not installed
on PATH; a module-scoped fixture boots one shared client/VSI/adapter
stack for all tests.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import AsyncIterator

import pytest

from leapflow.cli.commands.host import _cua_driver_installed
from leapflow.domain.events import UISnapshot
from leapflow.platform.adapters.darwin import DarwinExecutionAdapter, DarwinPerceptionAdapter
from leapflow.platform.adapters.mock import MockExecutionAdapter, MockPerceptionAdapter
from leapflow.platform.cua_client import CuaDriverClient
from leapflow.platform.facade import VirtualSystemInterface

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    not _cua_driver_installed(),
    reason="cua-driver binary not found on PATH",
)


def _is_browser_window(app_name: str):
    browsers = ["chrome", "firefox", "safari", "edge", "brave"]
    return any(browser in app_name.lower() for browser in browsers)


@dataclass(frozen=True)
class Adapters:
    """Bundle of a live cua-driver stack shared by all tests in this module."""

    rpc: CuaDriverClient
    darwin_execution: DarwinExecutionAdapter
    darwin_perception: DarwinPerceptionAdapter
    mock_execution: MockExecutionAdapter
    mock_perception: MockPerceptionAdapter


@pytest.fixture(scope="module")
async def adapters() -> AsyncIterator[Adapters]:
    rpc = CuaDriverClient()
    # hack timeout mapping
    rpc._timeout_map = {
        "ping": 3.0,
        "ax": 60.0,
        "app": 30.0,
        "app.list": 60.0,
        "input": 60.0,
        "screen": 60.0,
        "recording": 10.0,
        "clipboard": 3.0,
        "file": 15.0,
        "system": 5.0,
    }
    try:
        rpc.start()
        manifest = await VirtualSystemInterface(rpc).handshake()
    except Exception:
        try:
            rpc.stop()
        except Exception:
            logger.debug("CuaDriverClient cleanup failed after start error", exc_info=True)
        raise
    try:
        yield Adapters(
            rpc=rpc,
            darwin_execution=DarwinExecutionAdapter(rpc, manifest),
            darwin_perception=DarwinPerceptionAdapter(rpc, manifest),
            mock_execution=MockExecutionAdapter(),
            mock_perception=MockPerceptionAdapter(),
        )
    finally:
        rpc.stop()


async def test_perception_subscribe_fs(adapters: Adapters) -> None:
    """subscribe_fs() returns a str subscription id for the watched paths."""
    result1 = await adapters.darwin_perception.subscribe_fs([])
    assert isinstance(result1, str), type(result1)

    with tempfile.TemporaryDirectory() as tmpdir:
        result2 = await adapters.darwin_perception.subscribe_fs([tmpdir])
        assert isinstance(result2, str), type(result2)


async def test_perception_windows(adapters: Adapters) -> None:
    windows_info = await adapters.darwin_perception.list_windows()
    assert isinstance(windows_info, dict)
    assert "ok" in windows_info
    assert windows_info["ok"] is True
    assert "windows" in windows_info
    assert isinstance(windows_info["windows"], list)

    windows = windows_info.get("windows", [])

    # find a browser window
    windows = [window for window in windows if _is_browser_window(window.get("app_name", ""))]
    if len(windows) == 0:
        pytest.skip("No browser windows found to test read_window_state()")
    window = windows[0]

    assert isinstance(window, dict)
    assert "pid" in window
    assert "window_id" in window

    result = await adapters.darwin_perception.read_window_state(
        window["pid"], window["window_id"]
    )
    assert isinstance(result, UISnapshot)
    assert (result.pid, result.window_id) == (window["pid"], window["window_id"])
    for element in result.elements:
        assert isinstance(element.element_index, int)
        assert element.target  # token or index — always addressable


async def test_perception_execution_clipboard(adapters: Adapters) -> None:
    text = "I am the text for testing clipboard"
    await adapters.darwin_execution.set_clipboard(text)
    result = await adapters.darwin_perception.get_clipboard()

    assert isinstance(result, dict)
    assert "ok" in result
    assert result["ok"] is True
    assert "text" in result
    assert result["text"] == text


async def test_perception_capture_screenshot(adapters: Adapters) -> None:
    result = await adapters.darwin_perception.capture_screenshot()
    assert isinstance(result, dict)
    assert "ok" in result
    assert result["ok"] is True
    assert "path" in result
    assert os.path.exists(result["path"])


async def test_execution_perform_file_op(adapters: Adapters) -> None:
    """perform_file_op() list/move/copy/delete all return ok dicts with result paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "test.txt")
        with open(test_file, "w") as f:
            f.write("Hello, world!")

        result1 = await adapters.darwin_execution.perform_file_op(
            "list", {"path": tmpdir}
        )
        assert isinstance(result1, dict)
        assert "ok" in result1
        assert result1["ok"] is True
        assert "result" in result1
        assert isinstance(result1["result"], list)

        names = [entry["name"] for entry in result1["result"]]
        assert "test.txt" in names

        result2 = await adapters.darwin_execution.perform_file_op(
            "move", {"source": test_file, "destination": os.path.join(tmpdir, "test2.txt")}
        )
        assert isinstance(result2, dict)
        assert result2["ok"] is True
        assert "moved" in result2
        assert result2["moved"] == os.path.join(tmpdir, "test2.txt")

        result3 = await adapters.darwin_execution.perform_file_op(
            "copy", {"source": result2["moved"], "destination": os.path.join(tmpdir, "test3.txt")}
        )
        assert isinstance(result3, dict)
        assert result3["ok"] is True
        assert "copied" in result3
        assert result3["copied"] == os.path.join(tmpdir, "test3.txt")

        result4 = await adapters.darwin_execution.perform_file_op(
            "delete", {"path": result3["copied"]}
        )
        assert isinstance(result4, dict)
        assert result4["ok"] is True
        assert "deleted" in result4
        assert result4["deleted"] == os.path.join(tmpdir, "test3.txt")


async def test_execution_undo_restores_deleted_file(adapters: Adapters) -> None:
    """undo_last() after a delete restores the file from the pre-delete backup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "undo_me.txt")
        with open(test_file, "w") as f:
            f.write("precious content")

        depth_before = adapters.darwin_execution.undo_depth
        result = await adapters.darwin_execution.perform_file_op(
            "delete", {"path": test_file}
        )
        assert result["ok"] is True
        assert result.get("deleted") == test_file
        assert not os.path.exists(test_file)
        assert adapters.darwin_execution.undo_depth == depth_before + 1

        undone = await adapters.darwin_execution.undo_last()
        assert undone.get("ok") is True, undone
        assert os.path.exists(test_file)
        with open(test_file) as f:
            assert f.read() == "precious content"


async def test_execution_list_apps(adapters: Adapters) -> None:
    """list_apps() returns app records carrying name and an integer pid field."""
    result = await adapters.darwin_execution.list_apps()
    assert isinstance(result, dict)
    assert result.get("ok") is True
    apps = result.get("apps")
    assert isinstance(apps, list) and len(apps) > 0
    for record in apps[:5]:
        assert isinstance(record, dict)
        assert "name" in record
        assert isinstance(record.get("pid"), int)


async def test_execution_exec_shell(adapters: Adapters) -> None:
    """exec_shell() runs locally and reports stdout plus a zero exit code."""
    result = await adapters.darwin_execution.exec_shell("echo hello-leapflow")
    assert result["ok"] is True
    assert "hello-leapflow" in result["stdout"]
    assert result["exit_code"] == 0

    failed = await adapters.darwin_execution.exec_shell(
        "exit 3" if sys.platform != "win32" else "cmd /c exit 3"
    )
    assert failed["ok"] is False
    assert failed["exit_code"] == 3


async def test_execution_run_intent_degrades_without_capability(adapters: Adapters) -> None:
    """run_intent() reports intents_not_supported when the manifest lacks the capability."""
    result = await adapters.darwin_execution.run_intent("open_note", {})
    assert result == {"ok": False, "error": "intents_not_supported"}


async def test_execution_perform_ui_action_rejects_stale_token(adapters: Adapters) -> None:
    """perform_ui_action() with a fabricated element_token is refused by the driver."""
    from leapflow.platform.protocol import RpcError

    with pytest.raises(RpcError):
        await adapters.darwin_execution.perform_ui_action("s99999999:0", "press")


@pytest.mark.skipif(sys.platform != "win32", reason="notepad round-trip is Windows-only")
@pytest.mark.skipif(
    os.environ.get("LEAPFLOW_TEST_INTERACTIVE") != "1",
    reason="steals focus and opens windows; set LEAPFLOW_TEST_INTERACTIVE=1 to run",
)
async def test_execution_notepad_input_roundtrip(adapters: Adapters) -> None:
    """launch/activate/type_text/shortcut/scroll round-trip, verified via clipboard."""
    text = "leapflow input roundtrip input test"
    launch = await adapters.darwin_execution.launch_app("notepad")
    assert launch.get("ok") is True, launch
    pid = launch.get("pid")
    assert isinstance(pid, int) and pid > 0, launch

    try:
        # The launch response carries the windows array; fall back to
        # polling list_windows until the window is registered.
        window_id = None
        for record in launch.get("windows") or []:
            if isinstance(record.get("window_id"), int):
                window_id = record["window_id"]
                break
        for _ in range(10):
            if window_id is not None:
                break
            await asyncio.sleep(0.5)
            listed = await adapters.darwin_perception.list_windows()
            for record in listed.get("windows", []):
                if record.get("pid") == pid and isinstance(record.get("window_id"), int):
                    window_id = record["window_id"]
                    break
        assert window_id is not None, "notepad window never appeared"

        activated = await adapters.darwin_execution.activate_app(pid, window_id)
        assert isinstance(activated, dict), activated
        await asyncio.sleep(1.0)

        typed = await adapters.darwin_execution.type_text(text)
        assert typed.get("ok") is True, typed
        await asyncio.sleep(0.5)

        # Select-all + copy, then verify the typed text through the clipboard.
        await adapters.darwin_execution.send_shortcut("ctrl+a")
        await asyncio.sleep(0.3)
        await adapters.darwin_execution.send_shortcut("ctrl+c")
        await asyncio.sleep(0.5)
        clipboard = await adapters.darwin_perception.get_clipboard()
        assert clipboard.get("text") == text, clipboard

        # Targetless scroll drives the focused scroller (keystroke path).
        scrolled = await adapters.darwin_execution.scroll("", "down", 2)
        assert scrolled.get("ok") is True, scrolled
    finally:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True, timeout=10,
        )

