"""Tests for MCP server tool registration and main() transport wiring."""
import json
import pytest
from unittest.mock import AsyncMock, patch

import casee_mcp_server.server as server_mod
from casee_mcp_server.server import mcp, main

EXPECTED_TOOLS = {
    "find_trusted_sources",
    "search_intelligence",
    "analyze_trend",
    "aggregate_by_source",
    "semantic_search_tool",
    "search_with_cvc",
    "batch_search",
    "list_cvc_models_tool",
    "cvc_stats_tool",
    "cvc_cache_purge_tool",
    "cvc_cleanup_status_tool",
    "cvc_reindex_tool",
    "intelligence_purge_tool",
    "intelligence_purge_status_tool",
    # v1.4 report surface
    "list_report_templates",
    "plan_report",
    "generate_report",
    "get_report_job",
    "list_report_jobs",
    "share_report",
    "backup_cvc_intelligence",
    "rebuild_cvc_model",
    "casee_health",
    # v1.5 / v1.6 CVC lifecycle
    "search_cvc_companies",
    "create_cvc_model",
    "get_company_search_job",
    "get_cvc_creation_job",
    "get_cvc_build_status",
}


def _schema(tool):
    """Cross-version accessor: mcp SDK exposes inputSchema or input_schema."""
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)


class TestToolRegistration:
    """All 28 documented tools must be registered with usable schemas."""

    async def test_all_tools_registered(self):
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        missing = EXPECTED_TOOLS - names
        extra = names - EXPECTED_TOOLS
        assert not missing, f"missing tools: {missing}"
        assert not extra, f"unexpected tools: {extra}"
        assert len(names) == 28

    async def test_every_tool_has_description_and_schema(self):
        for tool in await mcp.list_tools():
            assert tool.description, f"{tool.name} lacks description"
            schema = _schema(tool)
            assert schema.get("type") == "object", f"{tool.name} schema not object"
            assert "properties" in schema, f"{tool.name} schema lacks properties"

    async def test_required_params_marked(self):
        tools = {t.name: t for t in await mcp.list_tools()}
        # q is required on the search/analysis tools
        for name in ("search_intelligence", "analyze_trend",
                     "aggregate_by_source", "semantic_search_tool"):
            assert "q" in _schema(tools[name]).get("required", []), name
        assert "task_id" not in _schema(tools["intelligence_purge_status_tool"]).get("required", [])

    async def test_cvc_tools_validate_pattern_in_schema(self):
        tools = {t.name: t for t in await mcp.list_tools()}
        for name in ("semantic_search_tool", "cvc_stats_tool",
                     "cvc_cache_purge_tool", "cvc_cleanup_status_tool",
                     "cvc_reindex_tool"):
            prop = _schema(tools[name])["properties"]["cvc_model_id"]
            assert prop.get("pattern") == "^cvc_[a-z0-9]{8,32}$", name

    async def test_report_tools_expose_required_params(self):
        tools = {t.name: t for t in await mcp.list_tools()}
        assert "cvc_id" in _schema(tools["plan_report"]).get("required", [])
        assert "template_id" in _schema(tools["plan_report"]).get("required", [])
        required = _schema(tools["generate_report"]).get("required", [])
        assert {"cvc_id", "template_id"} <= set(required)
        assert "job_id" in _schema(tools["get_report_job"]).get("required", [])
        assert "job_id" in _schema(tools["share_report"]).get("required", [])
        rebuild = _schema(tools["rebuild_cvc_model"])["properties"]
        assert rebuild["confirm"].get("default") is False
        assert rebuild["dry_run"].get("default") is True
        assert rebuild["backup"].get("default") is True


class TestPromptRegistration:
    """v1.6 exposes the CVC-creation recipe alongside v1.4 report recipes."""

    async def test_prompts_registered(self):
        prompts = await mcp.list_prompts()
        names = {p.name for p in prompts}
        assert {"cvc_creation", "report_generation", "retrieval_quality_guard"} <= names

    async def test_cvc_creation_prompt_has_workflow(self):
        result = await mcp.get_prompt("cvc_creation", {"query": "长时储能"})
        text = result.messages[0].content.text
        for tool_name in ("search_cvc_companies", "get_company_search_job",
                          "create_cvc_model", "get_cvc_creation_job",
                          "rebuild_cvc_model", "get_cvc_build_status"):
            assert tool_name in text
        assert "wait_seconds" in text
        assert "默认全选" in text
        assert "10 分钟" in text
        assert "邮件" in text
        assert "长时储能" in text

    async def test_report_generation_prompt_has_workflow(self):
        result = await mcp.get_prompt(
            "report_generation",
            {"cvc_id": "cvc_abcdefgh1234", "template_id": "report.cpm"},
        )
        text = result.messages[0].content.text
        for tool_name in ("list_report_templates", "plan_report", "generate_report",
                          "get_report_job", "share_report"):
            assert tool_name in text
        assert "cvc_abcdefgh1234" in text
        assert "compatibility" in text
        assert "data_gaps" in text

    async def test_retrieval_quality_guard_prompt(self):
        result = await mcp.get_prompt("retrieval_quality_guard", {})
        text = result.messages[0].content.text
        assert "vector_hits" in text
        assert "tscore" in text
        assert "batch_search" in text


class TestMainTransportWiring:
    """main() selects transport per MCP_TRANSPORT and wraps HTTP with auth."""

    def test_streamable_http_uses_authenticated_app(self, monkeypatch):
        monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_PORT", "8100")
        monkeypatch.setenv("MCP_PATH", "/mcp")
        sentinel = object()
        with patch("casee_mcp_server.http_auth.build_authenticated_app",
                   return_value=sentinel) as build, \
             patch("uvicorn.run") as uvi:
            main()
        build.assert_called_once_with(mcp, path="/mcp", json_response=False)
        uvi.assert_called_once()
        assert uvi.call_args[0][0] is sentinel
        assert uvi.call_args[1]["host"] == "0.0.0.0"
        assert uvi.call_args[1]["port"] == 8100

    def test_stdio_transport(self, monkeypatch):
        monkeypatch.setenv("MCP_TRANSPORT", "stdio")
        with patch.object(mcp, "run") as run:
            main()
        run.assert_called_once_with(transport="stdio")

    def test_json_response_flag_forwarded(self, monkeypatch):
        monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
        monkeypatch.setenv("MCP_JSON_RESPONSE", "true")
        sentinel = object()
        with patch("casee_mcp_server.http_auth.build_authenticated_app",
                   return_value=sentinel) as build, \
             patch("uvicorn.run"):
            main()
        build.assert_called_once_with(mcp, path="/mcp", json_response=True)

    def test_sse_transport(self, monkeypatch):
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        monkeypatch.setenv("MCP_PORT", "8200")
        with patch.object(mcp, "run") as run:
            main()
        run.assert_called_once_with(transport="sse", host="127.0.0.1", port=8200)

    def test_unknown_transport_falls_back_to_stdio(self, monkeypatch):
        monkeypatch.setenv("MCP_TRANSPORT", "websocket")
        with patch.object(mcp, "run") as run:
            main()
        run.assert_called_once_with(transport="stdio")


class TestVersionConsistency:
    """pyproject and package __version__ must agree (release hygiene)."""

    def test_versions_match(self):
        import casee_mcp_server as pkg
        import tomllib

        root = server_mod.__file__.split("/src/")[0]
        with open(f"{root}/pyproject.toml", "rb") as fh:
            pyproject_version = tomllib.load(fh)["project"]["version"]
        assert pkg.__version__ == pyproject_version


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
