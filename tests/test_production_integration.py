"""Live production smoke tests for the deployed MCP server.

Run against the public endpoint (default https://causalai.me:8100/mcp).
These tests hit the real production service, so they are SKIPPED unless
the environment variable CASEE_MCP_LIVE=1 is set.

Required env:
    CASEE_MCP_LIVE=1            enable live tests
    CASEE_MCP_TEST_KEY=<key>    a valid CaSee API key (X-API-Key)

Optional env:
    CASEE_MCP_URL               endpoint, default https://causalai.me:8100/mcp
"""
import json
import os

import pytest

requests = pytest.importorskip("requests")

BASE_URL = os.environ.get("CASEE_MCP_URL", "https://causalai.me:8100/mcp")
API_KEY = os.environ.get("CASEE_MCP_TEST_KEY", "")
RUN_LIVE = os.environ.get("CASEE_MCP_LIVE", "") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_LIVE, reason="live production tests disabled (set CASEE_MCP_LIVE=1)"
)

JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def post(payload, *, api_key=API_KEY, session_id=None, timeout=60):
    headers = dict(JSON_HEADERS)
    if api_key:
        headers["X-API-Key"] = api_key
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return requests.post(BASE_URL, headers=headers,
                         data=json.dumps(payload), timeout=timeout)


def extract_session_id(resp):
    for header in ("mcp-session-id", "Mcp-Session-Id"):
        if header in resp.headers:
            return resp.headers[header]
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data.get("sessionId") or data.get("session_id")
    except Exception:
        pass
    return ""


def parse_jsonrpc(resp):
    """Parse a JSON or SSE-framed JSON-RPC response body.

    NOTE: must NOT use str.splitlines() — payload strings may contain raw
    U+2028/U+2029 (common in CJK text), which splitlines() treats as line
    boundaries and truncates the JSON mid-string. Split on \n only and
    re-join SSE `data:` fields per the SSE framing spec.
    """
    text = resp.text.strip()
    try:
        return resp.json()
    except ValueError:
        pass
    data_lines = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        return json.loads("\n".join(data_lines))
    raise ValueError("no SSE data found in response")


@pytest.fixture(scope="module")
def session():
    """Anonymous MCP handshake; yields (session_id, server_info)."""
    resp = post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "casee-live-test", "version": "1.2.2"},
        },
    })
    assert resp.status_code == 200, resp.text
    data = parse_jsonrpc(resp)
    assert "result" in data, data
    sid = extract_session_id(resp)
    # protocol completion notification (anonymous)
    post({"jsonrpc": "2.0", "method": "notifications/initialized"},
         session_id=sid)
    return sid, data["result"].get("serverInfo", {})


