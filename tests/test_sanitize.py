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
    public_source,
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


def test_public_source_projects_source_dim_naming():
    """聚合口径修复（P1-1）：public_source 投影补 source_* 规范命名。"""
    out = public_source({
        "name": "Reuters",
        "source_category": "wire",
        "source_region": "us",
        "source_language": "en",
        "source_tscore": 0.8,
    })
    assert out["source_category"] == "wire"
    assert out["source_region"] == "us"
    assert out["source_language"] == "en"
    assert out["source_tscore"] == 0.8
    # 旧命名兼容保留一个版本
    legacy = public_source({"name": "Reuters", "category": "blog", "tscore": 0.5})
    assert legacy["category"] == "blog"
    assert legacy["tscore"] == 0.5


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


def test_citation_attached_for_intelligence_items():
    """P1-2 第 3 层：情报条目自动携带 citation 引用指引（分支全覆盖）。"""
    payload = {"organic_results": [
        {"news_id": "n1", "title": "t1", "url": "https://a.com/p/1",
         "summary": "s", "description": "d", "abstract": "a",
         "summary_origin": "llm_expanded"},
        {"news_id": "n2", "title": "t2",
         "link": "https://news.google.com/rss/articles/x",
         "resolved_url": "https://www.reuters.com/a",
         "summary": "s2", "summary_origin": "source_abstract"},
        {"news_id": "n3", "title": "t3", "url": "https://b.com/p/3",
         "summary": "检测到异常访问，请完成安全验证",
         "content_flags": ["anti_bot_page"]},
        {"news_id": "n4", "title": "t4", "url": "https://c.com/p/4",
         "summary": "s4"},
    ]}
    once = sanitize_payload(payload)
    items = once["organic_results"]

    c1 = items[0]["citation"]
    assert c1["quote_from"] == ["title", "description", "abstract"]
    assert c1["url"] == "https://a.com/p/1"
    assert "AI 扩写" in c1["summary_note"] and "禁止" in c1["summary_note"]

    # Google News link 清洗后引用 URL 回落到 resolved_url
    assert items[1]["citation"]["url"] == "https://www.reuters.com/a"
    assert "信源原生摘要" in items[1]["citation"]["summary_note"]

    assert "anti_bot_page" in items[2]["citation"]["summary_note"]

    # 存量数据无 summary_origin → unknown 警示
    assert "未知" in items[3]["citation"]["summary_note"]

    # citation 覆盖式重建 → 幂等
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
