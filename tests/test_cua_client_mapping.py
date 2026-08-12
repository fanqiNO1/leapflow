"""CuaDriverClient method→tool mapping and timeout resolution.

Locks the cua-driver 0.19.3 wire contract: get_window_state takes
pid+window_id (discovered via ax.list → list_windows), actions target
element_token/element_index with x/y pixel args, hotkey takes a keys array
(single keys go to press_key), scroll speaks direction/amount, activation
is bring_to_front by pid, and launch_app accepts bundle_id/name only.
"""

from __future__ import annotations

import pytest

from leapflow.platform.cua_client import (
    CuaDriverClient,
    _launch_app_key,
    _normalize_shortcut_keys,
    _resolve_ax_perform_tool,
)
from leapflow.platform.protocol import Methods, RpcError


def _client() -> CuaDriverClient:
    return CuaDriverClient()


# ── launch_app field selection ───────────────────────────────────────────────

def test_launch_app_key_recognizes_identifier_kinds() -> None:
    # AUMID (Windows) and reverse-DNS (macOS) are bundle ids.
    assert _launch_app_key("Microsoft.WindowsNotepad_8wekyb3d8bbwe!App") == "bundle_id"
    assert _launch_app_key("com.apple.calculator") == "bundle_id"
    # Display names and executable paths go through name — 0.19.3 has no path field.
    assert _launch_app_key("Notepad") == "name"
    assert _launch_app_key(r"C:\Program Files\Edge\msedge.exe") == "name"
    assert _launch_app_key("msedge.exe") == "name"


def test_app_launch_maps_to_schema_fields() -> None:
    client = _client()

    tool, args = client._map_to_cua_tool(Methods.APP_LAUNCH, {"app_name": "Notepad"})
    assert tool == "launch_app"
    assert args == {"name": "Notepad"}

    tool, args = client._map_to_cua_tool(
        Methods.APP_LAUNCH, {"bundle_id": "com.apple.calculator"}
    )
    assert args == {"bundle_id": "com.apple.calculator"}
    assert "path" not in args and "app_name" not in args


# ── window state / discovery / activation ────────────────────────────────────

def test_ax_tree_maps_to_pid_window_target() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(
        Methods.AX_TREE, {"pid": 100, "window_id": 7, "include_screenshot": False}
    )
    assert tool == "get_window_state"
    assert args == {"pid": 100, "window_id": 7, "include_screenshot": False}
    assert "app" not in args and "bundle_id" not in args


def test_ax_tree_without_target_is_refused() -> None:
    client = _client()
    with pytest.raises(RpcError) as excinfo:
        client._map_to_cua_tool(Methods.AX_TREE, {"bundle_id": "com.mock.app"})
    assert excinfo.value.code == "invalid_params"


def test_ax_list_maps_to_list_windows_without_args() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(Methods.AX_LIST, {})
    assert (tool, args) == ("list_windows", {})


def test_app_activate_maps_to_bring_to_front() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(
        Methods.APP_ACTIVATE, {"pid": 100, "window_id": 7}
    )
    assert tool == "bring_to_front"
    assert args == {"pid": 100, "window_id": 7}

    with pytest.raises(RpcError):
        client._map_to_cua_tool(Methods.APP_ACTIVATE, {"bundle_id": "com.mock.app"})


def test_app_list_maps_without_args() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(
        Methods.APP_LIST, {"filter": "ignored", "running_only": True}
    )
    assert (tool, args) == ("list_apps", {})


# ── ax.perform action table ──────────────────────────────────────────────────

def test_ax_perform_maps_legacy_ax_actions() -> None:
    tool, args = _resolve_ax_perform_tool({"action": "AXPress", "node_id": "tok-abc"})
    assert tool == "click"
    assert args == {"element_token": "tok-abc"}

    tool, args = _resolve_ax_perform_tool({"action": "AXShowMenu", "node_id": "tok-abc"})
    assert tool == "right_click"
    assert args == {"element_token": "tok-abc"}

    tool, args = _resolve_ax_perform_tool({"action": "double_click", "node_id": "42"})
    assert tool == "double_click"
    assert args == {"element_index": 42}


def test_ax_perform_pixel_coordinates_become_x_y() -> None:
    tool, args = _resolve_ax_perform_tool(
        {"action": "click", "coordinates": {"x": 10, "y": 20}, "pid": 100}
    )
    assert tool == "click"
    assert args["x"] == 10 and args["y"] == 20 and args["pid"] == 100
    assert "coordinates" not in args


