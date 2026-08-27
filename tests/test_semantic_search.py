"""Unit tests for semantic search SDK functionality.

Tests cover:
1. SemanticSearch class initialization and validation
2. semantic_search function execution
3. cvc_model_id format validation
4. mode parameter validation
5. Search payload construction
"""
import pytest
from unittest.mock import patch, MagicMock
from casee import semantic_search, SemanticSearch
from casee.exceptions import ValidationError


class TestSemanticSearchInitialization:
    """Test SemanticSearch class initialization and validation."""

    def test_init_valid_params_hybrid_mode(self):
        """Test initialization with valid parameters and hybrid mode."""
        ss = SemanticSearch(
            cvc_model_id="cvc_9f8e7d6c",
            q="竞争对手在东南亚的电动化布局战略",
            mode="hybrid",
            top_k=20,
            api_key="test_key",
        )
        assert ss.payload["cvc_model_id"] == "cvc_9f8e7d6c"
        assert ss.payload["q"] == "竞争对手在东南亚的电动化布局战略"
        assert ss.payload["mode"] == "hybrid"
        assert ss.payload["top_k"] == 20

    def test_init_valid_params_semantic_mode(self):
        """Test initialization with semantic mode."""
        ss = SemanticSearch(
            cvc_model_id="cvc_12345678",
            q="AI技术发展趋势",
            mode="semantic",
            api_key="test_key",
        )
        assert ss.payload["mode"] == "semantic"

    def test_init_valid_params_keyword_mode(self):
        """Test initialization with keyword mode."""
        ss = SemanticSearch(
            cvc_model_id="cvc_abcdef12",
            q="电动汽车电池",
            mode="keyword",
            api_key="test_key",
        )
        assert ss.payload["mode"] == "keyword"

    def test_init_with_all_optional_params(self):
        """Test initialization with all optional parameters."""
        ss = SemanticSearch(
            cvc_model_id="cvc_9f8e7d6c",
            q="市场分析",
            mode="hybrid",
            top_k=30,
            time_range="3m",
            days=90,
            category="market",
            region="asia",
            language="zh",
            min_tscore=0.6,
            api_key="test_key",
        )
        assert ss.payload["time_range"] == "3m"
        assert ss.payload["days"] == 90
        assert ss.payload["category"] == "market"
        assert ss.payload["region"] == "asia"
        assert ss.payload["language"] == "zh"
        assert ss.payload["min_tscore"] == 0.6

    def test_init_invalid_cvc_model_id_format(self):
        """Test that invalid cvc_model_id raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            SemanticSearch(
                cvc_model_id="invalid_id",  # Missing cvc_ prefix
                q="test",
                api_key="test_key",
            )
        assert "must match pattern" in str(exc_info.value)

    def test_init_cvc_model_id_with_underscore(self):
        """Test that cvc_model_id with underscore is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SemanticSearch(
                cvc_model_id="cvc_abc_def",  # Contains underscore
                q="test",
                api_key="test_key",
            )
        assert "must match pattern" in str(exc_info.value)

    def test_init_empty_query(self):
        """Test that empty query raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            SemanticSearch(
                cvc_model_id="cvc_9f8e7d6c",
                q="",
                api_key="test_key",
            )
        assert "requires 'q' parameter" in str(exc_info.value)

    def test_init_invalid_mode(self):
        """Test that invalid mode raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            SemanticSearch(
                cvc_model_id="cvc_9f8e7d6c",
                q="test",
                mode="invalid_mode",
                api_key="test_key",
            )
        assert "Unsupported mode" in str(exc_info.value)


