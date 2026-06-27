"""Upstream MCP servers: theodosia as an MCP client to other servers.

``bind_upstream`` installs a manager for the current context; ``call_upstream``
invokes a tool on a named upstream from inside a Burr action body. A manager
is anything with an async ``call(server, tool, args)`` method.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

_UPSTREAM: ContextVar[Any | None] = ContextVar("theodosia_upstream", default=None)


class UpstreamError(RuntimeError):
    """An upstream call failed or no manager/server was available.

    When the failure came from a named upstream tool call, ``server``,
    ``tool``, and ``body`` carry the upstream server name, the tool name,
    and the upstream error body so action code can branch on them without
    parsing the message string. All three are ``None`` for binding-level
    failures (no manager bound, unknown server name).
    """

    def __init__(  # noqa: D107  # the class docstring documents the fields
        self,
        message: str,
        *,
        server: str | None = None,
        tool: str | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.server = server
        self.tool = tool
        self.body = body


# ── Classified upstream responses ─────────────────────────────────────────

OK = "ok"
ERROR = "error"
MALFORMED = "malformed"

_STATUS_VALUES = frozenset({OK, ERROR, MALFORMED})
_ERROR_TEXT_HINTS = ("error", "exception", "traceback", "failed")
_DETAIL_LIMIT = 300
# Cap on an upstream string parsed as rows; bounds parse cost on hostile input.
_MAX_ROWS_BYTES = 8_000_000


@dataclass
class SourceResult:
    """A classified upstream response.

    ``status`` is the lowercase string ``"ok"``, ``"error"``, or
    ``"malformed"`` (the public ``theodosia.OK`` / ``ERROR`` /
    ``MALFORMED`` constants hold those values). The uppercase names are
    Python identifiers; the wire value is lowercase. Compare to the
    constants, not to bare strings, to avoid a case-mismatch silently
    classifying every response as degraded.
    """

    name: str
    status: str
    data: Any = None
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject statuses outside the OK/ERROR/MALFORMED wire vocabulary."""
        if self.status not in _STATUS_VALUES:
            raise ValueError(
                f"SourceResult.status must be one of {sorted(_STATUS_VALUES)}; got {self.status!r}"
            )

    @property
    def usable(self) -> bool:
        """True when the data is structured and not error-shaped."""
        return self.status == OK

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the wire: name, status, data, detail, meta."""
        return {
            "name": self.name,
            "status": self.status,
            "data": self.data,
            "detail": self.detail,
            "meta": self.meta,
        }


def _coerce_rows(payload: Any) -> list[Any] | None:
    """Coerce an upstream payload into a list of rows, or ``None``.

    Already a list -> as-is; a single dict -> ``[dict]``. A string is parsed
    as JSON, then as a Python literal (``ast.literal_eval``) — many DB/MCP
    servers (e.g. sqlite) return rows as a Python ``repr`` string, not JSON.
    ``literal_eval`` only builds literals, so it cannot execute upstream code.
    Returns ``None`` for anything not row-shaped.

    The string is upstream-supplied, so the parse is defended on two axes:
    oversized input is rejected before parsing (bounds parse cost), and the
    parse catches every exception — including ``RecursionError`` /
    ``MemoryError`` from a pathologically nested literal — so a hostile
    upstream can never break the "never raises" contract of ``safe_upstream``.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    if not isinstance(payload, str) or len(payload) > _MAX_ROWS_BYTES:
        return None
    value = _parse_literal(payload)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return None


def _parse_literal(text: str) -> Any:
    """Parse ``text`` as JSON, then as a Python literal; ``None`` if neither.

    The string is upstream-supplied, so every exception is swallowed — a
    ``RecursionError``/``MemoryError`` from a pathological literal must not
    escape (``ast.literal_eval`` builds literals only, never executes code).
    """
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(text)
        except Exception:  # any parse failure -> not a literal
            continue
    return None


