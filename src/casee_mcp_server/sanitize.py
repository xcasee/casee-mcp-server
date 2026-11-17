"""对外响应投影（MCP 侧）。

产品约束：
1. Agent 侧无法探测 CaSee 支持哪些信源：``source_id`` 一律输出
   ``src_<hash>``，不再下发信源 url / domain / type / slug / 插件配置。
2. Agent 侧无法探测采集通道：响应中的 Google News 指纹
   （``news.google.com`` 链接、``Google新闻 (xxx)`` 信源名、``gnews`` 通道标识）
   统一归一或清空。

所有工具返回都必须经过 :func:`sanitize_payload`，统一在 ``_safe_call`` 与
``_report_call`` 两个出口处接入。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Mapping

PUBLIC_SOURCE_NAME = "公开媒体"
PUBLIC_CHANNEL = "public_news"

_GNEWS_HOST_RE = re.compile(
    r"^https?://(?:"
    r"news\.google\.(?:com|cn|com\.hk|com\.tw)"
    r"|news\.url\.google\.com"
    r"|www\.google\.com/news"
    r")(?:[/:?#]|$)",
    re.I,
)
_GNEWS_TEXT_RE = re.compile(r"google\s*新闻|google\s*news|谷歌新闻|谷歌资讯", re.I)
_GNEWS_CHANNEL_RE = re.compile(r"^(?:gnews|google[-_]?news)", re.I)

_DROP_FIELDS = frozenset(
    {
        "source_url",
        "sourceUrl",
        "source_icon_url",
        "sourceIconUrl",
        "feed_url",
        "feedUrl",
        "rss_url",
        "rssUrl",
        "json_endpoint",
        "jsonEndpoint",
        "collected_from",
        "collectedFrom",
        "source_domain",
        "sourceDomain",
        "source_type",
        "sourceType",
        "source_slug",
        "sourceSlug",
        "connector",
        "plugin",
        "plugin_name",
        "properties",
        # Google News 条目 id（``CBMi...`` base64）：聚合层最强的判定指纹
        "guid",
        # T-Score 打分因子（含 tier/authority/warnings），属信源分级内部口径
        "tscore_factors",
        "tscoreFactors",
        "sample_data",
        "sampleData",
        "sample_items",
        # 检索通道/范围元数据：任何层级都不下发
        "fallback",
        "fallback_used",
        "fallbackUsed",
        "engine",
        "search_engine",
        "searchEngine",
        "source_ids",
        "sourceIds",
        "sources_searched",
        "sourcesSearched",
        "source_catalog",
        "sourceCatalog",
        # 按来源的分布/榜单：等价于暴露在用的信源清单
        "sources",
        "top_sources",
        "topSources",
        "source_distribution",
        "sourceDistribution",
        "source_breakdown",
        "sourceBreakdown",
    }
)

_URL_FIELDS = frozenset(
    {
        "link",
        "url",
        "resolved_url",
        "original_url",
        "permalink",
        "icon_url",
        "logo",
        "image_url",
        "thumbnail_small_url",
        "thumbnail_large_url",
        "avatar",
    }
)

_NAME_FIELDS = frozenset(
    {
        "source_name",
        "sourceName",
        "publisher",
        "publisherName",
        "site_name",
        "siteName",
        "news_source",
        "newsSource",
        "intelligence_source",
        "intelligenceSource",
        "source_label",
        "sourceLabel",
    }
)
_SOURCE_OBJECT_FIELDS = frozenset({"source", "source_info", "sourceInfo"})
_DROP_SEARCH_PARAM_KEYS = frozenset({"fallback", "engine", "source_ids", "sourceIds"})


def is_google_news_url(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and bool(_GNEWS_HOST_RE.match(text))


def normalize_source_name(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("name", "title", "source_name", "label", "display_name"):
            inner = value.get(key)
            if inner:
                return normalize_source_name(inner)
        return PUBLIC_SOURCE_NAME
    text = str(value or "").strip()
    if not text:
        return PUBLIC_SOURCE_NAME
    if _GNEWS_TEXT_RE.search(text) or _GNEWS_CHANNEL_RE.match(text):
        return PUBLIC_SOURCE_NAME
    return text


def safe_public_url(value: Any, *, fallback: Any = "") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if is_google_news_url(text):
        alt = str(fallback or "").strip()
        return alt if alt and not is_google_news_url(alt) else ""
    return text


def opaque_source_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("src_") and len(text) >= 8:
        return text
    digest = hashlib.sha256(f"casee-public-source-v1:{text}".encode("utf-8")).hexdigest()
    return f"src_{digest[:12]}"


def public_source(doc: Any) -> Dict[str, Any]:
    if not isinstance(doc, Mapping):
        return {"name": normalize_source_name(doc)}
    out: Dict[str, Any] = {"name": normalize_source_name(doc)}
    raw_id = doc.get("source_id") or doc.get("id") or doc.get("slug")
    if raw_id:
        out["source_id"] = opaque_source_id(raw_id)
    for key in ("category", "region", "language", "tscore"):
        if doc.get(key) is not None:
            out[key] = doc[key]
    return out


def sanitize_payload(value: Any) -> Any:
    """递归投影任意 JSON 结构（幂等）。"""
    if isinstance(value, Mapping):
        resolved = value.get("resolved_url") or value.get("original_url") or ""
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if key in _DROP_FIELDS:
                continue
            if key == "source_id":
                out[key] = opaque_source_id(item)
                continue
            if key in _SOURCE_OBJECT_FIELDS:
                out[key] = public_source(item) if isinstance(item, Mapping) else normalize_source_name(item)
                continue
            if key in _NAME_FIELDS:
                out[key] = normalize_source_name(item)
                continue
            if key in _URL_FIELDS:
                out[key] = safe_public_url(item, fallback=resolved)
                continue
            if key in ("search_parameters", "searchParameters") and isinstance(item, Mapping):
                out[key] = sanitize_payload(
                    {k: v for k, v in item.items() if k not in _DROP_SEARCH_PARAM_KEYS}
                )
                continue
            out[key] = sanitize_payload(item)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str) and _GNEWS_CHANNEL_RE.match(value.strip()):
        return PUBLIC_CHANNEL
    return value


__all__ = [
    "PUBLIC_CHANNEL",
    "PUBLIC_SOURCE_NAME",
    "is_google_news_url",
    "normalize_source_name",
    "opaque_source_id",
    "public_source",
    "safe_public_url",
    "sanitize_payload",
]
