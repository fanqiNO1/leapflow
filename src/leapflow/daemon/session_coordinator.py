"""Manages session lifecycle: create, resume, history, analysis, artifacts.

Extracted from service.py (Phase 2.3) to keep RuntimeLeapService focused on
orchestration while SessionCoordinator owns all session management logic.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_SESSION_ARTIFACTS = 5
_MAX_SESSION_ARTIFACT_CHARS = 6000
_MAX_SESSION_ARTIFACT_TOTAL_CHARS = 16000
_PATH_RE = re.compile(r'(?P<key>path|file_path)["\'=:\s]+(?P<value>[^"\'\n|]+)')

_SESSION_ANALYSIS_SYSTEM = (
    "You are a session analyst writing FOR THE USER (not for the agent). Read the "
    "conversation transcript and return STRICT JSON only, with keys: story (a "
    "user-facing narrative of the user's goals, findings, and outcomes — NOT a "
    "replay of the agent's tool calls), insights (array of {title, summary, "
    "severity in [info,notable,alert], kind in [finding,process]}), decisions "
    "(array of strings), action_items (array of strings), open_questions (array "
    "of strings), entities (array of strings), next_prompts (array of strings), "
    "process_notes (array of strings), series_intents (array of {id, label, unit, "
    "kind in [line,area,ohlc,distribution]}). "
    "DE-WEIGHT the agent's own mechanics: tool usage, failures, retries, auth "
    "errors, and script fixes are LOW-SIGNAL process. Omit them, or fold at most "
    "one into insights with kind='process' and severity='info'; never emit them "
    "as decisions or action_items unless the USER must act (e.g. provide an API "
    "key). Put unavoidable process remarks in process_notes. In series_intents, "
    "only NAME chart-worthy quantitative series that are actually present in the "
    "data (labels/units); do NOT invent numbers — numeric values are extracted "
    "separately. If a Session file artifacts section is present, treat artifact "
    "contents as first-class evidence. Do not wrap the JSON in prose or code fences."
)

_SESSION_SALIENCE_SYSTEM = (
    "Answer with only YES or NO: does the latest conversation contain a new decision, "
    "a topic shift, or a new action item that would justify refreshing an analysis "
    "dashboard?"
)


class SessionCoordinator:
    """Manages session lifecycle: create, resume, history, analysis, artifacts."""

    def __init__(self) -> None:
        self._session_registry: Any | None = None

    # ── Registry ──────────────────────────────────────────────────────────

    def ensure_registry(self, base_engine: Any, settings: Any) -> Any:
        """Lazily build the per-session engine registry around the base engine.

        The first session reuses ``base_engine`` (single-session daemon
        unchanged); additional sessions get isolated engines via the P3-1
        factory with a fresh working memory of the same capacity.
        """
        if self._session_registry is None:
            from leapflow.daemon.session_registry import SessionRegistry
            from leapflow.engine.session_factory import build_session_engine
            from leapflow.memory import WorkingMemoryProvider

            base_wm = getattr(base_engine, "_wm", None)
            max_tokens = int(getattr(base_wm, "_max_tokens", 8192) or 8192)
            self._session_registry = SessionRegistry(
                base_engine=base_engine,
                build_engine=lambda base, sid, wm, workspace_root: build_session_engine(
                    base,
                    session_id=sid,
                    working_memory=wm,
                    workspace_root=workspace_root,
                ),
                build_working_memory=lambda: WorkingMemoryProvider(max_tokens=max_tokens),
                max_sessions=int(getattr(settings, "daemon_max_live_sessions", 16) or 16),
                idle_ttl_s=float(getattr(settings, "daemon_session_idle_ttl_s", 1800.0) or 1800.0),
            )
        return self._session_registry

    @property
    def registry(self) -> Any:
        """Access the session registry (may be None if not initialized)."""
        return self._session_registry

    # ── Create / Resume ───────────────────────────────────────────────────

    async def create(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        """Create a new session."""
        session_id = getattr(ctx.session, "session_id", "") if ctx.session else ""
        return {"session_id": str(session_id), "created": bool(session_id), **kwargs}

    async def resume(self, ctx: Any, session_id: str) -> dict[str, Any]:
        """Resume an existing session."""
        engine = getattr(ctx, "engine", None)
        found = bool(engine and engine.load_session(session_id))
        current = getattr(engine, "_current_session_id", "") if engine else ""
        return {"found": found, "session_id": str(current or session_id)}

    # ── History ───────────────────────────────────────────────────────────

    async def get_history(self, ctx: Any, settings: Any, limit: int = 200) -> dict[str, Any]:
        """Get session message history with token stats."""
        if ctx is None:
            return {"session_id": "", "turn_count": 0, "token_count": 0, "messages": [], "artifacts": []}
        engine = getattr(ctx, "engine", None)
        session_id = getattr(engine, "_current_session_id", "") if engine else ""
        messages: list[dict[str, Any]] = []
        if engine is not None:
            wm = getattr(engine, "_wm", None)
            if wm is not None and hasattr(wm, "as_chat_messages"):
                try:
                    messages = [dict(m) for m in wm.as_chat_messages() if isinstance(m, dict)]
                except Exception:
                    messages = []
        store_messages = self._session_store_messages(ctx, session_id, limit=int(limit))
        if store_messages:
            if not messages:
                messages = store_messages
            else:
                messages.extend(m for m in store_messages if m.get("role") == "tool")
        normalized = [
            {
                "role": str(m.get("role", "")),
                "content": str(m.get("content", "")),
                "tool_name": str(m.get("tool_name", "") or ""),
                "created_at": float(m.get("created_at", 0.0) or 0.0),
            }
            for m in messages
        ][-int(limit):]
        workspace = self._workspace_root(ctx, settings)
        artifacts = self._collect_session_artifacts(session_id, store_messages or normalized, workspace)
        return {
            "session_id": session_id,
            "turn_count": int(getattr(engine, "turn_count", 0)) if engine else 0,
            "token_count": int(getattr(engine, "context_token_count", 0)) if engine else 0,
            "messages": normalized,
            "artifacts": artifacts,
        }

    # ── Analysis ──────────────────────────────────────────────────────────

    async def analyze(self, monitors: Any, ctx: Any, settings: Any) -> dict[str, Any]:
        """Trigger LLM session analysis via the monitor subsystem."""
        if monitors is None:
            return {"ok": False, "error": "monitor runtime unavailable"}
        watch_id = await self._ensure_session_watch(monitors, settings)
        result = await monitors.run_watch_once(watch_id, force=True)
        return {"ok": bool(result.get("ok", True)), "watch_id": watch_id, "result": result}

    async def analyze_llm(
        self,
        ctx: Any,
        messages: list[dict[str, Any]],
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run LLM-based session analysis on a message transcript."""
        base: dict[str, Any] = {
            "story": "", "insights": [], "decisions": [], "action_items": [],
            "open_questions": [], "entities": [], "next_prompts": [],
            "process_notes": [], "series_intents": [], "usage": {},
        }
        llm = getattr(ctx, "llm", None) if ctx is not None else None
        if llm is None or not messages:
            return base
        transcript = "\n".join(
            f"{m.get('role', '')}: {str(m.get('content', ''))[:500]}" for m in messages[-40:]
        )[:12000]
        artifact_block = self._format_artifact_context(artifacts or [])
        user_content = transcript if not artifact_block else f"{transcript}\n\n## Session file artifacts\n{artifact_block}"
        prompt = [
            {"role": "system", "content": _SESSION_ANALYSIS_SYSTEM},
            {"role": "user", "content": user_content[:18000]},
        ]
        try:
            response = await llm.achat(prompt, stream=False)
            data = _parse_session_json(getattr(response, "content", ""))
        except Exception:
            logger.debug("daemon: session analysis LLM call failed", exc_info=True)
            return base
        if isinstance(data, dict):
            for key in base:
                if key != "usage" and key in data:
                    base[key] = data[key]
        return base

    async def should_refresh(self, ctx: Any, messages: list[dict[str, Any]]) -> bool:
        """Determine if recent conversation warrants a dashboard refresh."""
        llm = getattr(ctx, "llm", None) if ctx is not None else None
        if llm is None or not messages:
            return False
        tail = "\n".join(
            f"{m.get('role', '')}: {str(m.get('content', ''))[:200]}" for m in messages[-6:]
        )[:2000]
        prompt = [
            {"role": "system", "content": _SESSION_SALIENCE_SYSTEM},
            {"role": "user", "content": tail},
        ]
        try:
            response = await llm.achat(prompt, stream=False)
            return str(getattr(response, "content", "")).strip().upper().startswith("Y")
        except Exception:
            return False

    # ── Private helpers ───────────────────────────────────────────────────

    async def _ensure_session_watch(self, monitors: Any, settings: Any) -> str:
        """Ensure the session analysis watch exists and return its id."""
        from leapflow.monitor.session_producer import ensure_session_watch, session_watch_params

        return await ensure_session_watch(monitors, params=session_watch_params(settings))

    def _session_store_messages(self, ctx: Any, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Query persisted messages from the conversation store."""
        store = getattr(ctx, "_conversation_store", None) if ctx is not None else None
        if store is None or not session_id:
            return []
        try:
            rows = store.get_messages(session_id, limit=int(limit))
        except Exception:
            logger.debug("daemon: session store messages unavailable", exc_info=True)
            return []
        return [self._conversation_message_to_dict(row) for row in rows]

    @staticmethod
    def _conversation_message_to_dict(message: Any) -> dict[str, Any]:
        if isinstance(message, dict):
            return dict(message)
        return {
            "role": str(getattr(message, "role", "")),
            "content": str(getattr(message, "content", "")),
            "tool_name": str(getattr(message, "tool_name", "") or ""),
            "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
            "created_at": float(getattr(message, "created_at", 0.0) or 0.0),
            "metadata": dict(getattr(message, "metadata", {}) or {}),
        }

    def _collect_session_artifacts(
        self, session_id: str, messages: list[dict[str, Any]], workspace: Path
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        candidates: list[tuple[str, dict[str, Any]]] = []
        for message in messages:
            if str(message.get("role", "")) != "tool":
                continue
            tool_name = str(message.get("tool_name", "") or "")
            if tool_name and tool_name not in {"file_write", "write_file"}:
                continue
            for path in self._extract_artifact_paths(message):
                candidates.append((path, message))
        seen: set[str] = set()
        artifacts: list[dict[str, Any]] = []
        total_chars = 0
        for raw_path, message in reversed(candidates):
            if len(artifacts) >= _MAX_SESSION_ARTIFACTS:
                break
            artifact = self._read_session_artifact(raw_path, workspace, message)
            key = str(artifact.get("path") or raw_path)
            if key in seen:
                continue
            seen.add(key)
            if artifact.get("status") == "included":
                content = str(artifact.get("content_excerpt", ""))
                remaining = max(0, _MAX_SESSION_ARTIFACT_TOTAL_CHARS - total_chars)
                if len(content) > remaining:
                    artifact["content_excerpt"] = content[:remaining]
                    artifact["truncated"] = True
                    artifact["reason"] = "artifact context budget reached"
                total_chars += len(str(artifact.get("content_excerpt", "")))
            artifacts.append(artifact)
        artifacts.reverse()
        return artifacts

    @staticmethod
    def _extract_artifact_paths(message: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        payloads = [message.get("content", ""), message.get("metadata", {})]
        for payload in payloads:
            if isinstance(payload, dict):
                for key in ("path", "file_path"):
                    if payload.get(key):
                        paths.append(str(payload[key]))
                continue
            text = str(payload or "")
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    for key in ("path", "file_path"):
                        if data.get(key):
                            paths.append(str(data[key]))
            except Exception:
                pass
            for match in _PATH_RE.finditer(text):
                value = match.group("value").strip().strip(",}")
                if value:
                    paths.append(value)
        return paths

    @staticmethod
    def _read_session_artifact(raw_path: str, workspace: Path, message: dict[str, Any]) -> dict[str, Any]:
        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = workspace / target
        try:
            target = target.resolve()
        except OSError:
            target = target.absolute()
        base = {
            "path": str(target),
            "name": target.name,
            "source": "file_write",
            "tool_call_id": str(message.get("tool_call_id", "") or ""),
            "status": "skipped",
        }
        try:
            target.relative_to(workspace)
        except ValueError:
            return {**base, "reason": "outside workspace boundary"}
        try:
            from leapflow.security.path_sensitivity import classify_path_sensitivity
            sensitivity = classify_path_sensitivity(target)
        except Exception:
            sensitivity = None
        if sensitivity is not None:
            base.update({"sensitivity": sensitivity.category, "sensitivity_level": sensitivity.level})
            if not sensitivity.readable or sensitivity.requires_approval or sensitivity.redact_on_read:
                return {**base, "reason": f"sensitive path ({sensitivity.category}) not read in background"}
        if not target.exists() or not target.is_file():
            return {**base, "reason": "file no longer exists"}
        try:
            stat = target.stat()
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {**base, "reason": f"read failed: {exc}"}
        truncated = len(content) > _MAX_SESSION_ARTIFACT_CHARS
        excerpt = content[:_MAX_SESSION_ARTIFACT_CHARS]
        try:
            from leapflow.security.redact import redact_sensitive_text
            excerpt = redact_sensitive_text(excerpt, file_read=bool(getattr(sensitivity, "redact_on_read", False)))
        except Exception:
            pass
        return {
            **base,
            "status": "included",
            "size": int(stat.st_size),
            "mtime": float(stat.st_mtime),
            "content_excerpt": excerpt,
            "truncated": truncated,
        }

    @staticmethod
    def _format_artifact_context(artifacts: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for artifact in artifacts:
            status = str(artifact.get("status", ""))
            path = str(artifact.get("path", ""))
            if status != "included":
                lines.append(f"- SKIPPED {path}: {artifact.get('reason', 'not included')}")
                continue
            excerpt = str(artifact.get("content_excerpt", ""))[:_MAX_SESSION_ARTIFACT_CHARS]
            truncated = " (truncated)" if artifact.get("truncated") else ""
            lines.append(f"- FILE {path}{truncated}\n```text\n{excerpt}\n```")
        return "\n".join(lines)

    @staticmethod
    def _workspace_root(ctx: Any, settings: Any) -> Path:
        s = getattr(ctx, "settings", settings) if ctx is not None else settings
        return Path(str(getattr(s, "workspace_root", os.getcwd()))).expanduser().resolve()


def _parse_session_json(content: str) -> Any:
    """Best-effort extraction of a JSON object from an LLM response."""
    import json as _json

    text = str(content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first, rest = text.split("\n", 1)
            if first.strip().lower().startswith("json"):
                text = rest
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        return _json.loads(text)
    except Exception:
        return None
