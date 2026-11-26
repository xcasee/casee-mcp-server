"""HTTP gateway API-Key authentication for the public MCP endpoint.

The streamable-HTTP deployment behind causalai.me:8100 is reachable from the
public internet. The MCP protocol handshake (initialize / initialized / ping)
is allowed anonymously per protocol convention; every other JSON-RPC method
(tools/list, tools/call, ...) must carry a valid ``X-API-Key`` header.

Keys are verified against **both** CaSee planes with short positive/negative
caches, so revocation propagates within the TTL:

* is-server (``CASEE_API_BASE_URL`` ``/v1/stats``) — retrieval tools;
* EIRS OpenAPI (``CASEE_OPENAPI_BASE_URL`` ``/keys/whoami``) — report and
  CVC-management tools, which only accept keys registered in the EIRS store.
  ``/keys/whoami`` is a lightweight identity probe (no credits/quota lookup),
  so gateway verification never shows up as balance-polling traffic.

A key that passes is-server but is unknown to EIRS (typical ``sk_...`` keys
created directly on is-server) is rejected at the gateway with
``AUTH_KEY_PLANE_MISMATCH`` instead of failing later as 40102 inside a tool.
An optional static allow-list (MCP_ALLOWED_API_KEYS) provides an emergency
bypass and skips the probes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import httpx
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

logger = logging.getLogger("casee_mcp_server.auth")

_PUBLIC_METHODS = frozenset(
    {"initialize", "ping", "notifications/initialized"}
)

# Verification verdicts (definitive ones are cached; ``None`` means the
# upstream could not be reached and the caller should retry).
_VERDICT_OK = "ok"
_VERDICT_INVALID = "invalid"
_VERDICT_PLANE_MISMATCH = "plane_mismatch"

# EIRS error code for "API Key 无效、已吊销或不存在"; on the OpenAPI probe it
# means the key is valid for is-server but was never registered in EIRS.
_EIRS_CODE_KEY_NOT_FOUND = 40102


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
        openapi_base_url: str = "",
        static_allowlist: frozenset | None = None,
        cache_ttl: float = 300.0,
        neg_cache_ttl: float = 60.0,
        verify_timeout: float = 5.0,
    ) -> None:
        self.app = app
        self.protected_path = protected_path
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.openapi_base_url = openapi_base_url.rstrip("/")
        self.static_allowlist = static_allowlist or frozenset()
        self.cache_ttl = cache_ttl
        self.neg_cache_ttl = neg_cache_ttl
        self.verify_timeout = verify_timeout
        self._cache: dict[str, tuple[str, float]] = {}
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
            # The caller key is rebound per MCP message inside the session task
            # (request_context.bind_caller_api_key); a ContextVar set here would
            # not reach the tool handler.
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
                error_code="AUTH_MISSING_KEY",
                hint="Set the X-API-Key header with a valid CaSee API key.",
                action="provide_api_key",
                retryable=False,
            )
        if api_key in self.static_allowlist:
            return None
        verdict = await self._verify_cached(api_key)
        if verdict == _VERDICT_OK:
            return None
        if verdict is None:
            return self._deny(
                503,
                "Upstream key verification unavailable; retry shortly.",
                retry_after=10,
                error_code="UPSTREAM_UNAVAILABLE",
                hint="The API key verification service is unavailable; retry later.",
                action="retry_later",
                retryable=True,
            )
        if verdict == _VERDICT_PLANE_MISMATCH:
            logger.info("reject: key not registered in EIRS ip=%s",
                        scope.get("client", ("",))[0])
            return self._deny(
                401,
                "This API key is valid on is-server but not registered in the "
                "CaSee OpenAPI (EIRS) key store, so report and CVC-management "
                "tools would fail with 40102.",
                error_code="AUTH_KEY_PLANE_MISMATCH",
                hint="Use the casee_... tenant key from https://www.casee.me "
                     "(系统配置 → API Keys); it is registered on both planes. "
                     "Keys created directly on is-server (sk_...) are not.",
                action="rotate_key",
                retryable=False,
            )
        logger.info("reject: invalid api key ip=%s",
                    scope.get("client", ("",))[0])
        return self._deny(
            401,
            "Invalid or revoked API key.",
            error_code="AUTH_INVALID_KEY",
            hint="Check CASEE_IS_API_KEY or create a new API key.",
            action="rotate_key",
            retryable=False,
        )

    async def _verify_cached(self, key: str) -> Optional[str]:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None:
            verdict, ts = hit
            ttl = self.cache_ttl if verdict == _VERDICT_OK else self.neg_cache_ttl
            if now - ts < ttl:
                return verdict
        verdict = await self._verify_planes(key)
        if verdict is not None:
            self._cache[key] = (verdict, now)
            if len(self._cache) > 1024:
                # Basic bound: drop everything older than 15 min.
                self._cache = {
                    k: v for k, v in self._cache.items() if now - v[1] < 900
                }
        return verdict

    async def _verify_planes(self, key: str) -> Optional[str]:
        """Check the key against is-server, then the EIRS OpenAPI plane.

        Returns ``ok`` / ``invalid`` / ``plane_mismatch``, or ``None`` when a
        plane is unreachable (caller retries; nothing is cached).
        """
        if not self.upstream_base_url:
            return _VERDICT_INVALID

        is_result = await self._probe_plane(
            f"{self.upstream_base_url}/v1/stats", key, "is-server",
        )
        if is_result["verdict"] != _VERDICT_OK:
            return is_result["verdict"]
        if not self.openapi_base_url:
            logger.warning(
                "openapi base URL not configured; dual-plane key check skipped"
            )
            return _VERDICT_OK

        eirs_result = await self._probe_plane(
            f"{self.openapi_base_url}/keys/whoami", key, "eirs-openapi",
        )
        if eirs_result["verdict"] == _VERDICT_OK:
            return _VERDICT_OK
        if eirs_result["verdict"] == _VERDICT_INVALID:
            # 40102 means the key is simply absent from the EIRS store; other
            # auth errors (expired / disabled / scope) are plain invalid keys.
            if eirs_result["code"] in (None, _EIRS_CODE_KEY_NOT_FOUND):
                return _VERDICT_PLANE_MISMATCH
            return _VERDICT_INVALID
        return None

    @staticmethod
    def _probe_headers(key: str) -> dict[str, str]:
        """鉴权探测专用请求头：显式标注用途，服务端据此归入「预检」而非业务调用。"""
        return {
            "X-API-Key": key,
            "X-Casee-Call-Purpose": "auth_probe",
            "X-Casee-Client": "casee-mcp-server",
        }

    async def _probe_plane(self, url: str, key: str, label: str) -> dict:
        """GET one verification probe and classify the response."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.verify_timeout)
        try:
            resp = await self._client.get(url, headers=self._probe_headers(key))
        except httpx.HTTPError as exc:
            logger.warning("%s key verify error: %s", label, exc)
            return {"verdict": None, "code": None}

        if resp.status_code == 200:
            return {"verdict": _VERDICT_OK, "code": None}
        if resp.status_code in (401, 403):
            code = None
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                raw_code = body.get("code")
                if isinstance(raw_code, int):
                    code = raw_code
            logger.info("%s rejected key status=%s code=%s",
                        label, resp.status_code, code)
            return {"verdict": _VERDICT_INVALID, "code": code}

        logger.warning(
            "%s key verify unexpected status=%s", label, resp.status_code
        )
        return {"verdict": None, "code": None}

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
    def _deny(
        status: int,
        detail: str,
        *,
        retry_after: int | None = None,
        error_code: str = "",
        hint: str = "",
        action: str = "",
        retryable: bool = False,
    ):
        headers = {"WWW-Authenticate": 'ApiKey realm="casee-mcp"'}
        if retry_after:
            headers["Retry-After"] = str(retry_after)

        async def respond(scope, receive, send) -> None:
            error: dict = {"code": -32001, "message": detail}
            if error_code:
                error["data"] = {
                    "error_code": error_code,
                    "hint": hint,
                    "action": action,
                    "retryable": retryable,
                }
            resp = JSONResponse(
                {"jsonrpc": "2.0", "error": error, "id": None},
                status_code=status,
                headers=headers,
            )
            await resp(scope, receive, send)

        return respond


