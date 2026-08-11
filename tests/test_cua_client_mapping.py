"""CuaDriverClient method→tool mapping and timeout resolution.

Locks the cua-driver 0.17 wire contract: launch_app accepts name/bundle_id/
urls (never app_name), and app.list gets the full call budget because
Windows app enumeration is slow.
"""

from __future__ import annotations

import pytest

from leapflow.platform.cua_client import CuaDriverClient
from leapflow.platform.protocol import Methods


def _client() -> CuaDriverClient:
    return CuaDriverClient()


def test_app_launch_maps_to_schema_fields() -> None:
    client = _client()

    tool, args = client._map_to_cua_tool(Methods.APP_LAUNCH, {"app_name": "Notepad"})
    assert tool == "launch_app"
    assert args == {"name": "Notepad"}

    aumid = "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"
    tool, args = client._map_to_cua_tool(Methods.APP_LAUNCH, {"bundle_id": aumid})
    assert args == {"bundle_id": aumid}

    exe = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    tool, args = client._map_to_cua_tool(Methods.APP_LAUNCH, {"bundle_id": exe})
    assert args == {"path": exe}
    assert "app_name" not in args


def test_app_activate_uses_name_field() -> None:
    client = _client()
    tool, args = client._map_to_cua_tool(Methods.APP_ACTIVATE, {"name": "Chrome"})
    assert tool == "launch_app"
    assert args == {"name": "Chrome"}


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
    # Exact entries win over prefixes; unknown prefixes fall back to default.
    assert client._resolve_timeout("custom.method") == client._call_timeout
