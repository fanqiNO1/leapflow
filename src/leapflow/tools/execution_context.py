"""Per-turn tool execution context for workspace-scoped safety.

Daemon-backed turns from different TUI clients may share one Python process but
must not share a project root.  The engine installs this context around each
actual tool execution so path-oriented tools resolve relative paths against the
active turn's workspace and reject accidental cross-workspace absolute paths.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolExecutionContext:
    """Workspace boundary for one tool execution turn."""

    workspace_root: Path
    allowed_roots: tuple[Path, ...]
    session_id: str = ""
    task_id: str = ""

    @classmethod
    def from_strings(
        cls,
        *,
        workspace_root: str,
        allowed_roots: tuple[str, ...] = (),
        session_id: str = "",
        task_id: str = "",
    ) -> "ToolExecutionContext":
        root = Path(workspace_root).expanduser().resolve()
        roots = tuple(Path(item).expanduser().resolve() for item in allowed_roots if item)
        return cls(
            workspace_root=root,
            allowed_roots=roots or (root,),
            session_id=session_id,
            task_id=task_id,
        )


_current_context: contextvars.ContextVar[ToolExecutionContext | None] = contextvars.ContextVar(
    "leapflow_tool_execution_context",
    default=None,
)


def current_tool_context() -> ToolExecutionContext | None:
    """Return the active tool context, if the caller installed one."""
    return _current_context.get()


def set_tool_context(ctx: ToolExecutionContext | None) -> contextvars.Token[ToolExecutionContext | None] | None:
    """Install a tool context and return a token for reset."""
    if ctx is None:
        return None
    return _current_context.set(ctx)


def reset_tool_context(token: contextvars.Token[ToolExecutionContext | None] | None) -> None:
    """Reset a previously installed tool context."""
    if token is not None:
        _current_context.reset(token)


def resolve_workspace_path(value: Any, *, default: str = ".") -> Path:
    """Resolve a tool path under the active workspace when it is relative."""
    raw = str(value if value not in (None, "") else default)
    path = Path(raw).expanduser()
    ctx = current_tool_context()
    if ctx is not None and not path.is_absolute():
        path = ctx.workspace_root / path
    return path.resolve()


def is_within_allowed_roots(path: Path, ctx: ToolExecutionContext | None = None) -> bool:
    """Return whether ``path`` is inside the active allowed roots."""
    context = current_tool_context() if ctx is None else ctx
    if context is None:
        return True
    resolved = path.expanduser().resolve()
    for root in context.allowed_roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def leapflow_managed_hint(path: Path) -> str:
    """Return a redirect hint when ``path`` is LeapFlow's own managed state.

    A refusal that only says "outside the workspace" leaves the model to guess
    another path, which is how a config change turns into a sequence of blocked
    probes. Classification comes from the layout descriptor rather than string
    matching, so it follows the path tree instead of duplicating it.

    Public because the shell gate refuses the same targets through a different
    entry point; a hint that only the file tools emit would leave the shell path
    telling the model nothing about what to use instead.
    """
    try:
        from leapflow.config import get_settings

        layout = getattr(get_settings(), "layout", None)
        if layout is None:
            return ""
        descriptor = layout.describe_path(path)
    except Exception:  # noqa: BLE001 - a hint must never break the refusal itself
        return ""

    category = str(getattr(descriptor, "category", "") or "")
    if category in {"config", "mcp_config", "workspace_manifest"}:
        return (
            " This is LeapFlow's own configuration: use the config_list / config_get / "
            "config_set tools instead of reading or editing these files."
        )
    if category == "secret_vault":
        return (
            " This is LeapFlow's credential vault and is never readable as a file: "
            "set credentials with config_set (e.g. key='llm.api_key')."
        )
    return ""


def workspace_scope_error(path: Path, *, operation: str) -> dict[str, Any] | None:
    """Return a structured error when ``path`` escapes the active workspace.

    The workspace boundary is a hard gate evaluated at the tool entry point: it
    is deliberately not routed through ApprovalGate, so the message must not
    imply that approving something will unblock it.
    """
    ctx = current_tool_context()
    if ctx is None or is_within_allowed_roots(path, ctx):
        return None
    hint = leapflow_managed_hint(path)
    return {
        "ok": False,
        "error": (
            f"{operation} path is outside the active workspace. "
            f"Resolved path: {path}; workspace root: {ctx.workspace_root}. "
            "This boundary cannot be lifted by approval; work inside the workspace, "
            "or ask the user to open a session in that directory." + hint
        ),
        "error_type": "outside_workspace",
        "retryable": False,
        "workspace_root": str(ctx.workspace_root),
        "allowed_roots": [str(root) for root in ctx.allowed_roots],
        "resolved_path": str(path),
        "session_id": ctx.session_id,
    }
