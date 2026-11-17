"""Simulated-environment tests for the v1.7 OpenAPI coverage tools.

All HTTP traffic is served by ``httpx.MockTransport``; no network is used.
The suite mirrors the CaSee OpenAPI contract:

- billing: /credits, /credits/transactions, /quota
- discovery: /meta/catalog, /meta/endpoints/{id}, /meta/mcp-tools
- charts: catalog / detail / preview / access-code + public bundle
- templates: detail / download / create / update / delete / validate
- report artifacts: /reports/jobs/{id}/download and the 413 fallback
- CVC: model meta / companies / entities / entity / ontology / intelligence /
  export / dashboard / trends / insights
"""
import json
import os

import httpx
import pytest

from casee_mcp_server import openapi_tools as ot
from casee_mcp_server import server as srv
from casee_mcp_server.reports import ReportClient

CVC = "cvc_9f8e7d6c1234"


def _ok(data):
    return httpx.Response(200, json={"code": 200, "message": "ok", "data": data})


def _err(status, code, message, details=None):
    """FastAPI HTTPException(detail={code,message,details}) wire shape."""
    return httpx.Response(status, json={
        "detail": {"code": code, "message": message, "details": details or {}},
    })


def _install(monkeypatch, handler, module=ot, **kwargs):
    client = ReportClient(
        api_key="casee_test",
        base_url="https://api.test",
        transport=httpx.MockTransport(handler),
        max_retries=kwargs.pop("max_retries", 0),
        **kwargs,
    )
    monkeypatch.setattr(module, "get_report_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def _download_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEE_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    return tmp_path


# --------------------------------------------------------------- billing


class TestBilling:
    def test_credits_returns_balance_and_prices(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({
            "balance": 12.5,
            "pending_credits": 1.0,
            "buckets": {"free": {"remaining": 2}, "monthly": {}, "paid": {}},
            "ledger_cursor": 42,
            "unit": "credit",
            "operations": {"report_generate": {"credits": 1, "available": True}},
        }))
        result = ot.casee_get_credits()
        assert result["ok"] is True
        assert result["data"]["balance"] == 12.5
        assert result["data"]["ledger_cursor"] == 42
        assert result["data"]["operations"]["report_generate"]["available"] is True

    def test_credit_ledger_filters_and_cursor(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return _ok({"items": [], "summary": {}, "next_cursor": 7,
                        "older_cursor": None, "has_more": False,
                        "pagination": {"page": None, "total_pages": 0}})

        _install(monkeypatch, handler)
        result = ot.casee_get_credit_ledger(
            operation="report_generate", status="confirmed", job_id="job-1",
            since_id=5, page_size=50,
        )
        assert result["ok"] is True
        assert captured["path"].endswith("/api/v1/openapi/credits/transactions")
        assert captured["params"] == {
            "operation": "report_generate",
            "status": "confirmed",
            "job_id": "job-1",
            "since_id": "5",
            "page_size": "50",
        }

    def test_credit_ledger_page_and_cursor_alias(self, monkeypatch):
        captured = {}

        def handler(request):
            captured.setdefault("calls", []).append(dict(request.url.params))
            return _ok({"items": [], "summary": {}, "pagination": {}})

        _install(monkeypatch, handler)
        page_result = ot.casee_get_credit_ledger(page=4, page_size=100)
        assert page_result["ok"] is True
        assert captured["calls"][0] == {"page": "4", "page_size": "100"}

        cursor_result = ot.casee_get_credit_ledger(cursor="1951")
        assert cursor_result["ok"] is True
        assert captured["calls"][1] == {"before_id": "1951", "page_size": "20"}

        older_result = ot.casee_get_credit_ledger(older_cursor="1951")
        assert older_result["ok"] is True
        assert captured["calls"][2] == {"before_id": "1951", "page_size": "20"}

    def test_credit_ledger_direction_filter(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return _ok({"items": [], "summary": {"refunded_count": 1},
                        "filters": {"direction": "refund"}})

        _install(monkeypatch, handler)
        result = ot.casee_get_credit_ledger(direction="refund")
        assert result["ok"] is True
        assert captured["params"] == {"direction": "refund", "page_size": "20"}
        assert result["data"]["summary"]["refunded_count"] == 1

        assert ot.casee_get_credit_ledger(direction="refunds")["ok"] is False
        assert ot.casee_get_credit_ledger(
            direction="refund", status="confirmed",
        )["ok"] is False
        assert ot.casee_get_credit_ledger(
            direction="charge", status="rolled_back",
        )["ok"] is False

    def test_credit_ledger_rejects_bad_status_and_cursor_conflict(self):
        bad_status = ot.casee_get_credit_ledger(status="done")
        assert bad_status["ok"] is False
        assert bad_status["error_code"] == "VALIDATION_ERROR"

        conflict = ot.casee_get_credit_ledger(since_id=1, before_id=2)
        assert conflict["ok"] is False
        assert "cannot be used together" in conflict["message"]

        bad_size = ot.casee_get_credit_ledger(page_size=501)
        assert bad_size["ok"] is False

        page_and_cursor = ot.casee_get_credit_ledger(page=2, before_id=99)
        assert page_and_cursor["ok"] is False
        assert "cannot be combined" in page_and_cursor["message"]

        bad_cursor = ot.casee_get_credit_ledger(cursor="not-a-tx")
        assert bad_cursor["ok"] is False

        both_aliases = ot.casee_get_credit_ledger(cursor="1951", older_cursor="1951")
        assert both_aliases["ok"] is False

    def test_quota_snapshot(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({
            "key_id": "k1", "scopes": ["cvc:read"],
            "rate_limit": {"per_minute": 60, "daily": 2000,
                           "remaining_minute": 59, "remaining_day": 1999},
            "cvc_models": {"accessible": 3},
        }))
        result = ot.casee_get_quota()
        assert result["ok"] is True
        assert result["data"]["rate_limit"]["remaining_day"] == 1999


# ------------------------------------------------------------- discovery


class TestDiscovery:
    def test_list_endpoints_passes_filters(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return _ok({"groups": [], "endpoints": [{"endpoint_id": "reports.download"}]})

        _install(monkeypatch, handler)
        result = ot.casee_list_endpoints(group="reports", q="download", detail=True)
        assert result["ok"] is True
        assert captured["params"] == {"group": "reports", "q": "download", "detail": "true"}

    def test_get_endpoint_and_validation(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            return _ok({"endpoint_id": "reports.download", "method": "GET"})

        _install(monkeypatch, handler)
        result = ot.casee_get_endpoint("reports.download")
        assert result["ok"] is True
        assert captured["path"].endswith("/api/v1/openapi/meta/endpoints/reports.download")

        rejected = ot.casee_get_endpoint("reports/download")
        assert rejected["ok"] is False

    def test_list_mcp_tools(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({
            "server": {"name": "casee-report", "version": "1.7.1"},
            "tools": [{"name": "casee_download_report"}],
        }))
        result = ot.casee_list_mcp_tools()
        assert result["ok"] is True
        assert result["data"]["server"]["version"] == "1.7.1"


# ------------------------------------------------- report artifact download


class TestDownloadReport:
    def test_saves_html_and_reports_hash(self, monkeypatch, tmp_path):
        html = "<html><body>report</body></html>"
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200, content=html.encode(),
                headers={"content-type": "text/html; charset=utf-8",
                         "content-disposition": 'attachment; filename="r.html"'},
            )

        _install(monkeypatch, handler)
        result = ot.casee_download_report("job-1", format="html")
        assert result["ok"] is True
        data = result["data"]
        assert captured["path"].endswith("/api/v1/openapi/reports/jobs/job-1/download")
        assert captured["params"] == {"format": "html"}
        assert data["bytes"] == len(html.encode())
        assert len(data["sha256"]) == 64
        assert data["api_url"].endswith("/reports/jobs/job-1/download?format=html")
        with open(data["saved_path"], encoding="utf-8") as fh:
            assert fh.read() == html

    def test_markdown_inline_and_truncation(self, monkeypatch):
        md = "# Title\n" + "x" * 50
        _install(
            monkeypatch,
            lambda req: httpx.Response(
                200, content=md.encode(),
                headers={"content-type": "text/markdown; charset=utf-8"},
            ),
        )
        result = ot.casee_download_report("job-2", format="md", inline=True, max_inline_chars=10)
        assert result["ok"] is True
        assert result["data"]["content"] == md[:10]
        assert result["data"]["truncated"] is True
        assert result["data"]["content_chars"] == len(md)

    def test_invalid_format_rejected_without_network(self):
        result = ot.casee_download_report("job-1", format="pdf")
        assert result["ok"] is False
        assert result["error_code"] == "VALIDATION_ERROR"

    def test_not_ready_job_maps_to_retryable_job_state(self, monkeypatch):
        _install(monkeypatch, lambda req: _err(409, 40903, "报告尚未生成完成",
                                               {"status": "running"}))
        result = ot.casee_download_report("job-1")
        assert result["ok"] is False
        assert result["error_code"] == "JOB_NOT_READY"
        assert result["retryable"] is True
        assert result["details"]["status"] == "running"

    def test_share_413_points_to_download_tool(self, monkeypatch):
        _install(
            monkeypatch,
            lambda req: _err(413, 40015, "报告超过分享体积上限，请下载后另行分发",
                             {"job_id": "job-1"}),
            module=srv,
        )
        result = srv.share_report("job-1")
        assert result["ok"] is False
        assert result["error_code"] == "TEMPLATE_TOO_LARGE"
        assert result["retryable"] is False
        assert result["details"]["fallback_tool"] == "casee_download_report"
        assert result["details"]["fallback_args"] == {"job_id": "job-1", "format": "html"}
        assert "casee_download_report" in result["hint"]

    def test_share_fail_then_download_recovers(self, monkeypatch, tmp_path):
        """The documented recovery path: 413 share -> download file."""
        def handler(request):
            if request.url.path.endswith("/share"):
                return _err(413, 40015, "报告超过分享体积上限", {"job_id": "job-1"})
            return httpx.Response(
                200, content=b"<html>ok</html>",
                headers={"content-type": "text/html"},
            )

        _install(monkeypatch, handler, module=srv)
        share = srv.share_report("job-1")
        assert share["error_code"] == "TEMPLATE_TOO_LARGE"

        _install(monkeypatch, handler)
        download = ot.casee_download_report("job-1")
        assert download["ok"] is True
        assert os.path.isfile(download["data"]["saved_path"])

    def test_issue_report_access_code_absolutizes_url(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _ok({"code": "ra-1", "expires_at": "2026-01-01T00:00:00Z",
                        "report_url": "/report/generate?code=ra-1"})

        _install(monkeypatch, handler)
        result = ot.casee_issue_report_access_code(
            CVC, "report.cpm", time_range="month", output_formats=["html", "deck"],
        )
        assert result["ok"] is True
        assert captured["body"]["output_formats"] == ["html", "deck"]
        assert result["data"]["report_url"].startswith("https://api.test/")


# --------------------------------------------------------------- templates


TEMPLATE_MD = "---\ntemplate_id: tenant.acme.cpm\nname: Acme\n---\n# Section\n"


class TestTemplates:
    def test_get_template_detail(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            return _ok({"template_id": "report.cpm", "sections": []})

        _install(monkeypatch, handler)
        result = ot.casee_get_report_template("report.cpm")
        assert result["ok"] is True
        assert captured["path"].endswith("/api/v1/openapi/reports/templates/report.cpm")

    def test_download_template_saves_markdown(self, monkeypatch):
        _install(
            monkeypatch,
            lambda req: httpx.Response(
                200, content=TEMPLATE_MD.encode(),
                headers={"content-type": "text/markdown; charset=utf-8"},
            ),
        )
        result = ot.casee_download_report_template("report.cpm", inline=True)
        assert result["ok"] is True
        assert result["data"]["filename"].endswith(".md")
        assert result["data"]["content"].startswith("---")
        with open(result["data"]["saved_path"], encoding="utf-8") as fh:
            assert fh.read() == TEMPLATE_MD

    def test_create_template_validates_and_posts(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            return _ok({"template": {"template_id": captured["body"]["template_id"]},
                        "validation": {"valid": True, "errors": []}})

        _install(monkeypatch, handler)
        result = ot.casee_create_report_template(
            "tenant.acme.cpm", "Acme CPM", TEMPLATE_MD, summary="s",
        )
        assert result["ok"] is True
        assert captured["method"] == "POST"
        assert captured["body"]["status"] == "active"
        assert captured["body"]["summary"] == "s"

        invalid = ot.casee_create_report_template("tenant.acme.cpm", "", TEMPLATE_MD)
        assert invalid["ok"] is False

    def test_update_template_requires_a_field(self, monkeypatch):
        rejected = ot.casee_update_report_template("tenant.acme.cpm")
        assert rejected["ok"] is False

        captured = {}

        def handler(request):
            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            return _ok({"template": {"template_id": "tenant.acme.cpm"}})

        _install(monkeypatch, handler)
        result = ot.casee_update_report_template(
            "tenant.acme.cpm", content_md=TEMPLATE_MD, status="draft",
        )
        assert result["ok"] is True
        assert captured["method"] == "PUT"
        assert captured["body"] == {"content_md": TEMPLATE_MD, "status": "draft"}

    def test_delete_template_requires_confirm(self, monkeypatch):
        rejected = ot.casee_delete_report_template("tenant.acme.cpm")
        assert rejected["ok"] is False

        captured = {}

        def handler(request):
            captured["method"] = request.method
            return _ok({"template_id": "tenant.acme.cpm", "deleted": True})

        _install(monkeypatch, handler)
        result = ot.casee_delete_report_template("tenant.acme.cpm", confirm=True)
        assert result["ok"] is True
        assert captured["method"] == "DELETE"

    def test_validate_template_returns_errors(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _ok({"valid": False, "errors": [{"code": 40013}]})

        _install(monkeypatch, handler)
        result = ot.casee_validate_report_template("tenant.acme.cpm", TEMPLATE_MD)
        assert result["ok"] is True
        assert captured["body"] == {"content_md": TEMPLATE_MD}
        assert result["data"]["valid"] is False


# ------------------------------------------------------------------ charts


class TestCharts:
    def test_search_charts_passes_filters(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return _ok({"total": 1, "charts": [{"ca_code": "ca.x"}]})

        _install(monkeypatch, handler)
        result = ot.casee_search_charts(
            domain="analysis", cvc_id=CVC, detail=True, with_screenshot=True,
        )
        assert result["ok"] is True
        assert captured["params"] == {
            "domain": "analysis", "cvc_id": CVC, "time_range": "month",
            "detail": "true", "with_screenshot": "true",
        }
        assert ot.casee_search_charts(time_range="year")["ok"] is False

    def test_get_chart_detail_and_validation(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return _ok({"ca_code": "ca.x", "fields": []})

        _install(monkeypatch, handler)
        result = ot.casee_get_chart("ca.market.market_growth", cvc_id=CVC)
        assert result["ok"] is True
        assert captured["path"].endswith("/api/v1/openapi/charts/catalog/ca.market.market_growth")
        assert captured["params"] == {"cvc_id": CVC, "time_range": "month"}

    def test_preview_strips_embed_html_and_saves_it(self, monkeypatch, tmp_path):
        html = "<div>chart</div>"
        _install(monkeypatch, lambda req: _ok({
            "ca_code": "ca.x", "data": {"rows": []},
            "embed": {"html": html, "width": 800},
            "render_params": {"series": []},
        }))
        result = ot.casee_preview_chart("ca.x", save_path=str(tmp_path))
        assert result["ok"] is True
        embed = result["data"]["embed"]
        assert "html" not in embed
        assert embed["html_omitted"] is True
        assert embed["html_chars"] == len(html)
        assert os.path.isfile(embed["html_saved_path"])

    def test_get_chart_bundle_issues_code_then_uses_public_base(self, monkeypatch):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path.endswith("/charts/access-code"):
                return _ok({
                    "code": "cc-abc", "expires_at": "2026-01-01T00:00:00Z",
                    "access": {"bundle": "/api/v1/public/charts/cc-abc",
                               "data_json": "", "data_csv": "", "report_markdown": ""},
                })
            return httpx.Response(200, json={
                "code": "cc-abc", "ca_code": "ca.x",
                "data": {"rows": [1]}, "embed": {"html": "<html/>"},
            })

        _install(monkeypatch, handler)
        result = ot.casee_get_chart_bundle(cvc_id=CVC, chart="ca.x")
        assert result["ok"] is True
        assert ("POST", "/api/v1/openapi/charts/access-code") in calls
        assert ("GET", "/api/v1/public/charts/cc-abc") in calls
        assert result["data"]["access"]["expires_at"] == "2026-01-01T00:00:00Z"
        assert "html" not in result["data"]["embed"]

    def test_get_chart_bundle_with_existing_code_skips_issue(self, monkeypatch):
        calls = []
        _install(
            monkeypatch,
            lambda req: (calls.append(req.method), httpx.Response(
                200, json={"code": "cc-1", "data": {}},
            ))[1],
        )
        result = ot.casee_get_chart_bundle(code="cc-1")
        assert result["ok"] is True
        assert calls == ["GET"]

    def test_get_chart_bundle_requires_cvc_without_code(self):
        result = ot.casee_get_chart_bundle(chart="ca.x")
        assert result["ok"] is False
        assert "cvc_id is required" in result["message"]


# ----------------------------------------------------------- CVC inspection


class TestCvcInspection:
    def test_home_overview(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return _ok({"cvc": {"total": 2}, "credits": {"remaining": 9}})

        _install(monkeypatch, handler)
        result = ot.casee_home_overview(limit=5)
        assert result["ok"] is True
        assert captured["params"] == {"limit": "5"}

    def test_get_cvc_model_passes_options(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return _ok({"model": {"status": "completed"}})

        _install(monkeypatch, handler)
        result = ot.casee_get_cvc_model(CVC, include="all", detail="full", min_confidence=0.7)
        assert result["ok"] is True
        assert captured["path"].endswith(f"/api/v1/openapi/cvc-models/{CVC}")
        assert captured["params"]["detail"] == "full"
        assert captured["params"]["min_confidence"] == "0.7"
        assert ot.casee_get_cvc_model(CVC, detail="bogus")["ok"] is False

    def test_list_cvc_companies(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({
            "cvc_id": CVC, "total": 1, "companies": [{"company_name": "Acme"}],
        }))
        result = ot.casee_list_cvc_companies(CVC)
        assert result["ok"] is True
        assert result["data"]["companies"][0]["company_name"] == "Acme"

    def test_list_entities_and_validation(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({"items": [], "pagination": {}}))
        assert ot.casee_list_cvc_entities(CVC, entity_type="product")["ok"] is True
        assert ot.casee_list_cvc_entities(CVC, entity_type="bogus")["ok"] is False
        assert ot.casee_list_cvc_entities(CVC, page_size=201)["ok"] is False

    def test_get_entity_quotes_path_segments(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            captured["raw_path"] = request.url.raw_path
            return _ok({"entity": {"name": "Acme Corp"}})

        _install(monkeypatch, handler)
        result = ot.casee_get_cvc_entity(
            CVC, "company", "Acme Corp", by="name", relations_depth=2,
        )
        assert result["ok"] is True
        assert b"%20" in captured["raw_path"]
        assert captured["path"].endswith("/entities/company/Acme Corp")
        assert ot.casee_get_cvc_entity(CVC, "all", "x")["ok"] is False

    def test_ontology_schema(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({"classes": [], "object_properties": []}))
        result = ot.casee_get_ontology_schema(CVC)
        assert result["ok"] is True

    def test_export_ontology_requires_scope_and_saves_file(self, monkeypatch):
        rejected = ot.casee_export_ontology(CVC)
        assert rejected["ok"] is False
        assert "requires entity_type+entity_id or keyword" in rejected["message"]

        ttl = "@prefix casee: <https://casee.me/ontology#> .\n"
        _install(
            monkeypatch,
            lambda req: httpx.Response(
                200, content=ttl.encode(),
                headers={"content-type": "text/turtle; charset=utf-8"},
            ),
        )
        result = ot.casee_export_ontology(CVC, entity_type="company", entity_id="e1")
        assert result["ok"] is True
        assert result["data"]["saved_path"].endswith(".ttl")
        assert result["data"]["saved_path"].endswith(f"{CVC}-e1.ttl")
        with open(result["data"]["saved_path"], encoding="utf-8") as fh:
            assert fh.read() == ttl

    def test_search_intelligence_params(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return _ok({"items": [], "pagination": {}})

        _install(monkeypatch, handler)
        result = ot.casee_search_cvc_intelligence(
            CVC, keywords="AI,GPU", match="all", sentiment="negative",
            page=2, page_size=50,
        )
        assert result["ok"] is True
        assert captured["params"]["keywords"] == "AI,GPU"
        assert captured["params"]["match"] == "all"
        assert captured["params"]["sentiment"] == "negative"
        assert "days" not in captured["params"]
        assert "min_score" not in captured["params"]
        assert ot.casee_search_cvc_intelligence(CVC, sentiment="angry")["ok"] is False

    def test_intelligence_detail(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            return _ok({"item": {"intel_id": "i1"}, "related_entities": []})

        _install(monkeypatch, handler)
        result = ot.casee_get_intelligence_detail(CVC, "i1")
        assert result["ok"] is True
        assert captured["path"].endswith(f"/cvc-models/{CVC}/intelligence/i1")

    def test_export_intelligence_saves_jsonl(self, monkeypatch):
        lines = '{"intel_id":"i1"}\n{"intel_id":"i2"}\n'
        _install(
            monkeypatch,
            lambda req: httpx.Response(
                200, content=lines.encode(),
                headers={"content-type": "application/x-ndjson; charset=utf-8",
                         "x-total-count": "2", "etag": '"abc"'},
            ),
        )
        result = ot.casee_export_intelligence(CVC)
        assert result["ok"] is True
        assert result["data"]["total_count"] == 2
        assert result["data"]["etag"] == "abc"
        assert result["data"]["saved_path"].endswith("-intelligence.jsonl")

    def test_dashboard_and_trends(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({"summary": {}}))
        assert ot.casee_get_cvc_dashboard(CVC, "quarter")["ok"] is True
        assert ot.casee_get_cvc_dashboard(CVC, "year")["ok"] is False
        assert ot.casee_get_market_trends(CVC)["ok"] is True

    def test_insights_default_to_dry_run(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            captured["path"] = request.url.path
            return _ok({"dry_run": True, "cache": {"hit": False}})

        _install(monkeypatch, handler)
        result = ot.casee_get_enterprise_insight(CVC, "Acme")
        assert result["ok"] is True
        assert captured["path"].endswith(f"/cvc-models/{CVC}/enterprise-insight")
        assert captured["params"]["dry_run"] == "true"
        assert captured["params"]["company_name"] == "Acme"

        assert ot.casee_get_market_insight(CVC, "EV")["ok"] is True
        assert ot.casee_get_customer_insight(CVC, "Bank")["ok"] is True
        assert ot.casee_get_market_insight(CVC, "EV", max_age_days=0)["ok"] is False


class TestArtifactLimits:
    def test_response_over_limit_is_rejected(self, monkeypatch):
        _install(
            monkeypatch,
            lambda req: httpx.Response(200, content=b"x" * 100),
        )
        client = ot.get_report_client()
        with pytest.raises(Exception) as excinfo:
            client.request_bytes("GET", "/reports/jobs/job-1/download", max_bytes=10)
        assert "exceeds" in str(excinfo.value)


# ------------------------------------------------------- MCP protocol level


class TestProtocolLevel:
    """Verify registration + JSON serialization via tools/call, not just functions."""

    async def test_credits_tool_call_returns_json_text(self, monkeypatch):
        _install(monkeypatch, lambda req: _ok({
            "balance": 3.0, "ledger_cursor": 9, "operations": {},
        }))
        result = await srv.mcp.call_tool("casee_get_credits", {})
        payload = json.loads(result.content[0].text)
        assert result.is_error is False
        assert payload["ok"] is True
        assert payload["data"]["balance"] == 3.0

    async def test_share_413_tool_call_is_structured(self, monkeypatch):
        _install(
            monkeypatch,
            lambda req: _err(413, 40015, "报告超过分享体积上限", {"job_id": "job-1"}),
            module=srv,
        )
        result = await srv.mcp.call_tool("share_report", {"job_id": "job-1"})
        payload = json.loads(result.content[0].text)
        assert payload["ok"] is False
        assert payload["error_code"] == "TEMPLATE_TOO_LARGE"
        assert payload["details"]["fallback_tool"] == "casee_download_report"

