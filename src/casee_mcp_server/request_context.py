"""Per-request caller API key + client/tool attribution propagation.

The streamable-HTTP gateway authenticates every ``tools/call`` with the
caller's ``X-API-Key`` (see :mod:`.http_auth`). Tool implementations must use
that same key for outbound CaSee calls, otherwise every agent would share the
single deployment key from ``CASEE_IS_API_KEY`` and tenant isolation, Credits
billing and revocation would follow the server instead of the caller.

The key is carried in a :class:`contextvars.ContextVar` set by the MCP server
middleware :func:`bind_caller_api_key`. It must run inside the session task
(not the ASGI request task), because MCP executes tool handlers from the
long-lived per-session receive loop; a value set at the ASGI layer never
reaches the tool and every call would fall back to ``CASEE_IS_API_KEY``.

The same middleware also binds usage-attribution context so outbound OpenAPI
calls can send ``X-Casee-Client`` / ``X-Casee-Tool`` / ``X-Casee-Session`` and
the backend audit log can attribute a call to a concrete MCP tool and session:

- ``X-Casee-Client`` / ``X-Casee-Client-Version`` come from the inbound MCP
  client (WorkBuddy / Trae ...) when it sets them, otherwise default to
  ``mcp`` + the server release version;
- ``X-Casee-Tool`` is read from the inbound ``tools/call`` params and never
  trusted from a header;
- ``X-Casee-Session`` prefers the inbound header, otherwise a per-process
  UUID is reused for the lifetime of the server process.

stdio deployments have no inbound key; callers fall back to ``CASEE_IS_API_KEY``
(legacy alias: ``CASEE_API_KEY``).
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from typing import Any

_caller_api_key: ContextVar[str] = ContextVar("casee_caller_api_key", default="")
_caller_tool: ContextVar[str] = ContextVar("casee_caller_tool", default="")
_caller_client: ContextVar[str] = ContextVar("casee_caller_client", default="mcp")
_caller_client_version: ContextVar[str] = ContextVar("casee_caller_client_version", default="")
_caller_session: ContextVar[str] = ContextVar("casee_caller_session", default="")

# Session ids are generated lazily once per process so stdio runs (one process
# per agent session) and long-lived HTTP servers both produce stable values.
_PROCESS_SESSION = uuid.uuid4().hex


def set_caller_api_key(api_key: str) -> Token:
    """Bind the verified caller key to the current request context."""
    return _caller_api_key.set((api_key or "").strip())


def reset_caller_api_key(token: Token) -> None:
    """Restore the previous value; tolerate cross-context resets."""
    try:
        _caller_api_key.reset(token)
    except ValueError:  # pragma: no cover - background task copied a token
        pass


def get_caller_api_key() -> str:
    """Return the verified caller key, or ``""`` outside an HTTP request."""
    return _caller_api_key.get()


def get_caller_tool() -> str:
    """Return the MCP tool currently executing, or ``""`` outside a call."""
    return _caller_tool.get()


def get_caller_client() -> str:
    """Return the MCP client name reported for this call."""
    return _caller_client.get()


def get_caller_client_version() -> str:
    """Return the MCP client version reported for this call, if any."""
    return _caller_client_version.get()


def get_caller_session() -> str:
    """Return the inbound session id, or the process-level fallback."""
    return _caller_session.get() or _PROCESS_SESSION


def attribution_headers(*, default_version: str = "") -> dict:
    """Headers that let the backend audit log attribute an outbound call."""
    return {
        "X-Casee-Client": get_caller_client() or "mcp",
        "X-Casee-Client-Version": get_caller_client_version() or default_version,
        "X-Casee-Tool": get_caller_tool(),
        "X-Casee-Session": get_caller_session(),
    }


def _headers_of(request: Any):
    headers = getattr(request, "headers", None)
    if headers:
        return headers
    # Some transports expose the raw scope instead of a Starlette request.
    scope = getattr(request, "scope", None)
    raw = scope.get("headers") if isinstance(scope, dict) else None
    if raw:
        return {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in raw
        }
    return None


def _header(request: Any, name: str) -> str:
    """Read a header case-insensitively, tolerating any transport shape."""
    headers = _headers_of(request)
    if not headers:
        return ""
    try:
        return (headers.get(name.lower()) or "").strip()
    except Exception:  # noqa: BLE001 - headers are best-effort here
        return ""


def _header_api_key(request: Any) -> str:
    """Read ``X-API-Key`` from a Starlette request (case-insensitive)."""
    return _header(request, "x-api-key")


def _current_tool(ctx: Any) -> str:
    """Extract ``tools/call`` tool name from the middleware context."""
    if str(getattr(ctx, "method", "") or "") != "tools/call":
        return ""
    params = getattr(ctx, "params", None)
    if isinstance(params, dict):
        name = params.get("name") or (params.get("request") or {}).get("name")
        return str(name or "")
    message = getattr(ctx, "message", None)
    if isinstance(message, dict):
        return str(message.get("params", {}).get("name") or "")
    return ""


async def bind_caller_api_key(ctx: Any, call_next):
    """MCP middleware: bind caller key + attribution for one message.

    The ASGI gateway cannot do this with a ContextVar: tools execute in the
    long-lived session receive task, not in the HTTP request task, so anything
    set at the ASGI layer is invisible to ``get_caller_api_key()`` and every
    tool silently falls back to the deployment ``CASEE_IS_API_KEY`` (which is
    not guaranteed to exist on the EIRS plane). This middleware runs inside the
    session task, immediately before the handler, so the value propagates into
    the handler (and into the worker thread of sync tools).
    """
    request = getattr(ctx, "request", None) or getattr(ctx, "transport", None)
    bindings = (
        (_caller_api_key, _header_api_key(request)),
        (_caller_tool, _current_tool(ctx)),
        (_caller_client, _header(request, "x-casee-client") or "mcp"),
        (_caller_client_version, _header(request, "x-casee-client-version")),
        (_caller_session, _header(request, "x-casee-session")),
    )
    tokens = [(var, var.set(value)) for var, value in bindings]
    try:
        return await call_next(ctx)
    finally:
        for var, token in reversed(tokens):
            try:
                var.reset(token)
            except ValueError:  # pragma: no cover - background task copied a token
                pass
