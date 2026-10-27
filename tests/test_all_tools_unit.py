"""Unit tests for all 14 MCP tools + shared helpers.

The casee SDK calls are patched, so no network access happens here.
Coverage focuses on parameter shaping, validation and error conversion
for every tool exposed by the MCP server.
"""
import pytest
from unittest.mock import patch

from casee.exceptions import CaseeError, AuthenticationError

from casee_mcp_server.server import (
    find_trusted_sources,
    search_intelligence,
    analyze_trend,
    aggregate_by_source,
    semantic_search_tool,
    search_with_cvc,
    batch_search,
    list_cvc_models_tool,
    cvc_stats_tool,
    cvc_cache_purge_tool,
    cvc_cleanup_status_tool,
    cvc_reindex_tool,
    intelligence_purge_tool,
    intelligence_purge_status_tool,
    _safe_call,
    _sdk_call,
    _diagnose_sdk_error,
    _validate_cvc_model_id,
)

VALID_CVC_ID = "cvc_9f8e7d6c"


# --------------------------------------------------------------- helpers


class TestSafeCall:
    """Test _safe_call / _sdk_call error conversion."""

    def test_success_returns_raw_result(self):
        result = _safe_call(lambda: {"ok": 1})
        assert result == {"ok": 1}

    def test_casee_error_to_dict_with_hint(self):
        def boom():
            raise CaseeError("bad request")

        result = _safe_call(boom)
        assert result["error"] == "CaseeError"
        assert "bad request" in result["message"]
        assert "hint" in result

    def test_authentication_error_hint(self):
        def boom():
            raise AuthenticationError("invalid key")

        result = _safe_call(boom)
        assert result["error"] == "AuthenticationError"
        assert "CASEE_API_KEY" in result["hint"]

    def test_unexpected_error_wrapped(self):
        def boom():
            raise ValueError("boom")

        result = _safe_call(boom)
        assert result["error"] == "UnexpectedError"
        assert "ValueError" in result["message"]

    def test_sdk_call_forwards_kwargs(self):
        result = _sdk_call(lambda a, b: a + b, a=1, b=2)
        assert result == 3


class TestDiagnoseSdkError:
    """Test _diagnose_sdk_error hint generation."""

    def test_html_landing_page_hint(self):
        exc = CaseeError(
            'Expected JSON, got HTML from CASEE_API_BASE_URL (landing page)'
        )
        hint = _diagnose_sdk_error(exc)
        assert "CASEE_API_BASE_URL" in hint
        assert "FastAPI" in hint

    def test_timeout_hint(self):
        hint = _diagnose_sdk_error(CaseeError("Request timed out after 30s"))
        assert "timed out" in hint.lower() or "timeout" in hint.lower()
        assert "CASEE_TIMEOUT" in hint

    def test_empty_hint_for_generic_error(self):
        assert _diagnose_sdk_error(CaseeError("some other failure")) == ""


class TestValidateCvcModelId:
    """Test CVC model id validation."""

    @pytest.mark.parametrize("cvc_id", [
        "cvc_9f8e7d6c",
        "cvc_abcdefgh",
        "cvc_" + "a1" * 16,   # 32 chars payload
    ])
    def test_valid_ids(self, cvc_id):
        assert _validate_cvc_model_id(cvc_id) == ""

    @pytest.mark.parametrize("cvc_id", [
        "",
        "invalid_id",
        "cvc_SHORT",
        "CVC_9f8e7d6c",        # uppercase prefix
        "cvc_9F8E7D6C",        # uppercase payload
        "cvc_123",             # too short
        "cvc_" + "a" * 40,     # too long
        " cvc_9f8e7d6c",       # leading space
    ])
    def test_invalid_ids(self, cvc_id):
        err = _validate_cvc_model_id(cvc_id)
        assert err
        assert "cvc_model_id must match" in err


# --------------------------------------------------------------- tool 1


