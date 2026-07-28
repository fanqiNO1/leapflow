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


def workspace_scope_error(path: Path, *, operation: str) -> dict[str, Any] | None:
    """Return a structured error when ``path`` escapes the active workspace."""
    ctx = current_tool_context()
    if ctx is None or is_within_allowed_roots(path, ctx):
        return None
    return {
        "ok": False,
        "error": (
            f"{operation} path is outside the active workspace. "
            f"Resolved path: {path}; workspace root: {ctx.workspace_root}. "
            "Open a TUI in that workspace or ask explicitly for an external path with approval."
        ),
        "error_type": "outside_workspace",
        "retryable": False,
        "workspace_root": str(ctx.workspace_root),
        "allowed_roots": [str(root) for root in ctx.allowed_roots],
        "resolved_path": str(path),
        "session_id": ctx.session_id,
    }
