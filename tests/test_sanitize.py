"""MCP 侧对外投影回归测试：禁止泄漏信源清单与采集通道指纹。"""
from __future__ import annotations

import json

import pytest

from casee_mcp_server.sanitize import (
    PUBLIC_CHANNEL,
    PUBLIC_SOURCE_NAME,
    is_google_news_url,
    normalize_source_name,
    opaque_source_id,
    safe_public_url,
    sanitize_payload,
)

FORBIDDEN = ("news.google.com", "Google News", "Google新闻", "google新闻",
             "/rss/search", "gnews", "source_url", "source_ids")


def _assert_clean(payload) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for token in FORBIDDEN:
        assert token not in blob, f"{token} leaked in {blob}"


def test_gnews_url_detection():
    assert is_google_news_url("https://news.google.com/rss/articles/abc?oc=5")
    assert not is_google_news_url("https://www.reuters.com/business/x")


def test_source_name_normalized():
    assert normalize_source_name("Google新闻 (Reuters)") == PUBLIC_SOURCE_NAME
    assert normalize_source_name("gnews") == PUBLIC_SOURCE_NAME


def test_url_falls_back_to_resolved():
    assert safe_public_url("https://news.google.com/rss/articles/x",
                           fallback="https://www.reuters.com/a") == "https://www.reuters.com/a"


def test_opaque_source_id_stable():
    once = opaque_source_id("reuters-business")
    assert once.startswith("src_") and "reuters" not in once
    assert opaque_source_id("reuters-business") == once
    assert opaque_source_id(once) == once


def test_sanitize_drops_collector_and_channel_fields():
    payload = {
        "items": [
            {
                "title": "EV market",
                "source_id": "reuters-business",
                "source_name": "Google新闻 (Reuters)",
                "source_url": "https://www.reuters.com",
                "source_domain": "reuters.com",
                "source_type": "rss",
                "url": "https://news.google.com/rss/articles/1",
                "resolved_url": "https://www.reuters.com/a",
                "data_source": "gnews",
            }
        ],
        "meta": {"fallback": True, "engine": "gnews", "source_ids": ["reuters"]},
        "search_parameters": {"q": "+EV", "days": 30, "fallback": "true"},
    }
    out = sanitize_payload(payload)
    assert out["items"][0]["url"] == "https://www.reuters.com/a"
    assert out["items"][0]["source_name"] == PUBLIC_SOURCE_NAME
    assert out["items"][0]["data_source"] == PUBLIC_CHANNEL
    assert out["meta"] == {}
    assert out["search_parameters"] == {"q": "+EV", "days": 30}
    _assert_clean(out)


def test_sanitize_is_idempotent():
    payload = {"source": {"name": "Google News", "url": "https://news.google.com/x"}}
    once = sanitize_payload(payload)
    assert sanitize_payload(once) == once


@pytest.mark.parametrize("key", ["source_url", "feed_url", "rss_url", "collected_from",
                                "source_domain", "source_type", "sample_data",
                                "guid", "tscore_factors"])
def test_dropped_keys_absent(key):
    assert key not in sanitize_payload({key: "x", "ok": 1})


def test_gnews_guid_not_exposed():
    """核查报告 §2.2：``guid`` 以 ``CBMi`` 开头即 Google News 聚合产物。"""
    out = sanitize_payload(
        {
            "organic_results": [
                {
                    "source_id": "gnews-eu.36kr.com",
                    "source_name": "36氪",
                    "guid": "CBMiU0FVX3lxTE9oR2h5b0hQc0Vn",
                    "tscore_factors": {"warnings": ["tier_missing"]},
                    "url": "https://eu.36kr.com/p/123",
                }
            ]
        }
    )["organic_results"][0]
    assert "guid" not in out and "tscore_factors" not in out
    assert out["source_name"] == "36氪"
    assert out["source_id"].startswith("src_")
    assert "CBMi" not in str(out)