class TestFindTrustedSources:
    """Test find_trusted_sources parameter shaping."""

    @patch("casee_mcp_server.server.search_sources")
    def test_defaults(self, mock_sources):
        mock_sources.return_value = {"sources": [], "total": 0}
        result = find_trusted_sources()
        mock_sources.assert_called_once_with(
            keyword=None, category=None, region=None, language=None,
            min_tscore=0.1, sample_size=2, limit=20,
        )
        assert result == {"sources": [], "total": 0}

    @patch("casee_mcp_server.server.search_sources")
    def test_empty_strings_become_none(self, mock_sources):
        find_trusted_sources(keyword="", category="", region="", language="")
        kwargs = mock_sources.call_args[1]
        assert kwargs["keyword"] is None
        assert kwargs["category"] is None
        assert kwargs["region"] is None
        assert kwargs["language"] is None

    @patch("casee_mcp_server.server.search_sources")
    def test_custom_filters(self, mock_sources):
        find_trusted_sources(
            keyword="tesla", category="wire", region="us",
            language="en", min_tscore=0.8, sample_size=5, limit=50,
        )
        mock_sources.assert_called_once_with(
            keyword="tesla", category="wire", region="us", language="en",
            min_tscore=0.8, sample_size=5, limit=50,
        )

    @patch("casee_mcp_server.server.search_sources")
    def test_sdk_error_returns_structured_dict(self, mock_sources):
        mock_sources.side_effect = CaseeError("upstream down")
        result = find_trusted_sources()
        assert result["error"] == "CaseeError"


# --------------------------------------------------------------- tool 2


class TestSearchIntelligence:
    """Test search_intelligence parameter shaping."""

    @patch("casee_mcp_server.server.search_advanced")
    def test_basic_query(self, mock_search):
        mock_search.return_value = {"count": 1, "items": []}
        result = search_intelligence(q="+Tesla +(EV|battery)")
        kwargs = mock_search.call_args[1]
        assert kwargs["q"] == "+Tesla +(EV|battery)"
        assert kwargs["must_keywords"] is None
        assert kwargs["days"] == 30
        assert result["count"] == 1

    @patch("casee_mcp_server.server.search_advanced")
    def test_keyword_lists_split_on_comma(self, mock_search):
        search_intelligence(
            q="EV",
            must_keywords="AI,chip",
            should_keywords="Nvidia, AMD",
            not_keywords="rumor",
        )
        kwargs = mock_search.call_args[1]
        assert kwargs["must_keywords"] == ["AI", "chip"]
        assert kwargs["should_keywords"] == ["Nvidia", " AMD"]
        assert kwargs["not_keywords"] == ["rumor"]

    @patch("casee_mcp_server.server.search_advanced")
    def test_dates_and_filters(self, mock_search):
        search_intelligence(
            q="EV",
            source_ids=["reuters"],
            start_date="2026-01-01",
            end_date="2026-02-01",
            category="tech",
            region="us",
            language="en",
            min_tscore=0.6,
            limit=5,
        )
        kwargs = mock_search.call_args[1]
        assert kwargs["source_ids"] == ["reuters"]
        assert kwargs["start_date"] == "2026-01-01"
        assert kwargs["end_date"] == "2026-02-01"
        assert kwargs["category"] == "tech"
        assert kwargs["region"] == "us"
        assert kwargs["language"] == "en"
        assert kwargs["min_tscore"] == 0.6
        assert kwargs["limit"] == 5

    @patch("casee_mcp_server.server.search_advanced")
    def test_sdk_error_propagates_as_dict(self, mock_search):
        mock_search.side_effect = CaseeError("bad query")
        result = search_intelligence(q="x")
        assert result["error"] == "CaseeError"


# ----------------------------------------------------------- tools 3 & 4


