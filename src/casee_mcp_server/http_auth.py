"""HTTP gateway API-Key authentication for the public MCP endpoint.

The streamable-HTTP deployment behind causalai.me:8100 is reachable from the
public internet. The MCP protocol handshake (initialize / initialized / ping)
is allowed anonymously per protocol convention; every other JSON-RPC method
(tools/list, tools/call, ...) must carry a valid ``X-API-Key`` header.

Keys are verified against the upstream CaSee is-server (GET /v1/stats) with
short positive/negative caches, so revocation propagates within the TTL. An
optional static allow-list (MCP_ALLOWED_API_KEYS) provides an emergency bypass.
"""
from __future__ import annotations

import json
import logging
import os
import time

import httpx
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

logger = logging.getLogger("casee_mcp_server.auth")

_PUBLIC_METHODS = frozenset(
    {"initialize", "ping", "notifications/initialized"}
)


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


class ApiKeyAuthMiddleware:
    """ASGI middleware enforcing X-API-Key on protected JSON-RPC methods."""

    def __init__(
        self,
        app,
        *,
        protected_path: str = "/mcp",
        upstream_base_url: str = "",
        static_allowlist: frozenset | None = None,
        cache_ttl: float = 300.0,
        neg_cache_ttl: float = 60.0,
        verify_timeout: float = 5.0,
    ) -> None:
        self.app = app
        self.protected_path = protected_path
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.static_allowlist = static_allowlist or frozenset()
        self.cache_ttl = cache_ttl
        self.neg_cache_ttl = neg_cache_ttl
        self.verify_timeout = verify_timeout
        self._cache: dict[str, tuple[bool, float]] = {}
        self._client: httpx.AsyncClient | None = None

    # ---------------------------------------------------------------- ASGI
    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") != self.protected_path:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "").upper()
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        api_key = headers.get("x-api-key", "").strip()

        if method == "POST":
            chunks: list[bytes] = []
            while True:
                msg = await receive()
                if msg["type"] == "http.request":
                    chunks.append(msg.get("body", b""))
                    if not msg.get("more_body", False):
                        break
                elif msg["type"] == "http.disconnect":
                    break
            body = b"".join(chunks)
            # First call replays the buffered body; every subsequent call
            # (notably http.disconnect, which drives the SSE connection
            # lifecycle) is transparently forwarded to the real receive.
            replay = self._replay_receive(body, receive)

            if not self._body_requires_auth(body):
                await self.app(scope, replay, send)
                return
            denied = await self._check(api_key, scope)
            if denied is not None:
                await denied(scope, replay, send)
                return
            await self.app(scope, replay, send)
            return

        if method == "GET":
            # Opening the SSE stream is the same trust boundary as POST.
            denied = await self._check(api_key, scope)
            if denied is not None:
                await denied(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        await self.app(scope, receive, send)

    # ---------------------------------------------------------------- auth
    def _body_requires_auth(self, body: bytes) -> bool:
        try:
            payload = json.loads(body or b"null")
        except Exception:
            # Malformed JSON must not slip through anonymously; the MCP app
            # would return a protocol error, but fail closed at the gateway.
            return True
        if isinstance(payload, list):
            methods = [
                str(m.get("method", ""))
                for m in payload
                if isinstance(m, dict)
            ]
        elif isinstance(payload, dict):
            methods = [str(payload.get("method", ""))]
        else:
            return True
        if not methods:
            return True
        return any(m not in _PUBLIC_METHODS for m in methods)

    async def _check(self, api_key: str, scope):
        if not api_key:
            logger.info("reject: missing api key path=%s ip=%s",
                        scope.get("path"), scope.get("client", ("",))[0])
            return self._deny(
                401,
                "Authentication required. Provide your CaSee API key via "
                "the X-API-Key header.",
            )
        if api_key in self.static_allowlist:
            return None
        verdict = await self._verify_cached(api_key)
        if verdict is True:
            return None
        if verdict is None:
            return self._deny(
                503,
                "Upstream key verification unavailable; retry shortly.",
                retry_after=10,
            )
        logger.info("reject: invalid api key ip=%s",
                    scope.get("client", ("",))[0])
        return self._deny(401, "Invalid or revoked API key.")

    async def _verify_cached(self, key: str) -> bool | None:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None:
            valid, ts = hit
            ttl = self.cache_ttl if valid else self.neg_cache_ttl
            if now - ts < ttl:
                return valid
        verdict = await self._verify_upstream(key)
        if verdict is not None:
            self._cache[key] = (verdict, now)
            if len(self._cache) > 1024:
                # Basic bound: drop everything older than 15 min.
                self._cache = {
                    k: v for k, v in self._cache.items() if now - v[1] < 900
                }
        return verdict

    async def _verify_upstream(self, key: str) -> bool | None:
        if not self.upstream_base_url:
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.verify_timeout)
        try:
            resp = await self._client.get(
                f"{self.upstream_base_url}/v1/stats",
                headers={"X-API-Key": key},
            )
        except httpx.HTTPError as exc:
            logger.warning("upstream key verify error: %s", exc)
            return None
        if resp.status_code == 200:
            return True
        if resp.status_code in (401, 403):
            return False
        logger.warning(
            "upstream key verify unexpected status=%s", resp.status_code
        )
        return None

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _replay_receive(body: bytes, original_receive):
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }
            return await original_receive()

        return receive

    @staticmethod
    def _deny(status: int, detail: str, *, retry_after: int | None = None):
        headers = {"WWW-Authenticate": 'ApiKey realm="casee-mcp"'}
        if retry_after:
            headers["Retry-After"] = str(retry_after)

        async def respond(scope, receive, send) -> None:
            resp = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32001, "message": detail},
                    "id": None,
                },
                status_code=status,
                headers=headers,
            )
            await resp(scope, receive, send)

        return respond


def build_authenticated_app(mcp_server, *, path: str):
    """Wrap the MCP streamable-HTTP ASGI app with API-key auth when enabled."""
    app = mcp_server.streamable_http_app(
        streamable_http_path=path, host="0.0.0.0"
    )
    if not _env_bool("MCP_REQUIRE_API_KEY", False):
        logger.warning(
            "MCP gateway API-key auth DISABLED (MCP_REQUIRE_API_KEY not true)"
        )
        return app
    allowlist = frozenset(
        x.strip()
        for x in os.environ.get("MCP_ALLOWED_API_KEYS", "").split(",")
        if x.strip()
    )
    wrapped = ApiKeyAuthMiddleware(
        app,
        protected_path=path,
        upstream_base_url=os.environ.get("CASEE_API_BASE_URL", ""),
        static_allowlist=allowlist,
        cache_ttl=float(os.environ.get("MCP_AUTH_CACHE_TTL", "300")),
        neg_cache_ttl=float(os.environ.get("MCP_AUTH_NEG_CACHE_TTL", "60")),
        verify_timeout=float(os.environ.get("MCP_AUTH_VERIFY_TIMEOUT", "5")),
    )
    logger.info(
        "MCP gateway API-key auth ENABLED upstream=%s static_allowlist=%d",
        wrapped.upstream_base_url,
        len(allowlist),
    )
    return wrapped
