"""ToolBridge factory — constructs a bridge with semantic tools when perception is available.

This is the integration point where the SemanticAdapter layer is wired in.
When only ExecutionPort is available, falls back to the basic ToolBridge
(file ops + shell + launch_app + ui_action). When PerceptionPort is also
provided, the full semantic tool set is registered (observe_ui, click,
type_text, shortcut, switch_app, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from leapflow.skills.tool_executor import ToolBridge

if TYPE_CHECKING:
    from leapflow.engine.confirmation import IOProvider
    from leapflow.skills.action_policy import PolicyEngine


def build_tool_bridge(
    execution: Any,
    perception: Optional[Any] = None,
    *,
    policy: Optional["PolicyEngine"] = None,
    io: Optional["IOProvider"] = None,
) -> ToolBridge:
    """Construct a ToolBridge, optionally enriched with semantic UI tools.

    Args:
        execution: ExecutionPort implementation.
        perception: Optional PerceptionPort. When provided, enables
                    semantic UI tools (observe_ui, click, type_text, etc.)
                    via the SemanticAdapter translation layer.

    Returns:
        A fully configured ToolBridge ready for ReAct execution.
    """
    bridge = ToolBridge(execution, policy=policy, io=io)

    if perception is None:
        return bridge

    from leapflow.skills.semantic_adapter import SemanticAdapter

    adapter = SemanticAdapter(perception=perception, execution=execution)

    bridge.register(
        "list_windows",
        "List all top-level windows with pid, window_id, title, and per-window state "
        "(minimized, on-screen). Call this first to pick the pid and window_id that "
        "observe_ui and other window tools require.",
        {},
        adapter.list_windows,
    )
    bridge.register(
        "observe_ui",
        "Snapshot one window's actionable UI elements, each tagged with an element_index "
        "for click/right_click/read_text. Re-observe after actions — indices belong to one "
        "snapshot. Requires the window's pid and window_id from list_windows.",
        {
            "pid": "int (required) — target process ID from list_windows",
            "window_id": "int (required) — target window ID from list_windows",
            "query": "string (optional) — case-insensitive filter over roles/labels to shrink large windows",
        },
        adapter.observe_ui,
    )
    bridge.register(
        "click",
        "Click a UI element by its element_index (from the latest observe_ui snapshot)",
        {"element_index": "int (required) — element_index from observe_ui"},
        adapter.click,
        mutates_state=True,
    )
    bridge.register(
        "type_text",
        "Type text into the currently focused element",
        {
            "text": "string (required) — text to type",
        },
        adapter.type_text,
        mutates_state=True,
    )
    bridge.register(
        "shortcut",
        "Execute a keyboard shortcut",
        {"keys": "string (required) — shortcut keys, e.g. 'cmd+c', 'cmd+v', 'enter', 'cmd+t'"},
        adapter.shortcut,
        mutates_state=True,
    )
    bridge.register(
        "switch_app",
        "Switch to an app (launch if needed, activate, verify)",
        {"app_id": "string (required) — target app bundle ID"},
        adapter.switch_app,
        mutates_state=True,
    )
    bridge.register(
        "list_apps",
        "List available applications on this system. Use to discover correct bundle_id before switch_app.",
        {
            "filter": "string (optional) — filter by app name or bundle_id substring",
            "running_only": "boolean (optional, default=false) — only list currently running apps",
        },
        adapter.list_apps,
    )
    bridge.register(
        "open_url",
        "Open a URL in the default or specified browser",
        {
            "url": "string (required) — URL to open",
            "app_id": "string (optional) — browser bundle ID",
        },
        adapter.open_url,
        mutates_state=True,
    )
    bridge.register(
        "get_clipboard",
        "Read current clipboard text content",
        {},
        adapter.get_clipboard,
    )
    bridge.register(
        "set_clipboard",
        "Write text to the clipboard",
        {"text": "string (required) — text to place on clipboard"},
        adapter.set_clipboard,
        mutates_state=True,
    )
    bridge.register(
        "read_text",
        "Read the text content of a specific UI element from the latest snapshot",
        {"element_index": "int (required) — element_index from observe_ui"},
        adapter.read_text,
    )
    bridge.register(
        "wait",
        "Wait for a specified duration before continuing",
        {"seconds": "number (required) — seconds to wait (0.1-30)"},
        adapter.wait,
        mutates_state=True,
        counts_as_progress=False,
    )
    bridge.register(
        "wait_until",
        "Wait until a UI condition is met (polls UI tree). Returns elements when found or on timeout.",
        {
            "condition": "string (required) — what to wait for (e.g. 'Send button', '发送')",
            "pid": "int (optional) — window's process ID, default = last observed window",
            "window_id": "int (optional) — window ID, default = last observed window",
            "timeout": "number (optional, default=30) — max seconds to wait",
            "poll_interval": "number (optional, default=2) — seconds between polls",
        },
        adapter.wait_until,
        mutates_state=True,
        counts_as_progress=False,
    )
    bridge.register(
        "wait_until_stable",
        "Wait until the UI stops changing (element set stabilizes across polls).",
        {
            "timeout": "number (optional, default=30) — max seconds to wait",
            "poll_interval": "number (optional, default=2) — seconds between polls",
            "pid": "int (optional) — window's process ID, default = last observed window",
            "window_id": "int (optional) — window ID, default = last observed window",
        },
        adapter.wait_until_stable,
        mutates_state=True,
        counts_as_progress=False,
    )
    bridge.register(
        "scroll",
        "Scroll a scrollable area. Omit element_index to scroll the focused/page scroller; "
        "pass one to scroll an exact element from the latest snapshot.",
        {
            "element_index": "int (optional) — scroll target from observe_ui, omit for focused scroller",
            "direction": "string (optional, default='down') — up/down/left/right",
            "amount": "number (optional, default=3) — scroll units (1-20)",
        },
        adapter.scroll,
        mutates_state=True,
    )
    bridge.register(
        "select_text",
        "Select all text in a UI element (focus + select-all, for subsequent copy)",
        {
            "element_index": "int (required) — element containing text, from observe_ui",
        },
        adapter.select_text,
        mutates_state=True,
    )
    bridge.register(
        "right_click",
        "Right-click a UI element to open its context menu. Returns visible menu items.",
        {
            "element_index": "int (required) — element to right-click, from observe_ui",
        },
        adapter.right_click,
        mutates_state=True,
    )
    bridge.register(
        "screenshot",
        "Capture a screenshot for visual verification. With pid + window_id captures that "
        "window (works across all displays); defaults to the last observed window, or the "
        "full desktop when no window has been observed.",
        {
            "pid": "int (optional) — window's process ID from list_windows",
            "window_id": "int (optional) — window ID from list_windows",
        },
        adapter.screenshot,
    )

    return bridge
