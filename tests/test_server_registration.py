"""Tests for MCP server tool registration and main() transport wiring."""
import json
import pytest
from unittest.mock import AsyncMock, patch

import casee_mcp_server.server as server_mod
from casee_mcp_server.server import mcp, main

EXPECTED_TOOLS = {
    "search_intelligence",
    "analyze_trend",
    "aggregate_intelligence",
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
    # v1.8 industry-template creation mode
    "list_cvc_templates",
    "get_cvc_template",
    "create_cvc_from_template",
    "get_cvc_template_provision_status",
    # v1.7 OpenAPI coverage: billing / quota
    "casee_get_credits",
    "casee_get_credit_ledger",
    "casee_get_quota",
    # v1.12 UX entry guidance / decision hints
    "casee_guide",
    "casee_plan_task",
    # v1.7 discovery
    "casee_list_endpoints",
    "casee_get_endpoint",
    "casee_list_mcp_tools",
    # v1.7 charts
    "casee_search_charts",
    "casee_get_chart",
    "casee_preview_chart",
    "casee_get_chart_bundle",
    # v1.7 templates
    "casee_get_report_template",
    "casee_download_report_template",
    "casee_create_report_template",
    "casee_update_report_template",
    "casee_delete_report_template",
    "casee_validate_report_template",
    # v1.7 report artifacts
    "casee_download_report",
    "casee_issue_report_access_code",
    # v1.7 CVC inspection / insights
    "casee_home_overview",
    "casee_get_cvc_model",
    "casee_list_cvc_companies",
    "casee_list_cvc_entities",
    "casee_get_cvc_entity",
    "casee_get_ontology_schema",
    "casee_export_ontology",
    "casee_search_cvc_intelligence",
    "casee_get_intelligence_detail",
    "casee_export_intelligence",
    "casee_get_cvc_dashboard",
    "casee_get_market_trends",
    "casee_get_enterprise_insight",
    "casee_get_market_insight",
    "casee_get_customer_insight",
}


def _schema(tool):
    """Cross-version accessor: mcp SDK exposes inputSchema or input_schema."""
    return getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)


class TestToolRegistration:
    """All 66 documented tools must be registered with usable schemas."""

    @pytest.fixture(autouse=True)
    def _full_profile(self, monkeypatch):
        """注册表断言针对完整目录，固定 full 以免被默认 core 过滤影响。"""
        monkeypatch.setenv("MCP_TOOL_PROFILE", "full")

    async def test_all_tools_registered(self):
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        missing = EXPECTED_TOOLS - names
        extra = names - EXPECTED_TOOLS
        assert not missing, f"missing tools: {missing}"
        assert not extra, f"unexpected tools: {extra}"
        assert len(names) == 66

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
                     "aggregate_intelligence", "semantic_search_tool"):
            assert "q" in _schema(tools[name]).get("required", []), name
        assert "task_id" not in _schema(tools["intelligence_purge_status_tool"]).get("required", [])

    async def test_cvc_tools_validate_pattern_in_schema(self):
        """Bad cvc ids must reach the tool body (structured error), not the protocol.

        Rationale: a JSON-Schema ``pattern`` makes the MCP layer reject the call
        with raw protocol text that agent platforms cannot render. The pattern is
        therefore advertised in the description and enforced by the tool, which
        returns ``{ok:false, error_code:"VALIDATION_ERROR", hint, action}``.
        """
        from casee_mcp_server.server import semantic_search_tool

        tools = {t.name: t for t in await mcp.list_tools()}
        for name in ("semantic_search_tool", "cvc_stats_tool",
                     "cvc_cache_purge_tool", "cvc_cleanup_status_tool",
                     "cvc_reindex_tool"):
            prop = _schema(tools[name])["properties"]["cvc_model_id"]
            assert prop.get("pattern") is None, name
            assert "cvc_" in (prop.get("description") or ""), name

        rejected = semantic_search_tool(cvc_model_id="bad id/../x", q="x")
        assert rejected["ok"] is False
        assert rejected["error_code"] == "VALIDATION_ERROR"
        assert rejected["hint"] and rejected["action"]
        assert rejected["alternatives"]

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

    async def test_creation_modes_exposed_in_schema(self):
        """建链三种方式（智能推理 / 精确定义 / 行业模版）必须可从工具签名发现。"""
        tools = {t.name: t for t in await mcp.list_tools()}
        create_props = _schema(tools["create_cvc_model"])["properties"]
        assert "objective" in create_props
        assert "objective_definition" in create_props
        assert "create_cvc_from_template" in tools
        template_props = _schema(tools["create_cvc_from_template"])["properties"]
        assert template_props["dry_run"].get("default") is True
        assert template_props["confirm"].get("default") is False
        assert "template_id" in _schema(tools["get_cvc_template"]).get("required", [])
        assert "template_id" in _schema(
            tools["get_cvc_template_provision_status"]).get("required", [])
        list_props = _schema(tools["list_cvc_templates"])["properties"]
        assert {"industry", "scenario", "q", "limit", "offset"} <= set(list_props)


class TestToolProfile:
    """MCP_TOOL_PROFILE 控制 tools/list 暴露范围（调用能力不受影响）。"""

    async def test_default_profile_is_core(self, monkeypatch):
        monkeypatch.delenv("MCP_TOOL_PROFILE", raising=False)
        from casee_mcp_server.server import (
            CORE_TOOL_NAMES,
            all_tool_names,
            get_tool_profile,
            visible_tool_names,
        )

        assert get_tool_profile() == "core"
        visible = {t.name for t in await mcp.list_tools()}
        assert visible == set(visible_tool_names())
        assert visible <= CORE_TOOL_NAMES
        assert 10 <= len(visible) <= 18
        # 隐藏工具仍在注册表中，可被显式调用
        assert len(all_tool_names()) == 66

    async def test_full_profile_exposes_everything(self, monkeypatch):
        monkeypatch.setenv("MCP_TOOL_PROFILE", "full")
        from casee_mcp_server.server import all_tool_names

        assert {t.name for t in await mcp.list_tools()} == set(all_tool_names())

    async def test_invalid_profile_falls_back_to_core(self, monkeypatch):
        monkeypatch.setenv("MCP_TOOL_PROFILE", "bogus")
        from casee_mcp_server.server import get_tool_profile

        assert get_tool_profile() == "core"

    def test_guide_reports_hidden_tools(self, monkeypatch):
        monkeypatch.delenv("MCP_TOOL_PROFILE", raising=False)
        from casee_mcp_server.guidance import casee_guide

        profile = casee_guide()["data"]["tool_profile"]
        assert profile["profile"] == "core"
        assert profile["visible_tools"] <= profile["total_tools"]
        assert profile["hidden_tools"]
        assert profile["enable_full"] == "MCP_TOOL_PROFILE=full"

    def test_guide_exposes_three_creation_modes(self):
        """智能推理 / 精确定义 / 行业模版：引导必须能区分三种建链路径。"""
        from casee_mcp_server.guidance import casee_guide, casee_plan_task

        data = casee_guide("cvc")["data"]
        modes = {m["mode"] for m in data["creation_modes"]["modes"]}
        assert modes == {"smart", "precise", "template"}
        plan = casee_plan_task("持续跟踪长时储能进展")["data"]
        assert "creation_modes" in plan


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
                          "rebuild_cvc_model", "get_cvc_build_status",
                          "list_cvc_templates", "create_cvc_from_template",
                          "get_cvc_template_provision_status"):
            assert tool_name in text
        # 三种建链方式都必须出现在提示词里
        assert "objective_definition" in text
        assert "smart" in text and "precise" in text and "模版" in text
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
