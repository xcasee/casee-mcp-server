"""Unit tests for the HTTP gateway API-Key auth middleware (http_auth.py).

The middleware is exercised directly as an ASGI callable with fake scopes,
so no real HTTP server or upstream is involved. Upstream verification is
stubbed at the middleware method / httpx layer.
"""
import json
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from casee_mcp_server.http_auth import (
    ApiKeyAuthMiddleware,
    build_authenticated_app,
    _env_bool,
    _PUBLIC_METHODS,
)

PROTECTED_BODY = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
).encode()
PUBLIC_BODY = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"}}}
).encode()


def make_scope(method="POST", path="/mcp", headers=None, body=PROTECTED_BODY):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("203.0.113.9", 51234),
        "query_string": b"",
    }


async def fake_receive(body=b""):
    sent = {"done": False}

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


class DownstreamApp:
    """Minimal ASGI app capturing the forwarded scope/body."""

    def __init__(self):
        self.scopes = []
        self.bodies = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope)
        if scope["type"] == "http" and scope["method"] == "POST":
            chunks = []
            while True:
                msg = await receive()
                if msg["type"] == "http.request":
                    chunks.append(msg.get("body", b""))
                    if not msg.get("more_body", False):
                        break
                else:
                    break
            self.bodies.append(b"".join(chunks))
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"ok"})


class SendRecorder:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def body(self):
        return b"".join(
            m.get("body", b"") for m in self.messages
            if m["type"] == "http.response.body"
        )


def make_mw(**kwargs):
    downstream = DownstreamApp()
    allowlist = kwargs.pop("allowlist", "")
    if isinstance(allowlist, str):
        allowlist = frozenset(x for x in allowlist.split(",") if x)
    mw = ApiKeyAuthMiddleware(
        downstream,
        protected_path="/mcp",
        static_allowlist=allowlist,
        **kwargs,
    )
    return mw, downstream


# ---------------------------------------------------------------- basics


class TestPassthrough:
    """Requests that must never be blocked."""

    async def test_non_http_scope_passthrough(self):
        mw, down = make_mw()
        rec = SendRecorder()
        scope = {"type": "websocket", "path": "/mcp"}
        await mw(scope, await fake_receive(), rec)
        assert down.scopes == [scope]
        assert rec.status == 200

    async def test_other_path_passthrough_without_key(self):
        mw, down = make_mw()
        rec = SendRecorder()
        await mw(make_scope(path="/health", headers={}), await fake_receive(), rec)
        assert len(down.scopes) == 1
        assert rec.status == 200

    async def test_options_passthrough(self):
        mw, down = make_mw()
        rec = SendRecorder()
        await mw(make_scope(method="OPTIONS"), await fake_receive(), rec)
        assert len(down.scopes) == 1

    async def test_initialize_is_anonymous(self):
        mw, down = make_mw()
        rec = SendRecorder()
        await mw(make_scope(body=PUBLIC_BODY), await fake_receive(PUBLIC_BODY), rec)
        assert rec.status == 200
        assert down.bodies == [PUBLIC_BODY]

    async def test_initialized_notification_is_anonymous(self):
        mw, down = make_mw()
        body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
        rec = SendRecorder()
        await mw(make_scope(body=body), await fake_receive(body), rec)
        assert rec.status == 200

    async def test_ping_is_anonymous(self):
        mw, down = make_mw()
        body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode()
        rec = SendRecorder()
        await mw(make_scope(body=body), await fake_receive(body), rec)
        assert rec.status == 200


# ------------------------------------------------------------ protected


