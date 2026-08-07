"""Best-effort source-tree fingerprint for long-lived-process staleness checks.

Long-lived local processes (the ``leapd`` daemon, the LeapBoard web server)
keep running whatever code was imported at process start; editing a source
file on disk does not hot-reload that in-memory logic (only per-request-read
YAML/JS/CSS reflect edits immediately). This module lets a process capture its
own fingerprint once at startup (:func:`capture_build_info`), then later ask
whether the source tree has moved on since (:func:`is_stale`) via a *fresh*
subprocess call — which always reflects the current disk state, unlike the
caller's own possibly-ancient Python import cache. Callers use this to tell a
developer "this process is stale, restart it" instead of silently serving
outdated behavior that looks like a code defect (see AGENTS.md, "User-Centric
Reliability").

Best-effort by design: outside a git checkout (e.g. a packaged release install)
there is no fingerprint to compare, so every function degrades to ``None``
("unknown") rather than a false verdict — never raises, never blocks longer
than ``_GIT_TIMEOUT_S``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from leapflow.version import __version__

# Short timeout: this runs on request paths (dashboard HTTP handlers, daemon
# status RPC), so a hung git process must not hang the caller.
_GIT_TIMEOUT_S = 1.5


def _repo_root() -> Path:
    """Best-effort source checkout root for git fingerprinting.

    Source checkouts use ``.../src/leapflow/utils/build_info.py``, but packaged
    installs may live under ``site-packages/leapflow`` without any repository
    metadata. Walk upward looking for a real git checkout or a source-tree
    ``pyproject.toml``; otherwise return the package directory. A non-checkout
    cwd simply makes the later git calls return None (unknown), which is the
    intended graceful degradation.
    """
    try:
        here = Path(__file__).resolve()
    except (OSError, RuntimeError, ValueError):
        return Path.cwd()
    for candidate in here.parents:
        if (candidate / ".git").exists():
            return candidate
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "leapflow").exists():
            return candidate
    try:
        return here.parents[1]
    except IndexError:
        return here.parent


def _run_git(args: List[str], cwd: Path) -> Optional[str]:
    """Run a short-lived, read-only git command; None on any failure or timeout."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _fingerprint(cwd: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(commit, dirty_digest)`` for the source tree rooted at ``cwd``.

    ``dirty_digest`` changes with any uncommitted edit (a short hash of
    ``git status --porcelain``, so file contents themselves are never
    retained). Both are None when ``cwd`` is not a git checkout, or git is
    unavailable — callers must treat that as "unknown", never as a verdict.
    """
    commit = _run_git(["rev-parse", "--short=12", "HEAD"], cwd)
    if commit is None:
        return None, None
    porcelain = _run_git(["status", "--porcelain", "--untracked-files=no"], cwd)
    digest = (
        hashlib.sha1(porcelain.encode("utf-8")).hexdigest()[:12]
        if porcelain is not None
        else None
    )
    return commit, digest


@dataclass(frozen=True)
class BuildInfo:
    """A process's captured identity: package version + source-tree fingerprint."""

    version: str
    commit: Optional[str]
    dirty_digest: Optional[str]
    pid: int
    started_at: float

    def to_dict(self) -> dict:
        """Serialize to a plain, JSON-safe dict for RPC/HTTP transport."""
        return {
            "version": self.version,
            "commit": self.commit or "",
            "dirty_digest": self.dirty_digest or "",
            "pid": self.pid,
            "started_at": self.started_at,
        }


def capture_build_info() -> BuildInfo:
    """Capture this process's build fingerprint once, at startup."""
    commit, digest = _fingerprint(_repo_root())
    return BuildInfo(
        version=__version__,
        commit=commit,
        dirty_digest=digest,
        pid=os.getpid(),
        started_at=time.time(),
    )


def is_stale(captured: BuildInfo) -> Optional[bool]:
    """Return True when ``captured`` no longer matches the current source tree.

    Always re-derives the *current* fingerprint via a fresh subprocess call, so
    the answer reflects disk state right now — regardless of how long this
    calling process itself has been running. None means "unknown" (no git
    checkout found); render that distinctly from a definite fresh/stale verdict.
    """
    if captured.commit is None:
        return None
    commit, digest = _fingerprint(_repo_root())
    if commit is None:
        return None
    return commit != captured.commit or digest != captured.dirty_digest


__all__ = ["BuildInfo", "capture_build_info", "is_stale"]
