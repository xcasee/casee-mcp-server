"""Per-request caller API key propagation.

The streamable-HTTP gateway authenticates every ``tools/call`` with the
caller's ``X-API-Key`` (see :mod:`.http_auth`). Tool implementations must use
that same key for outbound CaSee calls, otherwise every agent would share the
single key from ``CASEE_API_KEY`` and tenant isolation, Credits billing and
revocation would follow the server instead of the caller.

The key is carried in a :class:`contextvars.ContextVar` set by the MCP server
middleware :func:`bind_caller_api_key`. It must run inside the session task
(not the ASGI request task), because MCP executes tool handlers from the
long-lived per-session receive loop; a value set at the ASGI layer never
reaches the tool and every call would fall back to ``CASEE_API_KEY``.

stdio deployments have no inbound key; callers fall back to ``CASEE_API_KEY``.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_caller_api_key: ContextVar[str] = ContextVar("casee_caller_api_key", default="")


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


def _header_api_key(request: Any) -> str:
    """Read ``X-API-Key`` from a Starlette request (case-insensitive)."""
    headers = getattr(request, "headers", None)
    if not headers:
        return ""
    try:
        return (headers.get("x-api-key") or "").strip()
    except Exception:  # noqa: BLE001 - headers are best-effort here
        return ""


async def bind_caller_api_key(ctx: Any, call_next):
    """MCP middleware: bind the caller key for the duration of one message.

    The ASGI gateway cannot do this with a ContextVar: tools execute in the
    long-lived session receive task, not in the HTTP request task, so anything
    set at the ASGI layer is invisible to ``get_caller_api_key()`` and every
    tool silently falls back to the deployment ``CASEE_API_KEY`` (which is not
    guaranteed to exist on the EIRS plane). This middleware runs inside the
    session task, immediately before the handler, so the value propagates into
    the handler (and into the worker thread of sync tools).
    """
    token = set_caller_api_key(_header_api_key(getattr(ctx, "request", None)))
    try:
        return await call_next(ctx)
    finally:
        reset_caller_api_key(token)