class TestProtectedMethods:
    """tools/* and other JSON-RPC methods require a valid key."""

    async def test_missing_key_rejected_401(self):
        mw, down = make_mw()
        rec = SendRecorder()
        await mw(make_scope(headers={}), await fake_receive(PROTECTED_BODY), rec)
        assert rec.status == 401
        payload = json.loads(rec.body)
        assert payload["error"]["code"] == -32001
        assert payload["error"]["data"]["error_code"] == "AUTH_MISSING_KEY"
        assert payload["error"]["data"]["retryable"] is False
        assert down.scopes == []  # never reached downstream

    async def test_invalid_key_rejected_401(self):
        mw, down = make_mw()
        mw._verify_planes = AsyncMock(return_value="invalid")
        rec = SendRecorder()
        await mw(make_scope(headers={"X-API-Key": "bad"}), await fake_receive(), rec)
        assert rec.status == 401
        payload = json.loads(rec.body)
        assert payload["error"]["data"]["error_code"] == "AUTH_INVALID_KEY"
        assert down.scopes == []

    async def test_plane_mismatch_rejected_401(self):
        """IS-only keys are rejected before any tool runs, not as 40102."""
        mw, down = make_mw()
        mw._verify_planes = AsyncMock(return_value="plane_mismatch")
        rec = SendRecorder()
        await mw(make_scope(headers={"X-API-Key": "sk_legacy"}),
                 await fake_receive(), rec)
        assert rec.status == 401
        payload = json.loads(rec.body)
        assert payload["error"]["data"]["error_code"] == "AUTH_KEY_PLANE_MISMATCH"
        assert payload["error"]["data"]["retryable"] is False
        assert "casee_..." in payload["error"]["data"]["hint"]
        assert down.scopes == []

    async def test_allowlisted_key_passes_without_upstream(self):
        mw, down = make_mw(allowlist="emergency-key-1")
        rec = SendRecorder()
        await mw(make_scope(headers={"X-API-Key": "emergency-key-1"}),
                 await fake_receive(), rec)
        assert rec.status == 200
        assert len(down.scopes) == 1

    async def test_valid_upstream_key_passes(self):
        mw, down = make_mw()
        mw._verify_planes = AsyncMock(return_value="ok")
        rec = SendRecorder()
        await mw(make_scope(headers={"X-API-Key": "good"}),
                 await fake_receive(PROTECTED_BODY), rec)
        assert rec.status == 200
        assert len(down.scopes) == 1
        # body must be replayed to downstream intact
        assert down.bodies == [PROTECTED_BODY]

    async def test_upstream_unreachable_returns_503(self):
        mw, down = make_mw()
        mw._verify_planes = AsyncMock(return_value=None)
        rec = SendRecorder()
        await mw(make_scope(headers={"X-API-Key": "k"}), await fake_receive(), rec)
        assert rec.status == 503
        payload = json.loads(rec.body)
        assert payload["error"]["data"]["error_code"] == "UPSTREAM_UNAVAILABLE"
        assert payload["error"]["data"]["retryable"] is True
        # Retry-After header present (header names are lowercased by Starlette)
        start = next(m for m in rec.messages if m["type"] == "http.response.start")
        header_pairs = [(k.decode().lower(), v.decode())
                        for k, v in start["headers"]]
        assert ("retry-after", "10") in header_pairs

    async def test_malformed_json_fails_closed(self):
        mw, down = make_mw()
        rec = SendRecorder()
        await mw(make_scope(body=b"{not-json", headers={}),
                 await fake_receive(b"{not-json"), rec)
        assert rec.status == 401
        assert down.scopes == []

    async def test_get_without_key_rejected(self):
        mw, down = make_mw()
        rec = SendRecorder()
        await mw(make_scope(method="GET"), await fake_receive(), rec)
        assert rec.status == 401

    async def test_get_with_allowlisted_key_passes(self):
        mw, down = make_mw(allowlist="ek")
        rec = SendRecorder()
        await mw(make_scope(method="GET", headers={"X-API-Key": "ek"}),
                 await fake_receive(), rec)
        assert rec.status == 200


# ------------------------------------------------------- batch semantics


class TestBatchPayloadSemantics:
    """Batch payloads are public only when every sub-request is public."""

    async def test_batch_all_public_passes(self):
        mw, _ = make_mw()
        body = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]).encode()
        rec = SendRecorder()
        await mw(make_scope(body=body), await fake_receive(body), rec)
        assert rec.status == 200

    async def test_batch_with_one_protected_requires_auth(self):
        mw, _ = make_mw()
        body = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]).encode()
        rec = SendRecorder()
        await mw(make_scope(body=body), await fake_receive(body), rec)
        assert rec.status == 401