class TestAnalyzeTrend:
    """Test analyze_trend aggregation logic."""

    @patch("casee_mcp_server.server.search_advanced")
    def test_daily_buckets_and_summary(self, mock_search):
        mock_search.return_value = {"organic_results": [
            {"title": "Tesla Q3 earnings beat expectations",
             "source_id": "reuters", "published_at": "2026-10-01T08:00:00Z"},
            {"title": "Tesla Shanghai output record high",
             "source_id": "bloomberg", "published_at": "2026-10-01T12:00:00Z"},
            {"title": "BYD overseas expansion accelerates",
             "source_id": "reuters", "published_at": "2026-10-02T09:00:00Z"},
        ]}
        result = analyze_trend(q="+Tesla", days=30)
        assert result["query"] == "+Tesla"
        assert result["total_items"] == 3
        assert result["date_range"] == "2026-10-01 ~ 2026-10-02"

        trend = result["trend"]
        assert [d["date"] for d in trend] == ["2026-10-01", "2026-10-02"]
        assert trend[0]["count"] == 2
        assert trend[1]["count"] == 1
        # top keywords: words > 3 chars, lowercased
        assert "tesla" in trend[0]["top_keywords"]
        # top sources per day
        assert set(trend[0]["top_sources"]) == {"reuters", "bloomberg"}

        summary = result["summary"]
        assert summary["peak_date"]["date"] == "2026-10-01"
        assert summary["avg_daily"] == 1.5
        assert summary["total_sources"] == 2

    @patch("casee_mcp_server.server.search_advanced")
    def test_items_without_date_are_skipped(self, mock_search):
        mock_search.return_value = {"organic_results": [
            {"title": "No date item", "source_id": "reuters"},
        ]}
        result = analyze_trend(q="x")
        assert result["trend"] == []
        assert result["date_range"] == "N/A"
        assert result["summary"]["peak_date"] is None

    @patch("casee_mcp_server.server.search_advanced")
    def test_sdk_error_passthrough(self, mock_search):
        mock_search.return_value = {"error": "CaseeError", "message": "x"}
        result = analyze_trend(q="x")
        assert result == {"error": "CaseeError", "message": "x"}

    @patch("casee_mcp_server.server.search_advanced")
    def test_days_and_limit_forwarded(self, mock_search):
        analyze_trend(q="EV", days=90, limit=500, min_tscore=0.5,
                      source_ids=["a"])
        kwargs = mock_search.call_args[1]
        assert kwargs["days"] == 90
        assert kwargs["limit"] == 500
        assert kwargs["min_tscore"] == 0.5
        assert kwargs["source_ids"] == ["a"]


class TestAggregateBySource:
    """Test aggregate_by_source aggregation logic."""

    @patch("casee_mcp_server.server.search_advanced")
    def test_grouping_and_sorting(self, mock_search):
        mock_search.return_value = {"organic_results": [
            {"title": "A" * 100, "source_id": "reuters",
             "source": {"name": "Reuters", "tscore": 0.9}, "tscore": 0.8},
            {"title": "B", "source_id": "reuters",
             "source": {"name": "Reuters", "tscore": 0.9}, "tscore": 0.6},
            {"title": "C", "source_id": "bloomberg",
             "source": {"name": "Bloomberg", "tscore": 0.85}, "tscore": 0.7},
        ]}
        result = aggregate_by_source(q="+EV", days=30)
        assert result["total_items"] == 3
        assert result["total_sources"] == 2

        sources = result["sources"]
        # sorted by count desc: reuters(2) first
        assert sources[0]["source_id"] == "reuters"
        assert sources[0]["count"] == 2
        assert sources[0]["name"] == "Reuters"
        assert sources[0]["avg_tscore"] == 0.7  # (0.8+0.6)/2
        # sample titles capped at 3, truncated to 80 chars
        assert len(sources[0]["sample_titles"]) == 2
        assert len(sources[0]["sample_titles"][0]) == 80

    @patch("casee_mcp_server.server.search_advanced")
    def test_unknown_source_fallback(self, mock_search):
        mock_search.return_value = {"organic_results": [
            {"title": "orphan", "tscore": None},
        ]}
        result = aggregate_by_source(q="x")
        assert result["sources"][0]["source_id"] == "unknown"
        assert result["sources"][0]["avg_tscore"] == 0

    @patch("casee_mcp_server.server.search_advanced")
    def test_sdk_error_passthrough(self, mock_search):
        mock_search.return_value = {"error": "CaseeError", "message": "x"}
        assert aggregate_by_source(q="x")["error"] == "CaseeError"


# --------------------------------------------------------------- tool 5


