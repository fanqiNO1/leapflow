"""Shell command execution with timeout, safety, and output redaction.

All handlers follow the ToolBridge convention: receive params dict, return result dict.
Safety layers:
1. Hardline block: always-blocked destructive patterns (rm -rf /, fork bomb, etc.)
2. Dangerous detection: patterns requiring user confirmation (sudo, chmod, etc.)
3. Output redaction: secrets stripped from stdout/stderr before returning to LLM
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import signal
from pathlib import Path
from typing import Any, Dict, FrozenSet, Protocol, runtime_checkable

from leapflow.tools.execution_context import (
    current_tool_context,
    is_within_allowed_roots,
    leapflow_managed_hint,
    resolve_workspace_path,
    workspace_scope_error,
)

logger = logging.getLogger(__name__)

_MAX_STDOUT = 10_000
_MAX_STDERR = 5_000
_DEFAULT_TIMEOUT = 30.0
# Internal ceiling for the shell process timeout. Raised from the original
# hard-coded 120 s; injectable at startup via set_max_shell_timeout so
# long-running builds and tests are no longer unconditionally killed.
_max_shell_timeout_s: float = 300.0


def set_max_shell_timeout(seconds: float) -> None:
    """Set the maximum shell-process timeout (clamping ceiling, not the default).

    Called at engine / application startup from settings so operators can
    tune the ceiling without touching source code.
    """
    global _max_shell_timeout_s
    _max_shell_timeout_s = max(10.0, float(seconds))


@runtime_checkable
class CommandApprovalGate(Protocol):
    """Protocol for command approval (injectable, no hardcoded behavior)."""

    async def check(self, command: str) -> bool:
        """Return True if the command is approved, False to block."""
        ...


@runtime_checkable
class ActionApprovalEvaluator(Protocol):
    """Protocol for structured action approval evaluators."""

    async def evaluate(self, action: Any) -> Any:
        """Return an approval result for a structured action."""
        ...


# Hardline blocks: NEVER bypassed regardless of approval
_HARDLINE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+.*-[^\s]*r[^\s]*f|\brm\s+.*-[^\s]*f[^\s]*r", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*$", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+.*of=/dev/", re.IGNORECASE),
    re.compile(r":()\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),  # fork bomb
    re.compile(r"\b>\.?/dev/[sh]d[a-z]", re.IGNORECASE),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", re.IGNORECASE),
]

# Dangerous patterns: blocked by default, approvable via CommandApprovalGate
_DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\bchmod\s+[0-7]*7[0-7]*\b", re.IGNORECASE),
    re.compile(r"\bchown\b", re.IGNORECASE),
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh", re.IGNORECASE),
    re.compile(r"\bwget\b.*\|\s*(ba)?sh", re.IGNORECASE),
    re.compile(r"\b(?:python[23]?|perl|ruby|node|bash|sh|zsh|ksh)\s+<<", re.IGNORECASE),
    re.compile(r"\b(pip|npm|brew)\s+install\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+.*--force\b", re.IGNORECASE),
    re.compile(r"\brm\s+-r\b", re.IGNORECASE),
    re.compile(r"\bkill\s+-9\b", re.IGNORECASE),
    re.compile(r"\biptables\b|\bnft\b", re.IGNORECASE),
    re.compile(r"\bsystemctl\s+(stop|disable|mask)\b", re.IGNORECASE),
]

# CWD paths that should never be used for shell execution
_BLOCKED_CWD_PREFIXES: FrozenSet[str] = frozenset({
    "/System", "/usr", "/bin", "/sbin", "/var/root",
})

# Module-level approval gate (injected by orchestrator; None = auto-deny dangerous)
_approval_gate: CommandApprovalGate | None = None


def set_approval_gate(gate: CommandApprovalGate | None) -> None:
    """Install a command approval gate for dangerous-command review."""
    global _approval_gate
    _approval_gate = gate


def _is_hardline_blocked(command: str) -> bool:
    return any(p.search(command) for p in _HARDLINE_PATTERNS)


def _is_dangerous(command: str) -> bool:
    return any(p.search(command) for p in _DANGEROUS_PATTERNS)


def _is_cwd_blocked(cwd: str | None) -> bool:
    if not cwd:
        return False
    resolved = os.path.realpath(os.path.expanduser(cwd))
    return any(resolved.startswith(prefix) for prefix in _BLOCKED_CWD_PREFIXES)


async def _approve_command(command: str, cwd: str | None) -> tuple[bool, str]:
    if _approval_gate is None:
        return False, "Dangerous command blocked (no approval gate configured)"
    try:
        if isinstance(_approval_gate, ActionApprovalEvaluator):
            from leapflow.security.actions import ActionDescriptor

            result = await _approval_gate.evaluate(ActionDescriptor.shell(command, cwd=cwd))
            if getattr(result, "approved", False):
                return True, ""
            message = str(getattr(result, "denial_message", "") or "Dangerous command requires approval (denied)")
            return False, message
        approved = await _approval_gate.check(command)
        return approved, "" if approved else "Dangerous command requires approval (denied)"
    except Exception:
        logger.debug("shell approval check failed", exc_info=True)
        return False, "Dangerous command requires approval (denied)"


def _expand_operand(token: str) -> str:
    """Return the inspectable path operand carried by a shell token.

    Strips a leading ``--flag=`` and expands variable references, because the
    shell expands them at execution time: ``$HOME/x`` and ``/Users/me/x`` reach
    the same file, so a gate comparing raw text would guard the spelling rather
    than the target. Unset variables are left literal by ``expandvars`` and then
    fail the prefix/traversal tests below, which is the safe direction.
    """
    candidate = token.split("=", 1)[-1]
    return os.path.expandvars(candidate) if "$" in candidate else candidate


def _has_parent_traversal(operand: str) -> bool:
    """Return whether a relative operand walks upward out of its base directory."""
    return ".." in Path(operand).parts


def _command_workspace_escape(command: str, cwd: Path | None = None) -> dict[str, Any] | None:
    """Reject path operands that resolve outside the active workspace.

    Shell is intentionally a broad escape hatch, so this stays a conservative
    guard rather than a full shell parser. It does normalize what the shell
    itself would expand before deciding: variable references (``$HOME``,
    ``${HOME}``) and relative traversal (``../..``) address exactly the same
    files as an absolute path, so inspecting only literal ``/`` or ``~`` tokens
    would let a daemon-backed turn read another TUI's workspace through a
    different spelling.

    Command substitution (``$(...)``, backticks) and similar indirection remain
    out of reach by design; the hardline patterns, approval gate, and execution
    ledger are the defenses there.
    """
    ctx = current_tool_context()
    if ctx is None:
        return None
    base = cwd if cwd is not None else ctx.workspace_root
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if token.startswith(("http://", "https://")):
            continue
        operand = _expand_operand(token)
        if not operand:
            continue
        if operand.startswith(("/", "~")):
            path = Path(operand)
        elif _has_parent_traversal(operand):
            path = base / operand
        else:
            continue
        resolved = path.expanduser().resolve()
        if not is_within_allowed_roots(resolved, ctx):
            return {
                "ok": False,
                "error": (
                    "shell_run command references a path outside the active workspace. "
                    f"Path: {resolved}; workspace root: {ctx.workspace_root}. "
                    "This boundary cannot be lifted by approval; work inside the workspace, "
                    "or ask the user to open a session in that directory."
                    + leapflow_managed_hint(resolved)
                ),
                "error_type": "outside_workspace",
                "retryable": False,
                "workspace_root": str(ctx.workspace_root),
                "resolved_path": str(resolved),
                "operand": token,
                "session_id": ctx.session_id,
            }
    return None


async def shell_run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a shell command with timeout protection and safety layers."""
    command = params.get("command", "")
    raw_cwd = params.get("cwd") or None
    cwd_path = resolve_workspace_path(raw_cwd, default=".") if raw_cwd else None
    if cwd_path is None:
        ctx = current_tool_context()
        cwd_path = ctx.workspace_root if ctx is not None else None
    cwd = str(cwd_path) if cwd_path is not None else None
    timeout = min(float(params.get("timeout", _DEFAULT_TIMEOUT)), _max_shell_timeout_s)

    if not command:
        return {"ok": False, "error": "Missing required parameter: command"}

    if _is_hardline_blocked(command):
        return {"ok": False, "error": "Command blocked by safety policy (destructive pattern detected)"}

    if _is_cwd_blocked(cwd):
        return {"ok": False, "error": f"Working directory blocked by safety policy: {cwd}"}

    if cwd_path is not None:
        scope_error = workspace_scope_error(cwd_path, operation="shell_run cwd")
        if scope_error:
            return scope_error

    command_scope_error = _command_workspace_escape(str(command), cwd=cwd_path)
    if command_scope_error:
        return command_scope_error

    if _is_dangerous(command):
        approved, message = await _approve_command(command, cwd)
        if not approved:
            return {"ok": False, "error": message}

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
        stdout_text = stdout_bytes.decode(errors="replace")[:_MAX_STDOUT]
        stderr_text = stderr_bytes.decode(errors="replace")[:_MAX_STDERR]

        # Redact secrets from output before returning to LLM
        try:
            from leapflow.security.redact import redact_sensitive_text
            stdout_text = redact_sensitive_text(stdout_text)
            stderr_text = redact_sensitive_text(stderr_text)
        except ImportError:
            pass

        result: Dict[str, Any] = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "cwd": cwd,
        }
        if proc.returncode != 0:
            # Surface a concrete error: the tail of stderr holds the real cause
            # (e.g. the last line of a Python traceback), so downstream never has
            # to fall back to a bare "unknown error".
            detail = stderr_text.strip() or stdout_text.strip()
            result["error"] = detail[-800:] if detail else f"Command failed with exit code {proc.returncode}"
        return result
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # type: ignore[possibly-undefined]
        except (ProcessLookupError, OSError):
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except ProcessLookupError:
                pass
        return {"ok": False, "error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