# ------------------------------------------------------------- upstream


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class TestUpstreamVerification:
    """_verify_planes probes is-server first, then the EIRS OpenAPI plane."""

    def _mw(self):
        mw, _ = make_mw()
        mw.upstream_base_url = "http://upstream:8000"
        mw.openapi_base_url = "http://eirs:8009/api/v1/openapi"
        return mw

    async def test_both_planes_200_means_ok(self):
        mw = self._mw()
        with patch("casee_mcp_server.http_auth.httpx.AsyncClient") as cls:
            cls.return_value.get = AsyncMock(
                return_value=FakeResponse(200))
            assert await mw._verify_planes("k") == "ok"
            assert cls.return_value.get.await_count == 2
            urls = [c.args[0] for c in cls.return_value.get.await_args_list]
            assert urls == [
                "http://upstream:8000/v1/stats",
                "http://eirs:8009/api/v1/openapi/keys/whoami",
            ]

    async def test_is_401_means_invalid_and_skips_eirs(self):
        mw = self._mw()
        with patch("casee_mcp_server.http_auth.httpx.AsyncClient") as cls:
            cls.return_value.get = AsyncMock(
                return_value=FakeResponse(401, {}))
            assert await mw._verify_planes("k") == "invalid"
            assert cls.return_value.get.await_count == 1

    async def test_eirs_40102_means_plane_mismatch(self):
        mw = self._mw()
        with patch("casee_mcp_server.http_auth.httpx.AsyncClient") as cls:
            cls.return_value.get = AsyncMock(side_effect=[
                FakeResponse(200),
                FakeResponse(401, {"code": 40102,
                                   "details": {"reason": "not_found"}}),
            ])
            assert await mw._verify_planes("k") == "plane_mismatch"

    async def test_eirs_expired_or_disabled_is_plain_invalid(self):
        for code in (40103, 40302):
            mw = self._mw()
            with patch("casee_mcp_server.http_auth.httpx.AsyncClient") as cls:
                cls.return_value.get = AsyncMock(side_effect=[
                    FakeResponse(200),
                    FakeResponse(401, {"code": code}),
                ])
                assert await mw._verify_planes("k") == "invalid"

    async def test_network_error_returns_none(self):
        import httpx as _httpx
        mw = self._mw()
        with patch("casee_mcp_server.http_auth.httpx.AsyncClient") as cls:
            cls.return_value.get = AsyncMock(
                side_effect=_httpx.ConnectError("refused"))
            assert await mw._verify_planes("k") is None

    async def test_unexpected_status_returns_none(self):
        mw = self._mw()
        with patch("casee_mcp_server.http_auth.httpx.AsyncClient") as cls:
            cls.return_value.get = AsyncMock(return_value=FakeResponse(500))
            assert await mw._verify_planes("k") is None

    async def test_empty_is_url_means_invalid(self):
        mw, _ = make_mw()
        mw.upstream_base_url = ""
        assert await mw._verify_planes("k") == "invalid"

    async def test_missing_openapi_base_skips_dual_check(self):
        mw, _ = make_mw()
        mw.upstream_base_url = "http://upstream:8000"
        mw.openapi_base_url = ""
        with patch("casee_mcp_server.http_auth.httpx.AsyncClient") as cls:
            cls.return_value.get = AsyncMock(return_value=FakeResponse(200))
            assert await mw._verify_planes("k") == "ok"
            assert cls.return_value.get.await_count == 1


class TestVerifyCache:
    """Positive/negative caching short-circuits upstream calls."""

    async def test_positive_cache_hit_skips_upstream(self):
        mw, _ = make_mw()
        mw._verify_planes = AsyncMock(return_value="ok")
        assert await mw._verify_cached("k") == "ok"
        assert await mw._verify_cached("k") == "ok"
        assert mw._verify_planes.await_count == 1

    async def test_negative_cache_uses_short_ttl(self):
        mw, _ = make_mw()
        mw._verify_planes = AsyncMock(return_value="invalid")
        assert await mw._verify_cached("k") == "invalid"
        assert await mw._verify_cached("k") == "invalid"
        assert mw._verify_planes.await_count == 1
        # negative TTL shorter than positive
        assert mw.neg_cache_ttl < mw.cache_ttl

    async def test_expired_entry_reverified(self):
        mw, _ = make_mw()
        mw._verify_planes = AsyncMock(return_value="ok")
        await mw._verify_cached("k")
        mw._cache["k"] = ("ok", mw._cache["k"][1] - 10_000)  # far in the past
        await mw._verify_cached("k")
        assert mw._verify_planes.await_count == 2

    async def test_plane_mismatch_is_cached(self):
        mw, _ = make_mw()
        mw._verify_planes = AsyncMock(return_value="plane_mismatch")
        assert await mw._verify_cached("k") == "plane_mismatch"
        assert await mw._verify_cached("k") == "plane_mismatch"
        assert mw._verify_planes.await_count == 1

    async def test_unreachable_is_not_cached(self):
        mw, _ = make_mw()
        mw._verify_planes = AsyncMock(return_value=None)
        assert await mw._verify_cached("k") is None
        assert await mw._verify_cached("k") is None
        assert mw._verify_planes.await_count == 2


# ------------------------------------------------------------- builders