class TestSemanticSearchFunction:
    """Test the semantic_search function."""

    @patch("casee.search.HTTPClient")
    def test_semantic_search_hybrid_mode(self, mock_client_class):
        """Test semantic_search with hybrid mode mocking HTTP client."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "search_metadata": {"id": "test-123", "status": "ok"},
            "search_parameters": {"q": "test query"},
            "search_information": {
                "fusion": {
                    "bm25_hits": 15,
                    "vector_hits": 20,
                    "degraded": [],
                    "rrf_score": 0.85,
                }
            },
            "organic_results": [
                {
                    "title": "Test Result",
                    "description": "Test description",
                    "source_id": "reuters",
                    "tscore": 0.8,
                    "published_at": "2026-08-25T10:00:00Z",
                }
            ],
        }
        mock_client_class.return_value = mock_client

        result = semantic_search(
            cvc_model_id="cvc_9f8e7d6c",
            q="竞争对手在东南亚的电动化布局战略",
            mode="hybrid",
            top_k=20,
            api_key="test_key",
        )

        assert "search_metadata" in result
        assert "organic_results" in result
        assert len(result["organic_results"]) == 1
        assert result["organic_results"][0]["title"] == "Test Result"
        assert "fusion" in result.get("search_information", {})

        # Verify the POST request was made
        mock_client.post.assert_called_once()

    @patch("casee.search.HTTPClient")
    def test_semantic_search_semantic_mode(self, mock_client_class):
        """Test semantic_search with pure semantic mode."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "search_metadata": {"id": "test-456", "status": "ok"},
            "search_information": {
                "fusion": {
                    "bm25_hits": 0,
                    "vector_hits": 20,
                    "degraded": [],
                    "mode": "semantic",
                }
            },
            "organic_results": [],
        }
        mock_client_class.return_value = mock_client

        result = semantic_search(
            cvc_model_id="cvc_12345678",
            q="AI技术发展趋势",
            mode="semantic",
            api_key="test_key",
        )

        assert result["search_information"]["fusion"]["mode"] == "semantic"
        assert result["search_information"]["fusion"]["vector_hits"] == 20

    @patch("casee.search.HTTPClient")
    def test_semantic_search_with_filters(self, mock_client_class):
        """Test semantic_search with additional filters."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "search_metadata": {"id": "test-789"},
            "organic_results": [],
        }
        mock_client_class.return_value = mock_client

        result = semantic_search(
            cvc_model_id="cvc_abcdef12",
            q="电动汽车市场",
            mode="hybrid",
            time_range="3m",
            category="market",
            region="asia",
            language="zh",
            min_tscore=0.6,
            api_key="test_key",
        )

        assert "search_metadata" in result

        # Verify the payload includes filters
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/v1/semantic-search"
        payload = call_args[1]["json_data"]
        assert payload["time_range"] == "3m"
        assert payload["category"] == "market"
        assert payload["region"] == "asia"
        assert payload["language"] == "zh"
        assert payload["min_tscore"] == 0.6


class TestSemanticSearchFusionStats:
    """Test fusion statistics retrieval."""

    @patch("casee.search.HTTPClient")
    def test_get_fusion_stats(self, mock_client_class):
        """Test get_fusion_stats method."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "search_metadata": {"id": "test"},
            "search_information": {
                "fusion": {
                    "bm25_hits": 15,
                    "vector_hits": 20,
                    "degraded": [],
                    "rrf_score": 0.85,
                }
            },
            "organic_results": [],
        }
        mock_client_class.return_value = mock_client

        ss = SemanticSearch(
            cvc_model_id="cvc_9f8e7d6c",
            q="test",
            api_key="test_key",
        )
        ss.get_dict()
        fusion = ss.get_fusion_stats()

        assert fusion["bm25_hits"] == 15
        assert fusion["vector_hits"] == 20
        assert fusion["degraded"] == []
        assert fusion["rrf_score"] == 0.85


class TestSemanticSearchContextManager:
    """Test context manager protocol."""

    @patch("casee.search.HTTPClient")
    def test_context_manager_enter_exit(self, mock_client_class):
        """Test that context manager properly opens and closes."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "search_metadata": {"id": "test"},
            "organic_results": [],
        }
        mock_client_class.return_value = mock_client

        with SemanticSearch(
            cvc_model_id="cvc_9f8e7d6c",
            q="test",
            api_key="test_key",
        ) as ss:
            ss.get_dict()
            assert ss.last_response is not None

        # Verify close was called
        mock_client.close.assert_called_once()


class TestCvcModelIdFormat:
    """Test cvc_model_id format validation edge cases."""

    @pytest.mark.parametrize("model_id", [
        "cvc_abcdef12",        # Valid: 10 chars total (cvc_ + 6 chars)
        "cvc_1234567890abcdef",  # Valid: 20 chars total (cvc_ + 16 chars)
        "cvc_" + "a" * 28,     # Valid: 32 chars total (cvc_ + 28 chars, max)
    ])
    def test_valid_cvc_model_ids(self, model_id):
        """Test various valid cvc_model_id formats."""
        try:
            SemanticSearch(cvc_model_id=model_id, q="test", api_key="test_key")
        except ValidationError:
            pytest.fail(f"Valid cvc_model_id '{model_id}' raised ValidationError")

    @pytest.mark.parametrize("model_id", [
        "",                      # Empty
        "cvc_",                  # Too short (no chars after prefix)
        "cvc_a",                 # Too short (only 1 char)
        "invalid_prefix_123456", # Wrong prefix
        "cvc_ABCDEF12",          # Contains uppercase
        "cvc_abc-def",           # Contains hyphen
        "cvc_abc def",           # Contains space
        "cvc_abcdef1234567890abcdef12345678901",  # Too long (>32 chars)
    ])
    def test_invalid_cvc_model_ids(self, model_id):
        """Test various invalid cvc_model_id formats."""
        with pytest.raises(ValidationError):
            SemanticSearch(cvc_model_id=model_id, q="test", api_key="test_key")
