"""Tests for the v1.4 report tools and the OpenAPI report client.

All HTTP traffic is served by ``httpx.MockTransport``; no network is used.
"""
import json

import httpx
import pytest

from casee_mcp_server import server as srv
from casee_mcp_server.reports import (
    ReportClient,
    absolute_url,
    resolve_is_api_key,
    resolve_openapi_base,
    resolve_portal_base,
)

CVC = "cvc_9f8e7d6c1234"


def _ok(data):
    return httpx.Response(200, json={"code": 200, "message": "ok", "data": data})


def _install(monkeypatch, handler, **kwargs):
    client = ReportClient(
        api_key="casee_test",
        base_url="https://api.test",
        transport=httpx.MockTransport(handler),
        max_retries=kwargs.pop("max_retries", 0),
        **kwargs,
    )
    monkeypatch.setattr(srv, "get_report_client", lambda: client)
    return client


# --------------------------------------------------------------- reports.py


class TestBaseResolution:
    def test_is_api_key_prefers_new_name_with_legacy_fallback(self, monkeypatch):
        monkeypatch.setenv("CASEE_IS_API_KEY", "casee_new")
        monkeypatch.setenv("CASEE_API_KEY", "casee_legacy")
        assert resolve_is_api_key() == "casee_new"
        monkeypatch.delenv("CASEE_IS_API_KEY")
        assert resolve_is_api_key() == "casee_legacy"
        monkeypatch.delenv("CASEE_API_KEY")
        assert resolve_is_api_key() == ""

    def test_report_client_defaults_to_is_key(self, monkeypatch):
        monkeypatch.setenv("CASEE_IS_API_KEY", "casee_is")
        assert ReportClient().api_key == "casee_is"

    def test_openapi_base_variants(self, monkeypatch):
        monkeypatch.delenv("CASEE_OPENAPI_BASE_URL", raising=False)
        monkeypatch.setenv("CASEE_API_BASE_URL", "https://www.casee.me")
        assert resolve_openapi_base() == "https://www.casee.me/api/v1/openapi"
        assert resolve_openapi_base("https://x.test/api/v1") == "https://x.test/api/v1/openapi"
        assert resolve_openapi_base("https://x.test/api/v1/openapi/") == "https://x.test/api/v1/openapi"

    def test_portal_base_and_absolute_url(self, monkeypatch):
        monkeypatch.delenv("CASEE_PORTAL_BASE_URL", raising=False)
        monkeypatch.setenv("CASEE_API_BASE_URL", "https://www.casee.me/api/v1/openapi")
        assert resolve_portal_base() == "https://www.casee.me"
        assert absolute_url("/share/report/rs-1") == "https://www.casee.me/share/report/rs-1"
        assert absolute_url("https://cdn.test/a.html") == "https://cdn.test/a.html"


class TestClientRetryAndEnvelope:
    def test_unwraps_data_envelope(self):
        client = ReportClient(
            api_key="k", base_url="https://api.test",
            transport=httpx.MockTransport(lambda req: _ok({"items": [1]})),
            max_retries=0,
        )
        assert client.request("GET", "/reports/jobs") == {"items": [1]}

    def test_retries_rate_limit_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={"code": 42901, "message": "slow down"},
                                      headers={"Retry-After": "0"})
            return _ok({"ok": 1})

        client = ReportClient(
            api_key="k", base_url="https://api.test",
            transport=httpx.MockTransport(handler), max_retries=1,
        )
        assert client.request("GET", "/credits") == {"ok": 1}
        assert calls["n"] == 2

    def test_post_without_idempotency_is_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(503, json={"code": 50301, "message": "busy"})

        client = ReportClient(
            api_key="k", base_url="https://api.test",
            transport=httpx.MockTransport(handler), max_retries=2,
        )
        with pytest.raises(Exception):
            client.request("POST", "/reports/generate", json_body={})
        assert calls["n"] == 1

    def test_post_with_idempotency_key_retries(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, json={"code": 50301, "message": "busy"})
            return _ok({"job_id": "job-1"})

        client = ReportClient(
            api_key="k", base_url="https://api.test",
            transport=httpx.MockTransport(handler), max_retries=1,
        )
        data = client.request(
            "POST", "/reports/generate", json_body={},
            extra_headers={"Idempotency-Key": "abc12345"},
        )
        assert data["job_id"] == "job-1"
        assert calls["n"] == 2


# ------------------------------------------------------------ report tools