class TestBuildAuthenticatedApp:
    """build_authenticated_app env-driven wiring."""

    async def test_auth_disabled_returns_raw_app(self, monkeypatch):
        monkeypatch.setenv("MCP_REQUIRE_API_KEY", "false")
        from casee_mcp_server.server import mcp as mcp_server
        app = build_authenticated_app(mcp_server, path="/mcp")
        assert not isinstance(app, ApiKeyAuthMiddleware)

    async def test_auth_enabled_by_default(self, monkeypatch):
        """托管部署默认开启鉴权，避免租户归属塌缩到部署 Key。"""
        monkeypatch.delenv("MCP_REQUIRE_API_KEY", raising=False)
        from casee_mcp_server.server import mcp as mcp_server
        app = build_authenticated_app(mcp_server, path="/mcp")
        assert isinstance(app, ApiKeyAuthMiddleware)

    async def test_auth_enabled_wraps_middleware(self, monkeypatch):
        monkeypatch.setenv("MCP_REQUIRE_API_KEY", "true")
        monkeypatch.setenv("MCP_ALLOWED_API_KEYS", "a, b")
        monkeypatch.setenv("CASEE_API_BASE_URL", "http://up:8000")
        monkeypatch.setenv("CASEE_OPENAPI_BASE_URL", "http://eirs:8009/api/v1/openapi")
        monkeypatch.setenv("MCP_AUTH_CACHE_TTL", "77")
        monkeypatch.setenv("MCP_AUTH_NEG_CACHE_TTL", "7")
        from casee_mcp_server.server import mcp as mcp_server
        mw = build_authenticated_app(mcp_server, path="/mcp")
        assert isinstance(mw, ApiKeyAuthMiddleware)
        assert mw.static_allowlist == frozenset({"a", "b"})
        assert mw.upstream_base_url == "http://up:8000"
        assert mw.openapi_base_url == "http://eirs:8009/api/v1/openapi"
        assert mw.cache_ttl == 77.0
        assert mw.neg_cache_ttl == 7.0


def test_env_bool_parsing():
    assert _env_bool("X", default=False) is False  # not set
    assert _env_bool("X", default=True) is True    # not set, default True
    import os
    for val in ("1", "true", "True", "yes", "ON"):
        os.environ["X"] = val
        try:
            assert _env_bool("X") is True
        finally:
            del os.environ["X"]
    for val in ("0", "false", "no", "off", "junk"):
        os.environ["X"] = val
        try:
            assert _env_bool("X") is False
        finally:
            del os.environ["X"]


def test_public_methods_frozenset():
    assert _PUBLIC_METHODS == frozenset(
        {"initialize", "ping", "notifications/initialized"})


# ------------------------------------------------ caller key propagation


class _FakeCtx:
    """Minimal ServerRequestContext stand-in: only ``request.headers`` is read."""

    def __init__(self, headers):
        self.request = type("FakeRequest", (), {"headers": headers})()


class TestCallerKeyPropagation:
    """The key binds in the MCP session task and reaches sync tools.

    Regression guard: binding in the ASGI middleware looked correct but never
    reached tool handlers, so every tool silently used the deployment key.
    """

    async def test_handler_and_worker_thread_see_caller_key(self):
        from casee_mcp_server.request_context import (
            bind_caller_api_key,
            get_caller_api_key,
        )

        seen = {}

        async def call_next(ctx):
            seen["direct"] = get_caller_api_key()
            # Sync tools execute via anyio.to_thread; the value must follow.
            seen["worker"] = await anyio.to_thread.run_sync(get_caller_api_key)
            return "ok"

        result = await bind_caller_api_key(
            _FakeCtx({"x-api-key": "casee_k1"}), call_next)
        assert result == "ok"
        assert seen == {"direct": "casee_k1", "worker": "casee_k1"}
        assert get_caller_api_key() == ""  # reset after the message

    async def test_missing_header_binds_empty_and_clears_previous(self):
        from casee_mcp_server.request_context import (
            bind_caller_api_key,
            get_caller_api_key,
        )

        seen = {}

        async def call_next(ctx):
            seen["key"] = get_caller_api_key()

        await bind_caller_api_key(_FakeCtx({}), call_next)
        assert seen["key"] == ""

    def test_server_registers_caller_key_middleware(self):
        from casee_mcp_server.request_context import bind_caller_api_key
        from casee_mcp_server.server import mcp as mcp_server

        assert bind_caller_api_key in mcp_server._lowlevel_server.middleware


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