def test_ax_perform_value_entry_tools() -> None:
    tool, args = _resolve_ax_perform_tool(
        {"action": "set_value", "node_id": "tok-1", "value": "42"}
    )
    assert (tool, args["value"]) == ("set_value", "42")

    tool, args = _resolve_ax_perform_tool(
        {"action": "type_text", "node_id": "tok-1", "text": "hello"}
    )
    assert (tool, args["text"]) == ("type_text", "hello")


def test_ax_perform_unknown_action_falls_back_to_plain_click() -> None:
    tool, args = _resolve_ax_perform_tool({"action": "AXRaise", "node_id": "tok-1"})
    assert tool == "click"
    assert "action" not in args  # never forward an invalid action value


# ── scroll ───────────────────────────────────────────────────────────────────

def test_scroll_maps_direction_and_amount() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(
        Methods.AX_SCROLL, {"node_id": "tok-scroll", "direction": "down", "amount": 5}
    )
    assert tool == "scroll"
    assert args == {"element_token": "tok-scroll", "direction": "down", "amount": 5}


def test_scroll_without_direction_is_refused() -> None:
    client = _client()
    with pytest.raises(RpcError):
        client._map_to_cua_tool(Methods.AX_SCROLL, {"node_id": "tok-scroll"})


# ── keyboard ─────────────────────────────────────────────────────────────────

def test_shortcut_combo_parses_into_hotkey_keys_array() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(Methods.INPUT_SHORTCUT, {"keys": "cmd+c"})
    assert tool == "hotkey"
    assert args == {"keys": ["cmd", "c"], "scope": "desktop"}


def test_single_key_routes_to_press_key() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(Methods.INPUT_SHORTCUT, {"keys": "enter"})
    assert tool == "press_key"
    assert args == {"key": "enter", "scope": "desktop"}


def test_shortcut_with_pid_targets_without_desktop_scope() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(
        Methods.INPUT_SHORTCUT, {"keys": ["cmd", "v"], "pid": 100}
    )
    assert tool == "hotkey"
    assert args == {"keys": ["cmd", "v"], "pid": 100}


def test_normalize_shortcut_keys_variants() -> None:
    assert _normalize_shortcut_keys("cmd+shift+4") == ["cmd", "shift", "4"]
    assert _normalize_shortcut_keys("cmd c") == ["cmd", "c"]
    assert _normalize_shortcut_keys(["cmd", "c"]) == ["cmd", "c"]
    assert _normalize_shortcut_keys("") == []


def test_type_text_untargeted_uses_desktop_scope() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(Methods.INPUT_TYPE_TEXT, {"text": "hi"})
    assert tool == "type_text"
    assert args == {"text": "hi", "scope": "desktop"}


# ── screen capture ───────────────────────────────────────────────────────────

def test_screen_capture_maps_to_desktop_or_window_state() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(Methods.SCREEN_CAPTURE_FRAME, {})
    assert (tool, args) == ("get_desktop_state", {})

    tool, args = client._map_to_cua_tool(
        Methods.SCREEN_CAPTURE_FRAME, {"pid": 100, "window_id": 7}
    )
    assert (tool, args) == ("get_window_state", {"pid": 100, "window_id": 7})


# ── dispatch plumbing (pre-existing contracts) ───────────────────────────────

@pytest.mark.asyncio
async def test_open_url_is_local_dispatch() -> None:
    """open_url never round-trips to cua-driver; a missing url errors locally."""
    client = _client()
    result = await client.call(Methods.OPEN_URL, {})
    assert result == {"ok": False, "error": "url required"}


def test_app_list_timeout_gets_dedicated_budget() -> None:
    from leapflow.platform.cua_client import _APP_LIST_TIMEOUT_S

    client = _client()
    assert client._resolve_timeout(Methods.APP_LIST) == _APP_LIST_TIMEOUT_S
    assert _APP_LIST_TIMEOUT_S > client._call_timeout
    # Other app.* methods keep the launch/activate budget.
    assert client._resolve_timeout(Methods.APP_LAUNCH) == 30.0
    # ax.list shares the ax family budget.
    assert client._resolve_timeout(Methods.AX_LIST) == 8.0
    # Exact entries win over prefixes; unknown prefixes fall back to default.
    assert client._resolve_timeout("custom.method") == client._call_timeout
