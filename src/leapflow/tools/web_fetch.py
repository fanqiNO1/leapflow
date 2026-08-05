"""web_fetch — first-class read-only HTTP access for the agent loop.

Without this tool the only way to read a URL is ``shell_run`` with a hand-written
``curl | python3 -c`` pipeline. That path is classified ``external_side_effect``,
so a plain GET picks up side-effect batch stopping, session-scoped deduplication,
and "this may already have taken effect" retry guidance — none of which describe
reading a web page. Worse, HTTP failures surface as whatever the improvised
pipeline printed (typically a Python traceback) instead of a status code.

``web_fetch`` is therefore declared ``read_only``: retries are safe, the batch
gate does not fire, and failures come back as structured status information.

Two transports exist and both are load-bearing. httpx is the default (async
native, structured status/headers). curl is the fallback for environments where
the Python TLS stack cannot complete a handshake that the system curl can — most
commonly corporate TLS interception, where certifi lacks the intercepting CA but
the system trust store has it. curl is invoked with an argv list, never through a
shell, so the quoting and injection hazards of the pipeline it replaces are gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Protocol, runtime_checkable

from leapflow.security.network import NetworkTarget, UrlRejected, classify_url
from leapflow.tools.execution_context import current_tool_context
from leapflow.tools.web_extract import (
    KIND_BINARY,
    KIND_HTML,
    KIND_JSON,
    decode_json,
    extract_html,
    kind_for_content_type,
    select_path,
)

logger = logging.getLogger(__name__)

# A browser-style default: many CDNs answer 429/403 to curl's or a library's own
# user agent, which turns an ordinary read into a failure the model then has to
# debug. Overridable through `web.user_agent` for deployments that need to
# identify themselves honestly.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "application/json;q=0.9,text/plain;q=0.8,*/*;q=0.5"
)
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Statuses that mean "this client was refused" rather than "this resource does not
# exist". Observed against a real CDN edge: the same URL with the same user agent
# answers 429 intermittently, and which transport is refused varies between runs,
# so the refusal is not attributable to the user agent alone. Retrying the same
# transport is one lever; handing the request to the next transport is another,
# and it costs a single extra attempt. Anything outside this set is reported as-is.
_FAILOVER_STATUSES = frozenset({403, 429})
_MAX_RETRY_SLEEP_S = 8.0
_TEXT_BODY_LIMIT = 200_000
_CACHE_CATEGORY = "web_fetch"

# Module-level gate, installed by the CLI/daemon exactly like the shell and
# config gates. ``requires_approval`` in the tool's x_leapflow block only informs
# capability disclosure and does not gate execution, so the handler must consult
# this explicitly.
_approval_gate: Any = None


def set_web_approval_gate(gate: Any) -> None:
    """Install the approval gate consulted before a sensitive fetch."""
    global _approval_gate
    _approval_gate = gate


def get_web_approval_gate() -> Any:
    """Return the installed web approval gate (or ``None``)."""
    return _approval_gate


@dataclass(frozen=True)
class FetchRequest:
    """An outbound read, fully resolved from params plus settings."""

    url: str
    timeout_s: float
    max_bytes: int
    max_redirects: int
    user_agent: str


@dataclass(frozen=True)
class FetchOutcome:
    """A raw transport result, before content extraction."""

    status: int
    final_url: str
    content_type: str
    body: bytes
    truncated: bool
    transport: str
    elapsed_ms: int


class TransportUnavailable(RuntimeError):
    """Raised when a transport cannot run in this environment."""


class TransportFailure(RuntimeError):
    """Raised when a request failed before any HTTP status was received."""

    def __init__(self, error_type: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


@runtime_checkable
class WebTransport(Protocol):
    """Performs one bounded HTTP read."""

    name: str

    def available(self) -> bool:
        """Whether this transport can run in the current environment."""
        ...

    async def fetch(self, request: FetchRequest) -> FetchOutcome:
        """Perform the read, raising ``TransportFailure`` on connection errors."""
        ...


def _headers(request: FetchRequest) -> dict[str, str]:
    return {
        "User-Agent": request.user_agent,
        "Accept": _DEFAULT_ACCEPT,
        "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8",
    }


class HttpxTransport:
    """Default transport: async-native with structured status and headers."""

    name = "httpx"

    def available(self) -> bool:
        try:
            import httpx  # noqa: F401
        except ImportError:
            return False
        return True

    async def fetch(self, request: FetchRequest) -> FetchOutcome:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - guarded by available()
            raise TransportUnavailable("httpx is not installed") from exc

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=request.max_redirects,
                timeout=request.timeout_s,
            ) as client:
                async with client.stream("GET", request.url, headers=_headers(request)) as response:
                    chunks: list[bytes] = []
                    size = 0
                    truncated = False
                    # Streamed rather than read whole: the byte cap must hold even
                    # when the server sends no Content-Length.
                    async for chunk in response.aiter_bytes():
                        remaining = request.max_bytes - size
                        if remaining <= 0:
                            truncated = True
                            break
                        if len(chunk) > remaining:
                            chunks.append(chunk[:remaining])
                            truncated = True
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                    return FetchOutcome(
                        status=response.status_code,
                        final_url=str(response.url),
                        content_type=response.headers.get("content-type", ""),
                        body=b"".join(chunks),
                        truncated=truncated,
                        transport=self.name,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
        except httpx.TooManyRedirects as exc:
            raise TransportFailure("too_many_redirects", str(exc), retryable=False) from exc
        except httpx.TimeoutException as exc:
            raise TransportFailure(
                "timeout", f"Request timed out after {request.timeout_s}s", retryable=True
            ) from exc
        except httpx.ConnectError as exc:
            raise TransportFailure("connect_error", str(exc), retryable=True) from exc
        except httpx.HTTPError as exc:
            raise TransportFailure("transport_error", f"{type(exc).__name__}: {exc}", retryable=True) from exc


class CurlTransport:
    """Fallback transport using the system curl through an argv list.

    Earns its place two ways. The Python and system TLS stacks fail differently —
    behind TLS-intercepting proxies curl trusts the injected CA through the OS
    store while httpx (certifi) rejects it — and a CDN that refuses one client
    with 429 frequently serves the other, so the chain has a second thing to try.
    Invoked with ``create_subprocess_exec`` and an argv list, so no shell parses
    the URL.
    """

    name = "curl"
    # curl exit codes worth naming; anything else becomes a generic failure.
    _EXIT_ERRORS = {
        6: ("dns_error", False),
        7: ("connect_error", True),
        28: ("timeout", True),
        35: ("tls_error", False),
        47: ("too_many_redirects", False),
        60: ("tls_error", False),
        63: ("too_large", False),
    }

    def available(self) -> bool:
        import shutil

        return shutil.which("curl") is not None

    async def fetch(self, request: FetchRequest) -> FetchOutcome:
        if not self.available():
            raise TransportUnavailable("curl is not installed")
        # The sentinel separates the body from curl's --write-out metadata on one
        # stream, avoiding a temp file for the body. Split from the right so a
        # body that happens to contain the token cannot shift the metadata.
        sentinel = f"--leapflow-{uuid.uuid4().hex}--"
        argv = [
            "curl", "--silent", "--show-error", "--location", "--compressed",
            "--max-redirs", str(request.max_redirects),
            "--max-time", str(int(max(1, request.timeout_s))),
            "--max-filesize", str(request.max_bytes),
            "--user-agent", request.user_agent,
            "--header", f"Accept: {_DEFAULT_ACCEPT}",
            "--write-out", f"{sentinel}%{{http_code}}\t%{{content_type}}\t%{{url_effective}}",
            request.url,
        ]
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=request.timeout_s + 5
            )
        except asyncio.TimeoutError as exc:
            raise TransportFailure(
                "timeout", f"curl exceeded {request.timeout_s}s", retryable=True
            ) from exc
        except OSError as exc:
            raise TransportUnavailable(f"curl could not be started: {exc}") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode != 0:
            error_type, retryable = self._EXIT_ERRORS.get(
                proc.returncode or 0, ("transport_error", True)
            )
            detail = stderr.decode(errors="replace").strip() or f"curl exit {proc.returncode}"
            raise TransportFailure(error_type, detail, retryable=retryable)

        marker = stdout.rfind(sentinel.encode())
        if marker < 0:
            raise TransportFailure(
                "transport_error", "curl produced no status metadata", retryable=True
            )
        body = stdout[:marker]
        meta = stdout[marker + len(sentinel.encode()):].decode(errors="replace").split("\t")
        status = int(meta[0]) if meta and meta[0].strip().isdigit() else 0
        content_type = meta[1] if len(meta) > 1 else ""
        final_url = meta[2].strip() if len(meta) > 2 else request.url
        truncated = len(body) > request.max_bytes
        return FetchOutcome(
            status=status,
            final_url=final_url or request.url,
            content_type=content_type,
            body=body[: request.max_bytes],
            truncated=truncated,
            transport=self.name,
            elapsed_ms=elapsed_ms,
        )


def transports_for(preference: str) -> tuple[WebTransport, ...]:
    """Return the transport chain honoring the configured preference."""
    httpx_transport = HttpxTransport()
    curl_transport = CurlTransport()
    if preference == "httpx":
        return (httpx_transport,)
    if preference == "curl":
        return (curl_transport,)
    return tuple(t for t in (httpx_transport, curl_transport) if t.available())


async def _approve_fetch(target: NetworkTarget) -> str:
    """Return a denial reason, or ``""`` when the fetch may proceed.

    Fails closed when no gate is installed: an internal target reachable without
    review would let the model read the daemon socket's HTTP neighbors, the local
    dashboard, or cloud instance metadata. The URL is passed as the action detail
    but the grant is keyed on the origin, so approving one page trusts the host
    for the session rather than only that exact URL.
    """
    gate = _approval_gate
    if gate is None:
        return (
            f"{target.origin} resolves to a {target.category} address and needs approval, "
            "but no approval gate is available in this session. Ask the user to fetch it "
            "manually or run this in an interactive session."
        )
    try:
        from leapflow.security.actions import ActionDescriptor

        action = ActionDescriptor.network_fetch(
            target.url,
            origin=target.origin,
            metadata={"tool": "web_fetch", **target.to_metadata()},
        )
        result = await gate.evaluate(action)
    except Exception:  # noqa: BLE001 - a broken gate must not become an open door
        logger.warning("web_fetch: approval evaluation failed; denying", exc_info=True)
        return "Fetch denied: the approval gate could not be consulted."

    if getattr(result, "approved", False):
        return ""
    return str(
        getattr(result, "denial_message", "")
        or getattr(result, "reason", "")
        or f"Fetch denied by approval gate: {target.origin}"
    )


def _decode_text(body: bytes, content_type: str) -> str:
    """Decode a body to text using the declared charset when present."""
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    encodings = [match.group(1)] if match else []
    encodings += ["utf-8", "latin-1"]
    for encoding in encodings:
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _retry_delay(attempt: int) -> float:
    return min(_MAX_RETRY_SLEEP_S, 0.5 * (2 ** attempt))


async def _attempt(
    transport: WebTransport, request: FetchRequest, max_retries: int
) -> tuple[FetchOutcome | None, TransportFailure | None]:
    """Run one transport with bounded retries.

    Returns the outcome, or the failure that ended the attempts. Retrying at all
    is only safe because the tool is read-only; an unrecoverable failure (bad TLS,
    too many redirects) is not retried.
    """
    last_failure: TransportFailure | None = None
    for attempt in range(max_retries + 1):
        try:
            outcome = await transport.fetch(request)
        except TransportUnavailable:
            return None, last_failure
        except TransportFailure as failure:
            last_failure = failure
            if not failure.retryable or attempt >= max_retries:
                return None, failure
            await asyncio.sleep(_retry_delay(attempt))
            continue
        if outcome.status in _RETRY_STATUSES and attempt < max_retries:
            await asyncio.sleep(_retry_delay(attempt))
            continue
        return outcome, None
    return None, last_failure


async def _run_transports(
    request: FetchRequest, preference: str, max_retries: int
) -> FetchOutcome:
    """Fetch through the transport chain, failing over when a client is refused.

    Two escalation paths, deliberately distinct: a *failure* (no HTTP response)
    moves to the next transport, and a *client-rejection status* does too, since a
    refusal that is intermittent per client often clears on the other stack.
    Everything else is returned as-is — an ordinary 404 is the answer, not
    something to re-ask a second transport.
    """
    chain = [item for item in transports_for(preference) if item.available()]
    if not chain:
        raise TransportUnavailable(
            "No usable HTTP transport: install httpx (`pip install httpx`) or curl."
        )
    last_failure: TransportFailure | None = None
    last_outcome: FetchOutcome | None = None
    for index, transport in enumerate(chain):
        outcome, failure = await _attempt(transport, request, max_retries)
        if failure is not None:
            last_failure = failure
            continue
        if outcome is None:
            continue
        last_outcome = outcome
        if outcome.status in _FAILOVER_STATUSES and index + 1 < len(chain):
            logger.info(
                "web_fetch: %s refused with %d; trying %s",
                transport.name,
                outcome.status,
                chain[index + 1].name,
            )
            continue
        return outcome
    if last_outcome is not None:
        return last_outcome
    if last_failure is not None:
        raise last_failure
    raise TransportUnavailable("No usable HTTP transport for this request.")


def _redact(text: str) -> str:
    try:
        from leapflow.security.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except ImportError:  # pragma: no cover - redaction ships with the package
        return text


def _settings() -> Any:
    from leapflow.config import get_settings

    return get_settings()


def _cache_slot(url: str, settings: Any) -> tuple[Path, str, str] | None:
    """Return ``(body_path, workspace_id, session_id)``, or ``None`` if unavailable.

    Session-scoped and non-syncable: a fetched page is reproducible and may carry
    per-session context, so it neither belongs to the profile nor should leave the
    machine. The session scope requires a workspace id too, which also keeps one
    TUI's fetches out of another's cache. Paths come from ``CacheLayout`` rather
    than being assembled here, and any failure degrades to "no cache" instead of
    failing the fetch.
    """
    ctx = current_tool_context()
    session_id = getattr(ctx, "session_id", "") or "default"
    try:
        from leapflow.cache.manager import CacheManager, CacheScope
        from leapflow.layout import workspace_id_for_path

        workspace_root = getattr(ctx, "workspace_root", None) or settings.workspace_root
        workspace_id = workspace_id_for_path(Path(workspace_root))
        manager = CacheManager(settings.profile_layout.cache, profile_id=settings.profile)
        directory = manager.path(
            scope=CacheScope.SESSION,
            category=_CACHE_CATEGORY,
            workspace_id=workspace_id,
            session_id=session_id,
            source="body",
        )
    except Exception:  # noqa: BLE001 - caching is an optimization, never a requirement
        logger.debug("web_fetch: cache unavailable", exc_info=True)
        return None
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return directory / f"{digest}.bin", workspace_id, session_id


def _cache_meta_path(body_path: Path) -> Path:
    return body_path.with_suffix(".meta.json")


def _read_cache(url: str, settings: Any) -> FetchOutcome | None:
    """Return a cached outcome for ``url`` when one is present and fresh."""
    ttl = float(getattr(settings, "web_cache_ttl_s", 0) or 0)
    if ttl <= 0:
        return None
    slot = _cache_slot(url, settings)
    if slot is None:
        return None
    body_path, _workspace_id, _session_id = slot
    meta_path = _cache_meta_path(body_path)
    try:
        if not (body_path.exists() and meta_path.exists()):
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if time.time() - float(meta.get("fetched_at", 0)) > ttl:
            return None
        body = body_path.read_bytes()
    except (OSError, ValueError, json.JSONDecodeError):
        logger.debug("web_fetch: unreadable cache entry for %s", url, exc_info=True)
        return None
    return FetchOutcome(
        status=int(meta.get("status", 200)),
        final_url=str(meta.get("final_url") or url),
        content_type=str(meta.get("content_type") or ""),
        body=body,
        truncated=bool(meta.get("truncated", False)),
        transport=str(meta.get("transport") or "cache"),
        elapsed_ms=int(meta.get("elapsed_ms", 0)),
    )


def _write_cache(url: str, target: NetworkTarget, outcome: FetchOutcome, settings: Any) -> Path | None:
    """Persist a successful body and index it; return the stored path."""
    ttl = float(getattr(settings, "web_cache_ttl_s", 0) or 0)
    if ttl <= 0:
        return None
    slot = _cache_slot(url, settings)
    if slot is None:
        return None
    body_path, workspace_id, session_id = slot
    try:
        body_path.write_bytes(outcome.body)
        _cache_meta_path(body_path).write_text(
            json.dumps(
                {
                    "url": url,
                    "final_url": outcome.final_url,
                    "status": outcome.status,
                    "content_type": outcome.content_type,
                    "truncated": outcome.truncated,
                    "transport": outcome.transport,
                    "elapsed_ms": outcome.elapsed_ms,
                    "fetched_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("web_fetch: could not write cache for %s", url, exc_info=True)
        return None
    try:
        from leapflow.cache.manager import CacheManager, CacheScope

        manager = CacheManager(settings.profile_layout.cache, profile_id=settings.profile)
        manager.register(
            path=body_path,
            scope=CacheScope.SESSION,
            category=_CACHE_CATEGORY,
            source="body",
            workspace_id=workspace_id,
            session_id=session_id,
            content_hash=hashlib.sha256(outcome.body).hexdigest(),
            expires_at=time.time() + ttl,
            # A URL with a query string or credentials can itself be the secret,
            # and fetched content is cheap to re-acquire, so never sync it out.
            sensitive=bool(target.has_credentials or "?" in url),
            syncable=False,
            owner_component="web_fetch",
            metadata={"url": url, "status": outcome.status, "origin": target.origin},
        )
    except Exception:  # noqa: BLE001 - an unindexed cache file is still usable
        logger.debug("web_fetch: cache index update failed", exc_info=True)
    return body_path


async def web_fetch(params: Dict[str, Any]) -> Dict[str, Any]:
    """Read a URL and return extracted, context-sized content."""
    url = str(params.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "web_fetch requires 'url'.", "retryable": True}
    if "://" not in url:
        url = f"https://{url}"

    settings = _settings()
    try:
        timeout_s = min(float(params.get("timeout") or settings.web_timeout_s), 120.0)
    except (TypeError, ValueError):
        timeout_s = float(settings.web_timeout_s)
    try:
        max_bytes = min(int(params.get("max_bytes") or settings.web_max_bytes), 20_000_000)
    except (TypeError, ValueError):
        max_bytes = int(settings.web_max_bytes)

    try:
        target = await classify_url(url)
    except UrlRejected as rejected:
        return {
            "ok": False,
            "error": rejected.detail,
            "error_type": rejected.reason,
            "retryable": rejected.reason == "dns_error",
            "url": url,
        }

    if target.is_internal or target.has_credentials:
        mode = str(getattr(settings, "web_private_targets", "approval") or "approval").lower()
        if mode == "deny":
            return {
                "ok": False,
                "error": (
                    f"{target.origin} resolves to a {target.category} address and this "
                    "profile is configured to refuse internal targets "
                    "(`web.private_targets=deny`)."
                ),
                "error_type": "blocked_target",
                "retryable": False,
                "target_category": target.category,
                "url": url,
            }
        if mode != "allow":
            denial = await _approve_fetch(target)
            if denial:
                return {
                    "ok": False,
                    "error": denial,
                    "error_type": "blocked_target",
                    "retryable": False,
                    "requires_approval": True,
                    "target_category": target.category,
                    "url": url,
                }

    request = FetchRequest(
        url=url,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
        max_redirects=int(settings.web_max_redirects),
        user_agent=str(settings.web_user_agent or DEFAULT_USER_AGENT),
    )
    # Cache lookup happens after the egress gate, never before: a cached body must
    # not become a way to read an internal target that approval would refuse.
    cached = _read_cache(url, settings)
    if cached is not None:
        return _build_result(params, target, cached, settings, from_cache=True)
    try:
        outcome = await _run_transports(
            request,
            str(settings.web_transport or "auto").lower(),
            max(0, int(settings.web_max_retries)),
        )
    except TransportUnavailable as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": "transport_unavailable",
            "retryable": False,
            "url": url,
        }
    except TransportFailure as failure:
        return {
            "ok": False,
            "error": str(failure),
            "error_type": failure.error_type,
            "retryable": failure.retryable,
            "url": url,
            "origin": target.origin,
        }

    return _build_result(params, target, outcome, settings)


def _build_result(
    params: Mapping[str, Any],
    target: NetworkTarget,
    outcome: FetchOutcome,
    settings: Any,
    *,
    from_cache: bool = False,
) -> Dict[str, Any]:
    """Shape a transport outcome into the tool's structured result."""
    result: Dict[str, Any] = {
        "ok": 200 <= outcome.status < 300,
        "status": outcome.status,
        "url": target.url,
        "final_url": outcome.final_url,
        "content_type": outcome.content_type,
        "bytes": len(outcome.body),
        "truncated": outcome.truncated,
        "transport": outcome.transport,
        "elapsed_ms": outcome.elapsed_ms,
        "origin": target.origin,
    }
    if from_cache:
        result["from_cache"] = True
    kind = kind_for_content_type(outcome.content_type)
    result["kind"] = kind

    if not result["ok"]:
        # An HTTP error is the answer, not a crash: name the status and let the
        # model decide, with a body excerpt because error pages explain why.
        excerpt = ""
        if kind != KIND_BINARY:
            excerpt = _redact(_decode_text(outcome.body, outcome.content_type))[:600]
        result["error"] = f"HTTP {outcome.status} from {target.origin}"
        result["error_type"] = "http_error"
        result["retryable"] = outcome.status in _RETRY_STATUSES
        if excerpt.strip():
            result["body_excerpt"] = excerpt
        return result

    stored: Path | None = None
    if not from_cache:
        stored = _write_cache(target.url, target, outcome, settings)

    if kind == KIND_BINARY:
        # Binary never enters the transcript. When it was cached, hand back the
        # path so file-oriented tools can take over instead of a dead end.
        result["text"] = ""
        if stored is None and from_cache:
            slot = _cache_slot(target.url, settings)
            stored = slot[0] if slot else None
        if stored is not None:
            result["cache_path"] = str(stored)
            result["note"] = (
                "Binary content is not returned inline; it is saved at cache_path for "
                "a file-oriented tool to handle."
            )
        else:
            result["note"] = (
                "Binary content is not returned inline. Re-request with a text or JSON "
                "endpoint, or ask the user how this file should be handled."
            )
        return result

    body_text = _decode_text(outcome.body, outcome.content_type)

    if kind == KIND_JSON:
        data, error = decode_json(body_text)
        if error:
            # Declared JSON that does not parse: report it as a content problem
            # with the real status attached, never as an opaque failure.
            result["ok"] = False
            result["error"] = error
            result["error_type"] = "invalid_json"
            result["retryable"] = False
            result["body_excerpt"] = _redact(body_text)[:600]
            return result
        select = str(params.get("select") or "").strip()
        if select:
            value, select_error = select_path(data, select)
            if select_error:
                result["ok"] = False
                result["error"] = select_error
                result["error_type"] = "invalid_selector"
                result["retryable"] = True
                result["available_top_level_keys"] = (
                    sorted(data)[:20] if isinstance(data, dict) else []
                )
                return result
            result["select"] = select
            result["data"] = value
        else:
            result["data"] = data
        return result

    if kind == KIND_HTML:
        extracted = extract_html(
            body_text,
            url=outcome.final_url or target.url,
            prefer=str(getattr(settings, "web_extractor", "auto") or "auto").lower(),
        )
        result["title"] = extracted.title
        result["text"] = _redact(extracted.text)
        result["extractor"] = extracted.extractor
        if extracted.links:
            result["links"] = [{"text": text, "url": link} for text, link in extracted.links]
        if not extracted.text.strip():
            result["note"] = (
                "No readable text could be extracted; the page may render its content "
                "with JavaScript."
            )
        return result

    result["text"] = _redact(body_text)[:_TEXT_BODY_LIMIT]
    return result


__all__ = [
    "DEFAULT_USER_AGENT",
    "CurlTransport",
    "FetchOutcome",
    "FetchRequest",
    "HttpxTransport",
    "TransportFailure",
    "TransportUnavailable",
    "WebTransport",
    "get_web_approval_gate",
    "set_web_approval_gate",
    "transports_for",
    "web_fetch",
]