@pytest.mark.skipif(not API_KEY, reason="CASEE_MCP_TEST_KEY not set")
class TestProductionLive:
    """Full production verification: handshake, auth, all tools."""

    def test_initialize_server_info(self, session):
        _, info = session
        assert "casee" in info.get("name", "").lower()
        assert info.get("version")

    def test_unauthenticated_tools_list_rejected(self, session):
        resp = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                     "params": {}}, api_key="")
        assert resp.status_code == 401

    def test_invalid_key_rejected(self, session):
        resp = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                     "params": {}}, api_key="definitely-invalid-key")
        assert resp.status_code == 401

    def test_tools_list_with_key(self, session):
        sid, _ = session
        resp = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                     "params": {}}, session_id=sid)
        assert resp.status_code == 200
        data = parse_jsonrpc(resp)
        tools = {t["name"] for t in data["result"]["tools"]}
        assert len(tools) == 14
        for expected in ("find_trusted_sources", "search_intelligence",
                         "analyze_trend", "aggregate_by_source",
                         "semantic_search_tool", "search_with_cvc",
                         "batch_search", "list_cvc_models_tool",
                         "cvc_stats_tool", "cvc_cache_purge_tool",
                         "cvc_cleanup_status_tool", "cvc_reindex_tool",
                         "intelligence_purge_tool",
                         "intelligence_purge_status_tool"):
            assert expected in tools

    def _call_raw(self, sid, name, arguments, call_id):
        """Call a tool and return the parsed payload without isError checks.

        On protocol-level tool errors (isError=True) the content text is a
        plain message, not JSON — return it wrapped instead of failing.
        """
        resp = post({
            "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, session_id=sid)
        assert resp.status_code == 200, f"{name}: HTTP {resp.status_code}"
        data = parse_jsonrpc(resp)
        assert "result" in data, f"{name}: {data}"
        texts = [c.get("text", "") for c in data["result"].get("content", [])
                 if c.get("type") == "text"]
        if not texts:
            return {}
        if data["result"].get("isError"):
            return {"isError": True, "text": texts[0]}
        try:
            return json.loads(texts[0])
        except ValueError:
            return {"text": texts[0]}

    def _call(self, sid, name, arguments, call_id):
        resp = post({
            "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, session_id=sid)
        assert resp.status_code == 200, f"{name}: HTTP {resp.status_code}"
        data = parse_jsonrpc(resp)
        assert "result" in data, f"{name}: {data}"
        assert not data["result"].get("isError"), f"{name}: {data}"
        texts = [c.get("text", "") for c in data["result"].get("content", [])
                 if c.get("type") == "text"]
        payload = json.loads(texts[0]) if texts else {}
        return payload

    def test_call_find_trusted_sources(self, session):
        sid, _ = session
        payload = self._call(sid, "find_trusted_sources",
                             {"category": "wire", "min_tscore": 0.7, "limit": 5}, 10)
        assert "sources" in payload
        assert isinstance(payload["sources"], list)

    def test_call_search_intelligence(self, session):
        sid, _ = session
        payload = self._call(sid, "search_intelligence",
                             {"q": "+AI", "days": 30, "limit": 3}, 11)
        # upstream returns SerpAPI-style (search_metadata + organic_results);
        # legacy shape ({count, items}) is accepted for forward-compat
        assert "error" not in payload, payload
        assert ("organic_results" in payload) or ("items" in payload), \
            payload.keys()

    def test_call_search_intelligence_no_error_field_in_results(self, session):
        """Sanity: results really carry intelligence content, not HTML."""
        sid, _ = session
        payload = self._call(sid, "search_intelligence",
                             {"q": "+EV battery", "days": 30, "limit": 2}, 19)
        results = payload.get("organic_results") or payload.get("items") or []
        for item in results[:2]:
            assert isinstance(item, dict)
            assert "title" in item

    def test_call_analyze_trend(self, session):
        sid, _ = session
        payload = self._call(sid, "analyze_trend",
                             {"q": "+AI", "days": 30, "limit": 50}, 12)
        assert "trend" in payload and "summary" in payload
        assert "error" not in payload, payload

    def test_call_aggregate_by_source(self, session):
        sid, _ = session
        payload = self._call(sid, "aggregate_by_source",
                             {"q": "+AI", "days": 30, "limit": 50}, 13)
        assert "sources" in payload
        assert "error" not in payload, payload

    def test_call_batch_search(self, session):
        sid, _ = session
        payload = self._call(sid, "batch_search", {
            "groups": [{"id": "g1", "q": "+AI", "days": 30, "limit": 3}],
            "dedupe": True, "max_concurrency": 2,
        }, 14)
        assert "meta" in payload and "results" in payload
        assert "error" not in payload, payload

    def test_call_list_cvc_models(self, session):
        sid, _ = session
        payload = self._call(sid, "list_cvc_models_tool", {}, 15)
        assert "count" in payload or "error" in payload
        assert "models" in payload or "error" in payload

    def test_call_cvc_tools_invalid_id_short_circuit(self, session):
        """Invalid ids are rejected: either by MCP schema validation
        (isError=True with pattern message) or by the tool's own friendly
        InvalidCvcModelId dict."""
        sid, _ = session
        for i, name in enumerate(("cvc_stats_tool", "cvc_cache_purge_tool",
                                  "cvc_cleanup_status_tool",
                                  "cvc_reindex_tool")):
            payload = self._call_raw(sid, name, {"cvc_model_id": "bogus"},
                                     30 + i)
            if payload.get("isError"):
                assert "cvc_" in json.dumps(payload), name
            else:
                assert payload.get("error") == "InvalidCvcModelId", name

    def test_call_intelligence_purge_dry_run(self, session):
        sid, _ = session
        payload = self._call(sid, "intelligence_purge_tool",
                             {"older_than_days": 3650, "dry_run": True,
                              "include_mongodb": False}, 17)
        assert "error" not in payload, payload

    def test_call_intelligence_purge_status(self, session):
        sid, _ = session
        payload = self._call(sid, "intelligence_purge_status_tool", {}, 18)
        assert isinstance(payload, dict)