def _nested_error(payload: Any, depth: int = 3) -> str | None:
    """Return an error string if ``payload`` (a dict) carries an error envelope.

    Walks at most ``depth`` levels into nested dicts looking for an ``error``
    key with a truthy value. Stops at depth or first non-dict.
    """
    if depth <= 0 or not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if err:
        return str(err)
    for v in payload.values():
        nested = _nested_error(v, depth - 1)
        if nested is not None:
            return nested
    return None


def _classify_str(name: str, payload: str, expect: str) -> SourceResult:
    """String-payload arm of ``classify_payload`` (``text`` vs structured)."""
    if expect == "text":
        if payload.strip():
            return SourceResult(name, OK, data=payload)
        return SourceResult(name, ERROR, detail="empty response")
    low = payload.lower()
    if any(hint in low for hint in _ERROR_TEXT_HINTS):
        return SourceResult(name, ERROR, detail=payload[:_DETAIL_LIMIT])
    return SourceResult(name, MALFORMED, data=payload, detail="unstructured text")


def _classify_shape(name: str, payload: Any, expect: str) -> SourceResult:
    """Non-string arm: error envelope in dicts, then the ``list``/``dict`` expectation."""
    if isinstance(payload, dict):
        err = _nested_error(payload)
        if err is not None:
            return SourceResult(name, ERROR, detail=err[:_DETAIL_LIMIT])
    if expect == "list" and not isinstance(payload, (list, dict)):
        return SourceResult(name, MALFORMED, data=payload, detail="expected list/dict")
    if expect == "dict" and not isinstance(payload, dict):
        return SourceResult(name, MALFORMED, data=payload, detail="expected dict")
    return SourceResult(name, OK, data=payload)


def classify_payload(name: str, payload: Any, *, expect: str = "any") -> SourceResult:
    """Classify a payload. ``expect`` is ``any`` | ``list`` | ``dict`` | ``text`` | ``rows``.

    Use ``expect="rows"`` for tabular upstreams (a sqlite server, a query tool)
    that may return rows as JSON *or* as a Python ``repr`` string: the payload
    is coerced to a list of rows (``OK``) or rejected (``MALFORMED``).

    Dicts are checked for an error envelope up to three levels deep so that
    upstreams returning ``{"data": {"error": "..."}}`` still classify as
    ``ERROR``, not silently as ``OK``.

    Use ``expect="text"`` for prose-returning upstreams (a fetch server, a
    filesystem ``read_file``): a non-empty string is ``OK``. The default
    ``any`` mode is for structured (JSON) upstreams and treats a bare string
    as unstructured (``MALFORMED``), or ``ERROR`` if it reads like an error
    message — which would misfire on prose that merely mentions "error", so
    text upstreams must opt into ``text``.
    """
    if payload is None:
        return SourceResult(name, ERROR, detail="empty response")
    if expect == "rows":
        rows = _coerce_rows(payload)
        if rows is not None:
            return SourceResult(name, OK, data=rows)
        return SourceResult(name, MALFORMED, data=payload, detail="not row-shaped")
    if isinstance(payload, str):
        return _classify_str(name, payload, expect)
    return _classify_shape(name, payload, expect)


async def safe_upstream(
    name: str,
    server: str,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    expect: str = "any",
    timeout: float | None = None,
) -> SourceResult:
    """Call an upstream tool and return a classified result. Never raises.

    ``expect`` is ``any`` | ``list`` | ``dict`` | ``text`` | ``rows`` (see
    :func:`classify_payload`). ``timeout`` bounds the call (see
    :func:`call_upstream`).
    """
    try:
        payload = await call_upstream(server, tool, args or {}, timeout=timeout)
    except UpstreamError as exc:
        return SourceResult(name, ERROR, detail=f"upstream unavailable: {exc}"[:_DETAIL_LIMIT])
    except Exception as exc:
        return SourceResult(name, ERROR, detail=f"{type(exc).__name__}: {exc}"[:_DETAIL_LIMIT])
    return classify_payload(name, payload, expect=expect)


def coverage(results: list[SourceResult]) -> tuple[int, int]:
    """Return ``(usable_sources, configured_sources)``."""
    return sum(1 for r in results if r.usable), len(results)