def build_authenticated_app(mcp_server, *, path: str, json_response: bool = False):
    """Wrap the MCP streamable-HTTP ASGI app with API-key auth when enabled.

    ``json_response=True`` (env ``MCP_JSON_RESPONSE``) makes POST responses a
    single JSON document instead of an SSE stream, which avoids client-side
    SSE parsing failures on long tool results.
    """
    app = mcp_server.streamable_http_app(
        streamable_http_path=path, host="0.0.0.0", json_response=json_response,
    )
    if not _env_bool("MCP_REQUIRE_API_KEY", True):
        logger.warning(
            "MCP gateway API-key auth DISABLED (MCP_REQUIRE_API_KEY=false); "
            "every request falls back to the deployment key, so tenant "
            "attribution collapses. Only do this for single-tenant stdio-like "
            "deployments."
        )
        return app
    allowlist = frozenset(
        x.strip()
        for x in os.environ.get("MCP_ALLOWED_API_KEYS", "").split(",")
        if x.strip()
    )
    # Resolve the same OpenAPI base the report tools use, so the gateway and
    # the tools never disagree about which EIRS deployment a key must exist in.
    from .reports import resolve_openapi_base

    try:
        openapi_base = resolve_openapi_base()
    except Exception as exc:  # noqa: BLE001 - auth must not break on config
        logger.warning("cannot resolve OpenAPI base for key check: %s", exc)
        openapi_base = ""
    wrapped = ApiKeyAuthMiddleware(
        app,
        protected_path=path,
        upstream_base_url=os.environ.get("CASEE_API_BASE_URL", ""),
        openapi_base_url=openapi_base,
        static_allowlist=allowlist,
        cache_ttl=float(os.environ.get("MCP_AUTH_CACHE_TTL", "300")),
        neg_cache_ttl=float(os.environ.get("MCP_AUTH_NEG_CACHE_TTL", "60")),
        verify_timeout=float(os.environ.get("MCP_AUTH_VERIFY_TIMEOUT", "5")),
    )
    logger.info(
        "MCP gateway API-key auth ENABLED is-server=%s eirs=%s "
        "static_allowlist=%d",
        wrapped.upstream_base_url,
        wrapped.openapi_base_url,
        len(allowlist),
    )
    return wrapped
