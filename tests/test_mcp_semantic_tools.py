"""Integration tests for MCP semantic search tools.

Tests cover:
1. semantic_search_tool function validation and execution
2. search_with_cvc function validation and execution
3. Error handling for invalid parameters
4. Three-stage intelligent retrieval workflow
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add the server module to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from casee_mcp_server.server import (
    semantic_search_tool,
    search_with_cvc,
    find_trusted_sources,
)


class TestSemanticSearchTool:
    """Test semantic_search_tool MCP tool."""

    def test_semantic_search_tool_invalid_cvc_id(self):
        """Test that invalid cvc_model_id returns error dict."""
        result = semantic_search_tool(
            cvc_model_id="invalid_id",
            q="test query",
        )
        assert "error" in result
        assert "cvc_model_id" in result.get("message", "")

    def test_semantic_search_tool_invalid_mode(self):
        """Test that invalid mode returns error dict."""
        result = semantic_search_tool(
            cvc_model_id="cvc_9f8e7d6c",
            q="test query",
            mode="invalid_mode",
        )
        assert "error" in result
        assert "mode" in result.get("message", "")

    @patch("casee_mcp_server.server.semantic_search")
    def test_semantic_search_tool_valid_hybrid(self, mock_semantic_search):
        """Test semantic_search_tool with valid hybrid mode."""
        mock_semantic_search.return_value = {
            "search_metadata": {"id": "test-123", "status": "ok"},
            "search_information": {
                "fusion": {
                    "bm25_hits": 15,
                    "vector_hits": 20,
                    "degraded": [],
                }
            },
            "organic_results": [
                {"title": "Test Result", "tscore": 0.8, "source_id": "reuters"}
            ],
        }

        result = semantic_search_tool(
            cvc_model_id="cvc_9f8e7d6c",
            q="竞争对手在东南亚的电动化布局战略",
            mode="hybrid",
            top_k=20,
        )

        assert "search_metadata" in result
        assert "organic_results" in result
        assert len(result["organic_results"]) == 1

        # Verify correct parameters passed to semantic_search
        mock_semantic_search.assert_called_once()
        call_kwargs = mock_semantic_search.call_args[1]
        assert call_kwargs["cvc_model_id"] == "cvc_9f8e7d6c"
        assert call_kwargs["q"] == "竞争对手在东南亚的电动化布局战略"
        assert call_kwargs["mode"] == "hybrid"
        assert call_kwargs["top_k"] == 20

    @patch("casee_mcp_server.server.semantic_search")
    def test_semantic_search_tool_with_filters(self, mock_semantic_search):
        """Test semantic_search_tool with additional filters."""
        mock_semantic_search.return_value = {
            "search_metadata": {"id": "test"},
            "organic_results": [],
        }

        result = semantic_search_tool(
            cvc_model_id="cvc_12345678",
            q="AI市场趋势",
            mode="semantic",
            time_range="3m",
            days=90,
            category="market",
            region="asia",
            language="zh",
            min_tscore=0.6,
        )

        assert "search_metadata" in result

        # Verify filters were passed
        call_kwargs = mock_semantic_search.call_args[1]
        assert call_kwargs["time_range"] == "3m"
        assert call_kwargs["days"] == 90
        assert call_kwargs["category"] == "market"
        assert call_kwargs["region"] == "asia"
        assert call_kwargs["language"] == "zh"
        assert call_kwargs["min_tscore"] == 0.6


class TestSearchWithCvc:
    """Test search_with_cvc MCP tool."""

    def test_search_with_cvc_invalid_format(self):
        """Test that invalid cvc_model_id format returns error."""
        result = search_with_cvc(
            q="+AI +chip",
            cvc_model_id="invalid_format",
        )
        assert "error" in result

    @patch("casee_mcp_server.server.search_advanced")
    def test_search_with_cvc_with_model_id(self, mock_search_advanced):
        """Test search_with_cvc when cvc_model_id is provided."""
        mock_search_advanced.return_value = {
            "count": 10,
            "items": [
                {"title": f"AI Chip News {i}", "source_id": "reuters", "tscore": 0.8}
                for i in range(10)
            ],
        }

        result = search_with_cvc(
            q="+AI +chip",
            cvc_model_id="cvc_9f8e7d6c",
            min_tscore=0.5,
            days=30,
        )

        # Verify CVC sync status is included
        assert "cvc_sync_status" in result
        assert result["cvc_sync_status"]["cvc_model_id"] == "cvc_9f8e7d6c"
        assert result["cvc_sync_status"]["items_found"] == 10
        assert "semantic_search_tool" in result["cvc_sync_status"]["message"]

        # Verify cvc_model_id was passed to search_advanced
        call_kwargs = mock_search_advanced.call_args[1]
        assert call_kwargs["cvc_model_id"] == "cvc_9f8e7d6c"

    @patch("casee_mcp_server.server.search_advanced")
    def test_search_with_cvc_without_model_id(self, mock_search_advanced):
        """Test search_with_cvc when cvc_model_id is empty (behaves like search_intelligence)."""
        mock_search_advanced.return_value = {
            "count": 5,
            "items": [],
        }

        result = search_with_cvc(
            q="+AI +chip",
            cvc_model_id="",  # Empty, no CVC sync
            min_tscore=0.5,
        )

        # Verify no CVC sync status
        assert "cvc_sync_status" not in result

        # Verify cvc_model_id was NOT passed to search_advanced
        call_kwargs = mock_search_advanced.call_args[1]
        assert "cvc_model_id" not in call_kwargs or call_kwargs.get("cvc_model_id") == ""

    @patch("casee_mcp_server.server.search_advanced")
    def test_search_with_cvc_with_source_ids(self, mock_search_advanced):
        """Test search_with_cvc with source_ids filtering."""
        mock_search_advanced.return_value = {
            "count": 3,
            "items": [],
        }

        result = search_with_cvc(
            q="+EV +battery",
            cvc_model_id="cvc_abcdef12",
            source_ids=["reuters", "bloomberg"],
            min_tscore=0.6,
        )

        # Verify source_ids were passed
        call_kwargs = mock_search_advanced.call_args[1]
        assert call_kwargs["source_ids"] == ["reuters", "bloomberg"]


class TestThreeStageWorkflow:
    """Test the three-stage intelligent retrieval workflow."""

    @patch("casee_mcp_server.server.search_sources")
    @patch("casee_mcp_server.server.search_advanced")
    @patch("casee_mcp_server.server.semantic_search")
    def test_three_stage_workflow(self, mock_semantic_search, mock_search_advanced, mock_search_sources):
        """Complete three-stage workflow: sources -> search_with_cvc -> semantic_search."""
        # Stage 1: find trusted sources
        mock_search_sources.return_value = {
            "sources": [
                {"source_id": "reuters", "tscore": 0.81},
                {"source_id": "bloomberg", "tscore": 0.78},
            ]
        }

        stage1_result = find_trusted_sources(
            category="wire",
            min_tscore=0.7,
        )
        source_ids = [s["source_id"] for s in stage1_result["sources"]]
        assert len(source_ids) == 2

        # Stage 2: search with CVC sync
        mock_search_advanced.return_value = {
            "count": 15,
            "items": [{"title": f"News {i}"} for i in range(15)],
        }

        stage2_result = search_with_cvc(
            q="+(Tesla|BYD) +(EV|battery)",
            cvc_model_id="cvc_9f8e7d6c",
            source_ids=source_ids,
            min_tscore=0.6,
            days=30,
        )

        assert "cvc_sync_status" in stage2_result
        assert stage2_result["cvc_sync_status"]["items_found"] == 15

        # Stage 3: semantic search on CVC model
        mock_semantic_search.return_value = {
            "search_metadata": {"id": "semantic-test"},
            "search_information": {
                "fusion": {
                    "bm25_hits": 15,
                    "vector_hits": 15,
                    "degraded": [],
                    "rrf_score": 0.92,
                }
            },
            "organic_results": [
                {"title": "Semantic Result 1", "relevance_score": 0.95},
                {"title": "Semantic Result 2", "relevance_score": 0.90},
            ],
        }

        stage3_result = semantic_search_tool(
            cvc_model_id="cvc_9f8e7d6c",
            q="分析特斯拉和比亚迪在电动车电池技术上的竞争态势",
            mode="hybrid",
            top_k=10,
        )

        assert stage3_result["search_information"]["fusion"]["vector_hits"] == 15
        assert len(stage3_result["organic_results"]) == 2

        # Verify all stages executed in sequence
        mock_search_sources.assert_called_once()
        mock_search_advanced.assert_called_once()
        mock_semantic_search.assert_called_once()


class TestEdgeCases:
    """Test edge cases for MCP tools."""

    @patch("casee_mcp_server.server.semantic_search")
    def test_semantic_search_tool_zero_top_k(self, mock_semantic_search):
        """Test semantic_search_tool with top_k=0 (use default)."""
        mock_semantic_search.return_value = {
            "organic_results": [],
        }

        result = semantic_search_tool(
            cvc_model_id="cvc_9f8e7d6c",
            q="test",
            top_k=0,
        )

        # top_k=0 should not be passed (defaults to 20 in SDK)
        call_kwargs = mock_semantic_search.call_args[1]
        # SDK will default to 20 if top_k is not passed or is 0
        assert "top_k" not in call_kwargs or call_kwargs["top_k"] == 0

    @patch("casee_mcp_server.server.semantic_search")
    def test_semantic_search_tool_empty_time_range(self, mock_semantic_search):
        """Test semantic_search_tool with empty time_range."""
        mock_semantic_search.return_value = {
            "organic_results": [],
        }

        result = semantic_search_tool(
            cvc_model_id="cvc_9f8e7d6c",
            q="test",
            time_range="",  # Empty, should not be passed
        )

        call_kwargs = mock_semantic_search.call_args[1]
        assert "time_range" not in call_kwargs

    @patch("casee_mcp_server.server.search_advanced")
    def test_search_with_cvc_keywords_splitting(self, mock_search_advanced):
        """Test that comma-separated keywords are properly split."""
        mock_search_advanced.return_value = {"items": []}

        search_with_cvc(
            q="test",
            must_keywords="AI,chip,semiconductor",
            should_keywords="Nvidia,AMD,Intel",
            not_keywords="rumor,speculation",
        )

        call_kwargs = mock_search_advanced.call_args[1]
        assert call_kwargs["must_keywords"] == ["AI", "chip", "semiconductor"]
        assert call_kwargs["should_keywords"] == ["Nvidia", "AMD", "Intel"]
        assert call_kwargs["not_keywords"] == ["rumor", "speculation"]