def confidence_label(usable: int, total: int) -> str:
    """Map a coverage tuple to ``none``, ``degraded``, or ``full``."""
    if 0 in (total, usable):
        return "none"
    if usable < total:
        return "degraded"
    return "full"


def bind_upstream(manager: Any) -> Token[Any | None]:
    """Bind an upstream manager for the current context; returns the reset token."""
    return _UPSTREAM.set(manager)


def reset_upstream(token: Token[Any | None]) -> None:
    """Restore the upstream binding captured by ``bind_upstream``."""
    _UPSTREAM.reset(token)


async def call_upstream(
    server: str,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> Any:
    """Call ``tool`` on upstream ``server``; raises ``UpstreamError`` if unbound.

    ``timeout`` (seconds) bounds this one call independently of the step-level
    ``action_timeout_seconds``, so a slow or hung upstream fails fast as a
    structured ``UpstreamError`` instead of stalling the whole step.
    """
    mgr = _UPSTREAM.get()
    if mgr is None:
        raise UpstreamError(
            "No upstream manager bound. mount(application, upstream={...}) wires "
            "upstream MCP servers; outside mount, bind one with bind_upstream(...)."
        )
    call = mgr.call(server, tool, args or {})
    if timeout is None:
        return await call
    try:
        return await asyncio.wait_for(call, timeout)
    except TimeoutError as exc:
        raise UpstreamError(
            f"upstream {server!r} tool {tool!r} timed out after {timeout}s",
            server=server,
            tool=tool,
            body="timeout",
        ) from exc


def _extract(result: Any) -> Any:
    """Pull a JSON-able payload out of an MCP CallToolResult.

    FastMCP wraps scalar tool returns (str, int, bool) in a single-key
    ``{"result": <value>}`` envelope under ``structured_content``. Unwrap
    that single-key envelope so action bodies see the bare value, not the
    envelope. Tools returning dicts / lists pass through unchanged.
    """
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    content = getattr(result, "content", None)
    if content:
        parts = [t for c in content if (t := getattr(c, "text", None))]
        if parts:
            text = "\n".join(parts)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
    return None


def _as_transport(config: Any) -> Any:
    """Map an upstream config to a ``fastmcp.Client`` transport.

    A bare ``{"command": ..., "args": [...]}`` becomes a ``StdioTransport``
    so upstream tool names are not namespaced the way an mcp-config dict
    would prefix them.

    ``log_file`` defaults to ``sys.__stderr__`` (the real, unwrapped stderr
    with a real ``.fileno()``). FastMCP wraps ``sys.stderr`` in a ``StringIO``
    inside a running server for protocol cleanliness, and ``mcp.client.stdio``
    calls ``.fileno()`` on whatever stderr it gets when starting the upstream
    subprocess. Without this default the subprocess opener crashes with
    ``io.UnsupportedOperation: fileno`` and ``mount(upstream={...})`` is dead
    on arrival. Users can override per-config with ``{"log_file": Path(...)}``.
    """
    if isinstance(config, dict) and "command" in config and "mcpServers" not in config:
        import sys

        from fastmcp.client.transports import StdioTransport

        return StdioTransport(
            command=config["command"],
            args=list(config.get("args") or []),
            env=config.get("env"),
            cwd=config.get("cwd"),
            log_file=config.get("log_file", sys.__stderr__),
        )
    return config


class UpstreamManager:
    """Lazily opens and caches one ``fastmcp.Client`` session per upstream server."""

    def __init__(self, configs: dict[str, Any]) -> None:
        """Copy ``{server: transport-config-or-Client-target}`` for lazy opening."""
        self._configs = configs.copy()
        self._clients: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def server_names(self) -> list[str]:
        """Configured upstream server names, sorted."""
        return sorted(self._configs)

    async def _client(self, server: str) -> Any:
        if server in self._clients:
            return self._clients[server]
        if server not in self._configs:
            raise UpstreamError(
                f"unknown upstream server {server!r}; configured: {self.server_names}"
            )
        from fastmcp import Client

        client = Client(_as_transport(self._configs[server]))
        try:
            await client.__aenter__()  # type: ignore[no-untyped-call]  # fastmcp.Client ships no __aenter__ stub
        except RuntimeError as exc:
            if "fileno" in str(exc):
                raise UpstreamError(
                    f"upstream {server!r} (stdio subprocess) could not start: the "
                    "stderr it inherited has no real file descriptor. By default "
                    "Theodosia points the subprocess at ``sys.__stderr__``; this "
                    "branch fires when that has also been replaced. Override via "
                    "``{'log_file': pathlib.Path('/some/file')}`` in the config, "
                    "or pass a pre-built ``UpstreamManager`` for full control, "
                    "or substitute ``theodosia.testing.FakeUpstream`` in tests."
                ) from exc
            raise
        self._clients[server] = client
        return client

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        """Call ``tool`` on ``server``; raises ``UpstreamError`` on tool errors."""
        from fastmcp.exceptions import ToolError

        async with self._lock:
            client = await self._client(server)
            try:
                result = await client.call_tool(tool, args)
            except ToolError as exc:
                # Surface the upstream's error body structurally; without
                # this an action sees only the exception type, writes
                # incomplete state, and the ledger records a misleading
                # entry.
                raise UpstreamError(
                    f"upstream {server!r} tool {tool!r} returned an error: {exc}",
                    server=server,
                    tool=tool,
                    body=str(exc),
                ) from exc
        return _extract(result)

    async def health(self, *, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Ping each configured upstream: open it and list its tools.

        Returns one record per server: ``{server, status: "ok"|"error",
        tools: [...]}`` on success or ``{server, status, error}`` on failure.
        Surfaces a misconfigured upstream up front instead of mid-run. Opens
        any not-yet-open client as a side effect (cached for later calls).
        """
        out: list[dict[str, Any]] = []
        for server in self.server_names:
            try:
                client = await asyncio.wait_for(self._client(server), timeout)
                tools = await asyncio.wait_for(client.list_tools(), timeout)
                names = [getattr(t, "name", str(t)) for t in tools]
                out.append({"server": server, "status": "ok", "tools": names})
            except Exception as exc:
                out.append(
                    {"server": server, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
                )
        return out

    async def aclose(self) -> None:
        """Close every opened upstream client session."""
        async with self._lock:
            for client in self._clients.values():
                with contextlib.suppress(Exception):
                    await client.__aexit__(None, None, None)
            self._clients.clear()


_SESSION_PLACEHOLDER = "{session}"


def _config_is_per_session(upstream: Any) -> bool:
    """Whether an upstream config opts into per-session isolation.

    A config (or any nested value) containing the literal ``{session}`` marks
    a stateful upstream that needs its own per-session client/subprocess (e.g.
    ``"env": {"MEMORY_FILE_PATH": ".../{session}.json"}``). Without the marker
    one shared manager serves every session, which is correct and cheaper for
    stateless upstreams (filesystem reads, fetch).
    """
    if isinstance(upstream, str):
        return _SESSION_PLACEHOLDER in upstream
    if isinstance(upstream, dict):
        return any(_config_is_per_session(v) for v in upstream.values())
    if isinstance(upstream, (list, tuple)):
        return any(_config_is_per_session(v) for v in upstream)
    return False


def _sanitize_session(session_id: str) -> str:
    """Make a session id safe to drop into a file path or arg."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in session_id)


def substitute_session(upstream: Any, session_id: str) -> Any:
    """Return ``upstream`` with every ``{session}`` replaced by ``session_id``.

    Recurses through dicts/lists; replaces in string values only. Used to
    realize a per-session upstream config from the templated one passed to
    ``mount``.
    """
    safe = _sanitize_session(session_id)
    if isinstance(upstream, str):
        return upstream.replace(_SESSION_PLACEHOLDER, safe)
    if isinstance(upstream, dict):
        return {k: substitute_session(v, session_id) for k, v in upstream.items()}
    if isinstance(upstream, list):
        return [substitute_session(v, session_id) for v in upstream]
    return upstream
