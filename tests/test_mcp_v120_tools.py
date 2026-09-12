"""Unit tests for the v1.2 MCP tools (server-side batch search, CVC ops, full purge).

The SDK calls are patched so the tool logic (parameter shaping, validation and
error handling) is exercised without real network access.
"""
import sys
import os
from unittest.mock import patch

import pytest

# Add the server module to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from casee_mcp_server.server import (
    batch_search,
    list_cvc_models_tool,
    cvc_stats_tool,
    cvc_cache_purge_tool,
    cvc_cleanup_status_tool,
    cvc_reindex_tool,
    intelligence_purge_tool,
    intelligence_purge_status_tool,
)


class TestBatchSearch:
    """Test batch_search MCP tool."""

    def test_empty_groups(self):
        result = batch_search(groups=None)
        assert result["error"] == "EmptyGroupList"

    def test_invalid_max_concurrency(self):
        result = batch_search(groups=[{"id": "a"}], max_concurrency=99)
        assert result["error"] == "InvalidMaxConcurrency"

    @patch("casee_mcp_server.server.sdk_search_batch")
    def test_valid_call(self, mock_search_batch):
        mock_search_batch.return_value = {
            "meta": {"groups": 2, "returned_total": 15, "dedupe": True},
            "results": [
                {"id": "a", "count": 10, "data": []},
                {"id": "b", "count": 5, "data": []},
            ],
        }
        result = batch_search(
            groups=[{"id": "a", "must_keywords": ["AI"]}, {"id": "b", "q": "+EV"}],
            dedupe=True,
            max_concurrency=4,
        )
        assert result["meta"]["groups"] == 2
        args, kwargs = mock_search_batch.call_args
        assert args[0] == [{"id": "a", "must_keywords": ["AI"]}, {"id": "b", "q": "+EV"}]
        assert kwargs["dedupe"] is True
        assert kwargs["max_concurrency"] == 4


class TestListCvcModelsTool:
    """Test list_cvc_models_tool MCP tool."""

    @patch("casee_mcp_server.server.sdk_list_cvc_models")
    def test_valid(self, mock_list):
        mock_list.return_value = {"count": 1, "models": [{"cvc_model_id": "cvc_12ab"}]}
        result = list_cvc_models_tool()
        assert result["count"] == 1


class TestCvcOpsTools:
    """Test the CVC operations tools (stats / cache purge / cleanup / reindex)."""

    def test_invalid_cvc_id(self):
        for tool in (cvc_stats_tool, cvc_cache_purge_tool,
                     cvc_cleanup_status_tool, cvc_reindex_tool):
            result = tool("bad_id")
            assert result["error"] == "InvalidCvcModelId"

    @patch("casee_mcp_server.server.sdk_cvc_stats")
    def test_stats(self, mock_stats):
        mock_stats.return_value = {"doc_count": 10}
        result = cvc_stats_tool("cvc_9f8e7d6c")
        mock_stats.assert_called_once_with("cvc_9f8e7d6c")
        assert result["doc_count"] == 10

    @patch("casee_mcp_server.server.sdk_cvc_cache_purge")
    def test_cache_purge(self, mock_purge):
        mock_purge.return_value = {"status": "queued", "task_id": "t1"}
        result = cvc_cache_purge_tool("cvc_9f8e7d6c")
        mock_purge.assert_called_once_with("cvc_9f8e7d6c")
        assert result["task_id"] == "t1"

    @patch("casee_mcp_server.server.sdk_cvc_cleanup_status")
    def test_cleanup_status(self, mock_status):
        mock_status.return_value = {"status": "completed"}
        result = cvc_cleanup_status_tool("cvc_9f8e7d6c")
        mock_status.assert_called_once_with("cvc_9f8e7d6c")
        assert result["status"] == "completed"

    @patch("casee_mcp_server.server.sdk_cvc_reindex")
    def test_reindex(self, mock_reindex):
        mock_reindex.return_value = {"indexed": 8}
        result = cvc_reindex_tool("cvc_9f8e7d6c")
        mock_reindex.assert_called_once_with("cvc_9f8e7d6c")
        assert result["indexed"] == 8


class TestIntelligencePurgeTool:
    """Test the full-history purge tools."""

    def test_missing_cutoff(self):
        result = intelligence_purge_tool(older_than_days=None, end_date="")
        assert result["error"] == "MissingCutoff"

    @patch("casee_mcp_server.server.sdk_intelligence_purge")
    def test_dry_run(self, mock_purge):
        result = intelligence_purge_tool(older_than_days=90, dry_run=True,
                                         include_mongodb=True)
        kwargs = mock_purge.call_args[1]
        assert kwargs["older_than_days"] == 90
        assert kwargs["end_date"] is None
        assert kwargs["dry_run"] is True
        assert kwargs["include_mongodb"] is True

    @patch("casee_mcp_server.server.sdk_intelligence_purge")
    def test_end_date(self, mock_purge):
        intelligence_purge_tool(end_date="2026-08-31", cvc_model_ids=["cvc_9f8e7d6c"])
        kwargs = mock_purge.call_args[1]
        assert kwargs["older_than_days"] is None
        assert kwargs["end_date"] == "2026-08-31"
        assert kwargs["cvc_model_ids"] == ["cvc_9f8e7d6c"]

    @patch("casee_mcp_server.server.sdk_intelligence_purge_status")
    def test_status(self, mock_status):
        mock_status.return_value = {"state": "completed"}
        result = intelligence_purge_status_tool(task_id="t99")
        mock_status.assert_called_once_with("t99")
        assert result["state"] == "completed"

    @patch("casee_mcp_server.server.sdk_intelligence_purge_status")
    def test_status_default_task(self, mock_status):
        mock_status.return_value = {"state": "pending"}
        intelligence_purge_status_tool(task_id="")
        mock_status.assert_called_once_with(None)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])