class TestTemplates:
    def test_pagination_applied(self, monkeypatch):
        templates = [{"template_id": f"report.t{i}"} for i in range(8)]
        _install(monkeypatch, lambda req: _ok({"templates": templates, "scope": "all"}))
        result = srv.list_report_templates(page=2, page_size=3)
        assert result["ok"] is True
        assert [t["template_id"] for t in result["data"]["items"]] == [
            "report.t3", "report.t4", "report.t5",
        ]
        pagination = result["data"]["pagination"]
        assert pagination == {
            "page": 2, "page_size": 3, "total": 8, "total_pages": 3, "has_more": True,
        }

    def test_invalid_scope_rejected_without_network(self):
        result = srv.list_report_templates(scope="bogus")
        assert result["ok"] is False
        assert result["error_code"] == "VALIDATION_ERROR"


class TestPlan:
    def test_condenses_sections_and_posts_body(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _ok({
                "plan_id": "plan-1",
                "sections": [{
                    "id": "threat", "title_zh": "竞争威胁格局", "render": "chart",
                    "available_chart_count": 1,
                    "charts": [{"ca_code": "ca.x", "name_zh": "威胁矩阵",
                                "available": True, "access": {"bundle": "secret-code"}}],
                }],
                "charts": {"total": 1, "available": 1, "gaps": 0, "gaps_ratio": 0},
                "compatibility": {"compatible": True, "reasons": []},
                "quality": {"charts_total": 1},
                "data_gaps": [],
                "recommended_time_range": "month",
            })

        _install(monkeypatch, handler)
        result = srv.plan_report(CVC, "report.competition_dynamics", "auto")
        assert result["ok"] is True
        assert captured["body"] == {
            "cvc_id": CVC, "template_id": "report.competition_dynamics",
            "time_range": "auto", "language": "zh", "include_unavailable": False,
        }
        chart = result["data"]["sections"][0]["charts"][0]
        assert chart["available"] is True
        assert "access" not in chart
        assert result["data"]["compatibility"]["compatible"] is True

    def test_requires_template(self):
        result = srv.plan_report(CVC, "")
        assert result["ok"] is False
        assert "list_report_templates" in result["message"]


class TestGenerate:
    def test_sends_idempotency_and_formats(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            captured["idem"] = request.headers.get("Idempotency-Key")
            return _ok({"job_id": "job-1", "status": "queued",
                        "time_range": "month", "poll_url": "/reports/jobs/job-1"})

        _install(monkeypatch, handler)
        result = srv.generate_report(
            CVC, "report.cpm", "auto", ["html", "deck"],
            idempotency_key="abc12345xy",
        )
        assert result["ok"] is True
        assert result["meta"]["next_poll_after_ms"] == 5000
        assert result["data"]["job_id"] == "job-1"
        assert captured["idem"] == "abc12345xy"
        assert captured["body"]["output_formats"] == ["html", "deck"]
        assert captured["body"]["share"] is False

    def test_invalid_output_format_rejected(self):
        result = srv.generate_report(CVC, "report.cpm", output_formats=["pdf"])
        assert result["ok"] is False
        assert result["details"]["invalid"] == ["pdf"]

    def test_short_idempotency_key_rejected(self):
        result = srv.generate_report(CVC, "report.cpm", idempotency_key="short")
        assert result["ok"] is False
        assert result["error_code"] == "VALIDATION_ERROR"


class TestJobs:
    def test_job_view_absolutizes_urls(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({
            "job_id": "job-1", "status": "succeeded",
            "artifacts": [{"format": "html", "ready": True,
                           "url": "/api/v1/openapi/reports/jobs/job-1/download?format=html"}],
            "share": {"code": "rs-1", "url": "/share/report/rs-1"},
            "next_poll_after_ms": 0,
        }))
        result = srv.get_report_job("job-1")
        assert result["ok"] is True
        assert result["data"]["artifacts"][0]["url"] == (
            "https://api.test/api/v1/openapi/reports/jobs/job-1/download?format=html"
        )
        assert result["data"]["share"]["url"] == "https://api.test/share/report/rs-1"

    def test_list_validates_status(self):
        result = srv.list_report_jobs(status="done")
        assert result["ok"] is False
        assert "queued" in result["message"]

    def test_list_passes_filters(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return _ok({"items": [], "pagination": {"page": 1, "page_size": 20, "total": 0}})

        _install(monkeypatch, handler)
        result = srv.list_report_jobs(cvc_id=CVC, status="partial", page=2, page_size=10)
        assert result["ok"] is True
        assert captured["params"] == {
            "cvc_id": CVC, "status": "partial", "page": "2", "page_size": "10",
        }


class TestShareBackupRebuild:
    def test_share_absolutizes_urls(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({
            "code": "rs-1", "url": "/share/report/rs-1",
            "api_url": "/api/v1/public/reports/rs-1",
            "pdf_url": "/agent/report/pdf?code=rs-1",
            "expires_at": "2026-11-07T00:00:00Z",
        }))
        result = srv.share_report("job-1")
        assert result["ok"] is True
        assert result["data"]["url"] == "https://api.test/share/report/rs-1"
        assert result["data"]["api_url"] == "https://api.test/api/v1/public/reports/rs-1"
        assert result["data"]["pdf_url"] == "https://api.test/agent/report/pdf?code=rs-1"

    def test_backup_returns_metadata_only(self, monkeypatch):
        _install(monkeypatch, lambda req: httpx.Response(
            200, content=b"{}\n",
            headers={"x-total-count": "24", "etag": '"abc"', "content-length": "100"},
        ))
        result = srv.backup_cvc_intelligence(CVC)
        assert result["ok"] is True
        assert result["data"]["count"] == 24
        assert result["data"]["etag"] == "abc"

    def test_rebuild_defaults_to_dry_run(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _ok({"cvc_id": CVC, "dry_run": True,
                        "will_delete": {"intelligence": 24}, "requires_confirm": True})

        _install(monkeypatch, handler)
        result = srv.rebuild_cvc_model(CVC)
        assert result["ok"] is True
        assert captured["body"] == {
            "confirm": False, "dry_run": True, "backup": True, "mode": "full",
        }
        assert result["data"]["executed"] is False

    def test_rebuild_executes_only_with_confirm(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _ok({"cvc_id": CVC, "status": "building", "task_id": "task-1"})

        _install(monkeypatch, handler)
        result = srv.rebuild_cvc_model(CVC, confirm=True, dry_run=False)
        assert result["ok"] is True
        assert captured["body"]["confirm"] is True
        assert captured["body"]["dry_run"] is False
        assert result["data"]["executed"] is True


# --------------------------------------------------------------- errors


class TestErrorMapping:
    def test_product_denied_keeps_code_and_request_id(self, monkeypatch):
        _install(monkeypatch, lambda req: httpx.Response(403, json={
            "code": 40303,
            "message": "当前租户未开通「市场情报平台」，请开通该产品后重试",
            "details": {"module_id": "market-intel", "module_name": "市场情报平台"},
            "error_code": "PRODUCT_ACCESS_DENIED",
            "request_id": "req_abc",
        }))
        result = srv.plan_report(CVC, "report.market_dynamics")
        assert result["ok"] is False
        assert result["error_code"] == "PRODUCT_ACCESS_DENIED"
        assert result["retryable"] is False
        assert result["request_id"] == "req_abc"
        assert result["meta"]["request_id"] == "req_abc"
        assert result["details"]["module_id"] == "market-intel"

    def test_unauthorized_maps_to_authentication_failed(self, monkeypatch):
        _install(monkeypatch, lambda req: httpx.Response(
            401, json={"detail": "Invalid API key"},
        ))
        result = srv.list_report_templates()
        assert result["ok"] is False
        assert result["error_code"] == "AUTHENTICATION_FAILED"
        assert "CASEE_IS_API_KEY" in result["hint"]

    def test_not_found_maps_to_not_found(self, monkeypatch):
        _install(monkeypatch, lambda req: httpx.Response(404, json={
            "code": 40401, "message": "竞争价值链不存在或不属于当前租户",
            "details": {"cvc_id": CVC}, "error_code": "NOT_FOUND",
        }))
        result = srv.plan_report(CVC, "report.cpm")
        assert result["ok"] is False
        assert result["error_code"] == "NOT_FOUND"
        assert result["retryable"] is False

    def test_server_error_is_retryable(self, monkeypatch):
        _install(
            monkeypatch,
            lambda req: httpx.Response(503, json={"code": 50301, "message": "busy"}),
        )
        result = srv.list_report_jobs()
        assert result["ok"] is False
        assert result["error_code"] == "SERVER_ERROR"
        assert result["retryable"] is True


class TestHealth:
    def test_health_reports_versions(self, monkeypatch):
        def handler(request):
            if request.url.path.endswith("/credits"):
                return _ok({"balance": 10, "costs": {}})
            return httpx.Response(200, json={"openapi": "3.1.0",
                                             "info": {"version": "1.1.0"}})

        _install(monkeypatch, handler)
        result = srv.casee_health()
        assert result["ok"] is True
        assert result["data"]["gateway_version"] == srv.__version__
        assert result["data"]["backend_version"] == "1.1.0"
        assert result["data"]["api_base"] == "https://api.test/api/v1/openapi"
        assert result["data"]["credits"]["balance"] == 10


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