class TestSemanticSearchToolValidation:
    """Test semantic_search_tool filter shaping beyond existing coverage."""

    @patch("casee_mcp_server.server.semantic_search")
    def test_all_filters_omitted_when_empty(self, mock_sem):
        mock_sem.return_value = {"organic_results": []}
        semantic_search_tool(cvc_model_id=VALID_CVC_ID, q="test")
        kwargs = mock_sem.call_args[1]
        for absent in ("time_range", "days", "category", "region", "language"):
            assert absent not in kwargs
        # default min_tscore=0.1 is > 0 and therefore forwarded
        assert kwargs["min_tscore"] == 0.1
        assert kwargs["mode"] == "hybrid"
        assert kwargs["top_k"] == 20

    @patch("casee_mcp_server.server.semantic_search")
    def test_zero_min_tscore_not_sent(self, mock_sem):
        semantic_search_tool(cvc_model_id=VALID_CVC_ID, q="t", min_tscore=0)
        assert "min_tscore" not in mock_sem.call_args[1]

    @patch("casee_mcp_server.server.semantic_search")
    def test_sdk_error_passthrough(self, mock_sem):
        mock_sem.side_effect = CaseeError("no such model")
        result = semantic_search_tool(cvc_model_id=VALID_CVC_ID, q="t")
        assert result["error"] == "CaseeError"


# --------------------------------------------------------------- tool 7


class TestBatchSearchBoundaries:
    """Extra boundary tests for batch_search."""

    def test_empty_list_rejected(self):
        assert batch_search(groups=[])["error"] == "EmptyGroupList"

    @pytest.mark.parametrize("bad", [0, 9, -1])
    def test_concurrency_out_of_range(self, bad):
        result = batch_search(groups=[{"id": "a"}], max_concurrency=bad)
        assert result["error"] == "InvalidMaxConcurrency"

    @pytest.mark.parametrize("good", [1, 8])
    def test_concurrency_boundaries_accepted(self, good):
        with patch("casee_mcp_server.server.sdk_search_batch") as mock_b:
            mock_b.return_value = {"meta": {}, "results": []}
            batch_search(groups=[{"id": "a"}], max_concurrency=good)
            assert mock_b.call_args[1]["max_concurrency"] == good


# ----------------------------------------------------------- tools 8-14


class TestCvcAndPurgeToolsSmoke:
    """Cover tools 8-14 argument pass-through (error paths already covered)."""

    @patch("casee_mcp_server.server.sdk_list_cvc_models")
    def test_list_models_error_path(self, mock_list):
        mock_list.side_effect = CaseeError("db down")
        result = list_cvc_models_tool()
        assert result["error"] == "CaseeError"
        assert "hint" in result

    @patch("casee_mcp_server.server.sdk_cvc_stats")
    def test_stats_invalid_id_short_circuits(self, mock_stats):
        result = cvc_stats_tool("bogus")
        mock_stats.assert_not_called()
        assert result["error"] == "InvalidCvcModelId"

    @patch("casee_mcp_server.server.sdk_cvc_cache_purge")
    def test_cache_purge_invalid_id_short_circuits(self, mock_purge):
        result = cvc_cache_purge_tool("bogus")
        mock_purge.assert_not_called()
        assert result["error"] == "InvalidCvcModelId"

    @patch("casee_mcp_server.server.sdk_cvc_cleanup_status")
    def test_cleanup_status_ok(self, mock_status):
        mock_status.return_value = {"status": "completed", "residual": {}}
        assert cvc_cleanup_status_tool(VALID_CVC_ID)["status"] == "completed"

    @patch("casee_mcp_server.server.sdk_cvc_reindex")
    def test_reindex_error_path(self, mock_reindex):
        mock_reindex.side_effect = CaseeError("opensearch unreachable")
        result = cvc_reindex_tool(VALID_CVC_ID)
        assert result["error"] == "CaseeError"

    @patch("casee_mcp_server.server.sdk_intelligence_purge")
    def test_purge_defaults_forwarded(self, mock_purge):
        mock_purge.return_value = {"task_id": "t1", "state": "pending"}
        result = intelligence_purge_tool(older_than_days=90)
        kwargs = mock_purge.call_args[1]
        assert kwargs == {
            "older_than_days": 90, "end_date": None, "cvc_model_ids": None,
            "include_mongodb": True, "dry_run": False,
        }
        assert result["task_id"] == "t1"

    @patch("casee_mcp_server.server.sdk_intelligence_purge")
    def test_purge_missing_cutoff_rejected(self, mock_purge):
        mock_purge.assert_not_called()
        result = intelligence_purge_tool()
        assert result["error"] == "MissingCutoff"

    @patch("casee_mcp_server.server.sdk_intelligence_purge_status")
    def test_purge_status_error_path(self, mock_status):
        mock_status.side_effect = CaseeError("task store down")
        result = intelligence_purge_status_tool(task_id="t1")
        assert result["error"] == "CaseeError"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
