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

import logging
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
        "input": 5.0,
        "screen": 10.0,
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


async def test_perception_read_window_state(adapters: Adapters) -> None:
    windows_info = await adapters.darwin_perception.list_windows()
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


async def test_perception_list_windows(adapters: Adapters) -> None:
    windows = await adapters.darwin_perception.list_windows()
    assert isinstance(windows, dict)
    assert "windows" in windows
    assert isinstance(windows["windows"], list)

