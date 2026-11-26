"""Tests for the v1.6 CVC lifecycle tools (async search / create / job polling).

All HTTP traffic is served by ``httpx.MockTransport``; no network is used.
"""
import json

import httpx

from casee_mcp_server import server as srv
from casee_mcp_server.reports import ReportClient


def _ok(data):
    return httpx.Response(200, json={"code": 200, "message": "ok", "data": data})


def _install(monkeypatch, handler):
    client = ReportClient(
        api_key="casee_test",
        base_url="https://api.test",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    monkeypatch.setattr(srv, "get_report_client", lambda: client)
    return client


class TestSearchCvcCompanies:
    def test_creates_job_then_returns_selection_defaults(self, monkeypatch):
        seen = {"paths": []}

        def handler(request):
            seen["paths"].append(request.url.path)
            if request.method == "POST":
                seen["body"] = json.loads(request.content)
                return _ok({"job_id": "csjob-test", "status": "queued",
                            "kind": "company_search"})
            return _ok({
                "job_id": "csjob-test",
                "status": "succeeded",
                "result": {
                    "items": [{"name": "大连融科储能", "selected": True,
                               "products": ["全钒液流电池"]}],
                    "total": 1,
                    "default_selection": "all",
                    "selection_hint": "已默认全选",
                    "suggested_cvc_name": "价值链-长时储能",
                },
            })

        _install(monkeypatch, handler)
        res = srv.search_cvc_companies(query="长时储能", limit=5)

        assert res["ok"] is True
        assert seen["paths"][0].endswith("/companies/search-jobs")
        assert seen["paths"][1].endswith("/companies/search-jobs/csjob-test")
        assert seen["body"] == {"query": "长时储能", "language": "zh", "limit": 5}
        assert res["data"]["default_selection"] == "all"
        assert res["data"]["items"][0]["selected"] is True

    def test_wait_zero_returns_job_without_polling(self, monkeypatch):
        seen = {"paths": []}

        def handler(request):
            seen["paths"].append(request.url.path)
            return _ok({"job_id": "csjob-test", "status": "queued",
                        "kind": "company_search"})

        _install(monkeypatch, handler)
        res = srv.search_cvc_companies(query="长时储能", wait_seconds=0)

        assert res["ok"] is True
        assert res["data"]["job_id"] == "csjob-test"
        assert res["data"]["status"] == "queued"
        assert res["data"]["next_actions"][0]["tool"] == "get_company_search_job"
        assert len(seen["paths"]) == 1

    def test_rejects_short_query_without_http(self, monkeypatch):
        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("HTTP must not be called")

        _install(monkeypatch, handler)
        res = srv.search_cvc_companies(query="储")

        assert res["ok"] is False
        assert res["error_code"] == "VALIDATION_ERROR"
        assert res["retryable"] is False

    def test_surfaces_structured_backend_error(self, monkeypatch):
        def handler(request):
            return httpx.Response(404, json={
                "code": 40401, "error_code": "CVC_NOT_FOUND",
                "message": "not found", "hint": "check cvc_id",
            })

        _install(monkeypatch, handler)
        res = srv.search_cvc_companies(query="长时储能")

        assert res["ok"] is False
        assert res["status_code"] == 404
        assert res["error_code"]

    def test_precise_definition_composes_query(self, monkeypatch):
        """企业检索也支持精确定义：服务端组合检索词并透传结构化定义。"""
        seen = {}

        def handler(request):
            if request.method == "POST":
                seen["body"] = json.loads(request.content)
                return _ok({"job_id": "csjob-test", "status": "queued",
                            "kind": "company_search"})
            raise AssertionError("wait_seconds=0 must not poll")

        _install(monkeypatch, handler)
        res = srv.search_cvc_companies(
            query="",
            wait_seconds=0,
            objective_definition={
                "mode": "precise",
                "target_regions": ["东南亚"],
                "target_competitors": "Gemini",
            },
        )

        assert res["ok"] is True
        assert seen["body"]["query"] == "目标地区：东南亚；目标竞品：Gemini"
        assert seen["body"]["objective_definition"]["mode"] == "precise"
        assert res["data"]["job_id"] == "csjob-test"


class TestCreateCvcModel:
    def test_creates_job_then_adds_next_step(self, monkeypatch):
        seen = {"paths": []}

        def handler(request):
            seen["paths"].append(request.url.path)
            if request.method == "POST":
                seen["body"] = json.loads(request.content)
                seen["idem"] = request.headers.get("Idempotency-Key")
                return _ok({"job_id": "cvcjob-test", "status": "queued",
                            "kind": "cvc_create"})
            return _ok({
                "job_id": "cvcjob-test",
                "status": "succeeded",
                "result": {
                    "cvc_id": "cvc_abcd1234",
                    "status": "draft",
                    "build_readiness": {"ready": True, "warnings": []},
                },
            })

        _install(monkeypatch, handler)
        res = srv.create_cvc_model(
            name="价值链-长时储能",
            companies=["大连融科储能", {"name": "宁德时代", "products": ["储能电池"]}],
            query_topic="储能 长时储能",
            idempotency_key="abcdefgh",
        )

        assert res["ok"] is True
        assert seen["paths"][0].endswith("/cvc-models/creation-jobs")
        assert seen["paths"][1].endswith("/cvc-models/creation-jobs/cvcjob-test")
        assert seen["body"]["companies"] == [
            "大连融科储能", {"name": "宁德时代", "products": ["储能电池"]},
        ]
        assert seen["body"]["query_topic"] == "储能 长时储能"
        assert seen["idem"] == "abcdefgh"
        assert "10 分钟" in res["data"]["user_message"]
        assert res["data"]["next_step"]["tool"] == "rebuild_cvc_model"
        assert res["data"]["next_step"]["args"]["confirm"] is True

    def test_requires_at_least_one_company(self, monkeypatch):
        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("HTTP must not be called")

        _install(monkeypatch, handler)
        res = srv.create_cvc_model(name="x", companies=[])

        assert res["ok"] is False
        assert res["error_code"] == "VALIDATION_ERROR"

    def test_rejects_invalid_company_entries(self, monkeypatch):
        _install(monkeypatch, lambda request: _ok({}))
        res = srv.create_cvc_model(name="x", companies=[{"products": ["p"]}])

        assert res["ok"] is False
        assert "companies[0]" in res["message"]

    def test_passes_precise_objective_definition(self, monkeypatch):
        """精确定义：四维按 地区→客户→竞品→行业规约 组合后透传，与主页同口径。"""
        seen = {}

        def handler(request):
            if request.method == "POST":
                seen["body"] = json.loads(request.content)
                return _ok({"job_id": "cvcjob-test", "status": "queued",
                            "kind": "cvc_create"})
            return _ok({
                "job_id": "cvcjob-test",
                "status": "succeeded",
                "result": {
                    "cvc_id": "cvc_abcd1234",
                    "status": "draft",
                    "creation_mode": "precise",
                },
            })

        _install(monkeypatch, handler)
        res = srv.create_cvc_model(
            name="价值链-欧洲储能",
            companies=["大连融科储能"],
            objective_definition={
                "mode": "precise",
                "target_regions": ["欧洲", "北美"],
                "target_customers": "电网侧储能集成商",
                "target_competitors": "Fluence",
                "target_industry_standards": "IEC 62933",
            },
        )

        assert res["ok"] is True
        body = seen["body"]
        assert body["objective_definition"]["mode"] == "precise"
        assert body["objective_definition"]["target_regions"] == ["欧洲", "北美"]
        assert body["objective"] == (
            "目标地区：欧洲、北美；目标客户：电网侧储能集成商；"
            "目标竞品：Fluence；行业规约：IEC 62933"
        )
        assert res["data"]["creation_mode"] == "precise"
        assert res["data"]["objective"] == body["objective"]

    def test_smart_objective_passed_through(self, monkeypatch):
        seen = {}

        def handler(request):
            if request.method == "POST":
                seen["body"] = json.loads(request.content)
                return _ok({"job_id": "cvcjob-test", "status": "queued",
                            "kind": "cvc_create"})
            return _ok({"job_id": "cvcjob-test", "status": "succeeded",
                        "result": {"cvc_id": "cvc_abcd1234", "status": "draft"}})

        _install(monkeypatch, handler)
        res = srv.create_cvc_model(
            name="价值链-长时储能", companies=["大连融科储能"],
            objective="分析长时储能行业竞争态势",
        )

        assert res["ok"] is True
        assert seen["body"]["objective"] == "分析长时储能行业竞争态势"
        assert "objective_definition" not in seen["body"]
        assert res["data"]["creation_mode"] == "smart"

    def test_rejects_empty_precise_definition(self, monkeypatch):
        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("HTTP must not be called")

        _install(monkeypatch, handler)
        res = srv.create_cvc_model(
            name="x", companies=["a"],
            objective_definition={"mode": "precise", "target_regions": []},
        )

        assert res["ok"] is False
        assert res["error_code"] == "VALIDATION_ERROR"
        assert "至少填写一项" in res["message"]

    def test_rejects_unknown_objective_mode(self, monkeypatch):
        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("HTTP must not be called")

        _install(monkeypatch, handler)
        res = srv.create_cvc_model(
            name="x", companies=["a"],
            objective_definition={"mode": "fuzzy", "target_customers": "电网"},
        )

        assert res["ok"] is False
        assert res["error_code"] == "VALIDATION_ERROR"
        assert "smart" in res["message"] and "precise" in res["message"]


class TestJobPollingTools:
    def test_get_company_search_job(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return _ok({"job_id": "csjob-test", "status": "running", "kind": "company_search"})

        _install(monkeypatch, handler)
        res = srv.get_company_search_job("csjob-test")

        assert res["ok"] is True
        assert seen["path"].endswith("/companies/search-jobs/csjob-test")
        assert res["data"]["status"] == "running"

    def test_get_cvc_creation_job(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return _ok({"job_id": "cvcjob-test", "status": "succeeded",
                        "kind": "cvc_create", "result": {"cvc_id": "cvc_abcd1234"}})

        _install(monkeypatch, handler)
        res = srv.get_cvc_creation_job("cvcjob-test")

        assert res["ok"] is True
        assert seen["path"].endswith("/cvc-models/creation-jobs/cvcjob-test")
        assert res["data"]["result"]["cvc_id"] == "cvc_abcd1234"


class TestGetCvcBuildStatus:
    def test_trims_logs_and_forwards_query_params(self, monkeypatch):
        seen = {}
        logs = [{"seq": i, "message": f"m{i}"} for i in range(30)]

        def handler(request):
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return _ok({
                "status": "building",
                "status_label": "构建中",
                "build_progress": 52,
                "current_phase": 1,
                "phase_name": "竞争情报采集",
                "phases": [{"phase": 1, "status": "running", "progress": 80}],
                "failure": None,
                "build_logs": logs,
                "log_total": 30,
                "log_next_since": 30,
            })

        _install(monkeypatch, handler)
        res = srv.get_cvc_build_status("cvc_abcd1234", wait_seconds=15, log_since=3)

        assert res["ok"] is True
        assert seen["path"].endswith("/cvc_abcd1234/build-status")
        assert seen["params"]["wait_seconds"] == "15"
        assert seen["params"]["log_since"] == "3"
        assert len(res["data"]["build_logs"]) == 20
        assert res["data"]["build_logs"][0]["seq"] == 10
        assert res["data"]["log_next_since"] == 30
        assert res["data"]["phases"][0]["status"] == "running"

    def test_rejects_out_of_range_wait(self, monkeypatch):
        _install(monkeypatch, lambda request: _ok({}))
        res = srv.get_cvc_build_status("cvc_abcd1234", wait_seconds=30)

        assert res["ok"] is False
        assert res["error_code"] == "VALIDATION_ERROR"
