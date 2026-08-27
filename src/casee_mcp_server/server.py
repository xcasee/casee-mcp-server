"""casee_mcp_server — CaSee Intelligence MCP Server.

将 CaSee 竞争情报检索能力封装为 MCP Tools，供 AI Agent 调用。

提供 6 个 MCP Tool：
- find_trusted_sources: 信源检索
- search_intelligence: 复杂逻辑情报检索
- analyze_trend: 趋势分析
- aggregate_by_source: 信源聚合分析
- semantic_search: 语义检索（基于 CVC 模型的两阶段混合检索）
- search_with_cvc: 带 CVC 归集的高级检索

启动方式:
    # stdio 模式（WorkBuddy / Claude Desktop 推荐）
    CASEE_API_KEY=sk_xxx python -m casee_mcp_server

    # Streamable-HTTP 模式（Trae Work / WorkBuddy / 远程 Agent 推荐）
    CASEE_API_KEY=sk_xxx MCP_TRANSPORT=streamable-http python -m casee_mcp_server
"""

import os
import re
from collections import Counter, defaultdict
from mcp.server import MCPServer
from casee import search_sources, search_advanced, semantic_search
from casee.exceptions import CaseeError, AuthenticationError


mcp = MCPServer(
    name="casee",
    description="CaSee 企业竞争情报检索服务 —— 可信信源查询 + 复杂逻辑情报检索 + 趋势分析 + 语义检索",
)


def _sdk_call(fn, **kwargs):
    """Wrap an SDK call so any exception becomes a structured error dict
    instead of an uncaught exception that surfaces as an opaque MCP failure.
    """
    try:
        return fn(**kwargs)
    except CaseeError as exc:
        return {
            "error": exc.__class__.__name__,
            "message": str(exc),
            "hint": _diagnose_sdk_error(exc),
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "error": "UnexpectedError",
            "message": f"{type(exc).__name__}: {exc}",
        }


def _diagnose_sdk_error(exc: CaseeError) -> str:
    """Best-effort hint based on the error type/message."""
    msg = str(exc)
    if "HTML" in msg and ("CASEE_API_BASE_URL" in msg or "web page" in msg or "landing page" in msg):
        return (
            "CASEE_API_BASE_URL 指向了一个网页而非 CaSee API 服务器。"
            "请检查 MCP Server 环境变量 CASEE_API_BASE_URL 是否指向了 FastAPI 服务端口"
            "（例如 http://host.docker.internal:8000 或宿主 IP:8000），"
            "而不是门户网站/静态站点。"
        )
    if isinstance(exc, AuthenticationError):
        return "API Key 无效或缺失，请检查 CASEE_API_KEY 环境变量。"
    if "timed out" in msg.lower():
        return "请求超时，请稍后重试或增大 CASEE_TIMEOUT。"
    return ""

# ---- Tool 1: 信源检索 ----


@mcp.tool()
def find_trusted_sources(
    keyword: str = "",
    category: str = "",
    region: str = "",
    language: str = "",
    min_tscore: float = 0.6,
    sample_size: int = 2,
    limit: int = 20,
) -> dict:
    """检索可信信源：按关键字/类别/地区/语言查找信源，按可信度 tscore 过滤。

    tscore 含义：
    - >= 0.8: 高可信（通讯社、权威媒体）
    - 0.6-0.8: 中可信（主流媒体、专业媒体）
    - < 0.6: 低可信（自媒体、个人博客等，建议谨慎使用）

    使用建议：先调用此工具获取高可信信源 ID 列表，
    再调用 search_intelligence 并传入 source_ids 限定检索范围。

    Args:
        keyword: 关键字模糊匹配（匹配 source_id / name / description）
        category: 信源类别（wire=通讯社, mainstream=主流媒体, tech=科技, market=市场, ai=AI 等）
        region: 地区过滤（us / europe / china / ...）
        language: 语言过滤（en / zh / ja / ko / ...）
        min_tscore: 最低可信度阈值，0.0-1.0，建议 >= 0.6
        sample_size: 每个信源返回的样例情报条数
        limit: 返回信源数上限

    Returns:
        dict: {count, total, sources: [{source_id, name, tscore, category, sample_data, ...}]}
    """
    return _sdk_call(
        search_sources,
        keyword=keyword or None,
        category=category or None,
        region=region or None,
        language=language or None,
        min_tscore=min_tscore,
        sample_size=sample_size,
        limit=limit,
    )


# ---- Tool 2: 情报检索 ----


@mcp.tool()
def search_intelligence(
    q: str,
    source_ids: list[str] | None = None,
    must_keywords: str = "",
    should_keywords: str = "",
    not_keywords: str = "",
    min_tscore: float = 0.5,
    days: int = 30,
    start_date: str = "",
    end_date: str = "",
    category: str = "",
    region: str = "",
    language: str = "",
    limit: int = 20,
) -> dict:
    """复杂逻辑情报检索，支持 AND/OR/NOT/精确短语/同义词组。

    查询语法（q 参数，优先级最高）：
    - ``+word``: 必须包含（AND 逻辑）
    - ``word1 word2``: 任一匹配（OR 逻辑）
    - ``-word``: 排除（NOT 逻辑）
    - ``"exact phrase"``: 精确短语匹配
    - ``word1|word2``: 同义词组（OR 逻辑）
    - ``(a OR b)``: 分组逻辑

    典型用法（两段式检索）：
    1. 先调用 find_trusted_sources 获取可信信源 ID
    2. 调用本工具，传入 source_ids=<上一步结果> 限定检索范围

    Args:
        q: 查询语法字符串，如 +(Tesla|BYD) +(EV|battery) -rumor
        source_ids: 信源 ID 列表（来自 find_trusted_sources 的结果）
        must_keywords: AND 关键词（逗号分隔），如 "EV,battery"
        should_keywords: OR 关键词（逗号分隔），如 "Tesla,BYD"
        not_keywords: NOT 排除关键词（逗号分隔），如 "rumor,speculation"
        min_tscore: 最低情报可信度，0.0-1.0
        days: 时间窗口（天），与 start_date/end_date 二选一
        start_date: 精确起始日期 YYYY-MM-DD
        end_date: 精确结束日期 YYYY-MM-DD
        category: 信源类别过滤
        region: 信源地区过滤
        language: 信源语言过滤
        limit: 返回条数上限

    Returns:
        dict: {count, items: [{title, description, source_id, tscore, published_at, ...}]}
    """
    return _sdk_call(
        search_advanced,
        q=q,
        source_ids=source_ids,
        must_keywords=must_keywords.split(",") if must_keywords else None,
        should_keywords=should_keywords.split(",") if should_keywords else None,
        not_keywords=not_keywords.split(",") if not_keywords else None,
        min_tscore=min_tscore,
        days=days,
        start_date=start_date or None,
        end_date=end_date or None,
        category=category or None,
        region=region or None,
        language=language or None,
        limit=limit,
    )


# ---- Tool 3: 趋势分析 ----


@mcp.tool()
def analyze_trend(
    q: str,
    source_ids: list[str] | None = None,
    min_tscore: float = 0.5,
    days: int = 90,
    limit: int = 200,
) -> dict:
    """对检索结果进行时间趋势分析，输出按日/周聚合的情报量变化。

    适用场景：
    - 追踪某话题的舆论热度变化
    - 发现某厂商/产品的报道周期规律
    - 识别突发事件的时间节点

    Args:
        q: 查询语法（同 search_intelligence）
        source_ids: 信源 ID 列表
        min_tscore: 最低可信度
        days: 分析时间窗口（天）
        limit: 检索条数上限

    Returns:
        dict: {trend: [{date, count, top_sources, top_keywords}], summary: {...}}
    """
    result = _sdk_call(
        search_advanced,
        q=q, source_ids=source_ids, min_tscore=min_tscore,
        days=days, limit=limit,
    )
    if isinstance(result, dict) and "error" in result:
        return result
    items = result.get("organic_results", []) or result.get("items", [])

    by_date = defaultdict(lambda: {"count": 0, "sources": Counter(), "keywords": Counter()})
    for item in items:
        pub = (item.get("published_at") or item.get("pub_date") or "")[:10]
        if not pub:
            continue
        by_date[pub]["count"] += 1
        sid = item.get("source_id") or item.get("source", {}).get("source_id", "")
        if sid:
            by_date[pub]["sources"][sid] += 1
        title = item.get("title", "")
        for word in title.split():
            if len(word) > 3:
                by_date[pub]["keywords"][word.lower()] += 1

    trend = []
    for date in sorted(by_date.keys()):
        d = by_date[date]
        trend.append({
            "date": date,
            "count": d["count"],
            "top_sources": [s for s, _ in d["sources"].most_common(3)],
            "top_keywords": [k for k, _ in d["keywords"].most_common(5)],
        })

    return {
        "query": q,
        "total_items": len(items),
        "date_range": f"{trend[0]['date']} ~ {trend[-1]['date']}" if trend else "N/A",
        "trend": trend,
        "summary": {
            "avg_daily": round(len(items) / max(len(trend), 1), 1),
            "peak_date": max(trend, key=lambda x: x["count"]) if trend else None,
            "total_sources": len(set(
                item.get("source_id") or item.get("source", {}).get("source_id", "")
                for item in items
            )),
        },
    }


# ---- Tool 4: 信源聚合分析 ----


@mcp.tool()
def aggregate_by_source(
    q: str,
    source_ids: list[str] | None = None,
    min_tscore: float = 0.5,
    days: int = 30,
    limit: int = 100,
) -> dict:
    """按信源聚合分析检索结果，输出各信源的报道量、平均 tscore、情感倾向。

    适用场景：
    - 评估不同信源对某话题的关注度
    - 对比各信源报道的立场差异
    - 筛选高质量信源进行持续跟踪

    Args:
        q: 查询语法（同 search_intelligence）
        source_ids: 信源 ID 列表
        min_tscore: 最低可信度
        days: 时间窗口
        limit: 检索条数上限

    Returns:
        dict: {sources: [{source_id, name, count, avg_tscore, sample_titles}]}
    """
    result = _sdk_call(
        search_advanced,
        q=q, source_ids=source_ids, min_tscore=min_tscore,
        days=days, limit=limit,
    )
    if isinstance(result, dict) and "error" in result:
        return result
    items = result.get("organic_results", []) or result.get("items", [])

    by_source = defaultdict(lambda: {"count": 0, "tscores": [], "titles": []})
    for item in items:
        sid = item.get("source_id") or item.get("source", {}).get("source_id", "unknown")
        name = item.get("source", {}).get("name", sid)
        ts = item.get("tscore") or item.get("source", {}).get("tscore", 0)
        by_source[sid]["name"] = name
        by_source[sid]["count"] += 1
        by_source[sid]["tscores"].append(float(ts) if ts else 0)
        by_source[sid]["titles"].append(item.get("title", "")[:80])

    sources = []
    for sid, data in sorted(by_source.items(), key=lambda x: -x[1]["count"]):
        tscores = data["tscores"]
        sources.append({
            "source_id": sid,
            "name": data["name"],
            "count": data["count"],
            "avg_tscore": round(sum(tscores) / len(tscores), 3) if tscores else 0,
            "sample_titles": data["titles"][:3],
        })

    return {
        "query": q,
        "total_items": len(items),
        "total_sources": len(sources),
        "sources": sources,
    }


# ---- Tool 5: 语义检索（基于 CVC 模型）----


@mcp.tool()
def semantic_search_tool(
    cvc_model_id: str,
    q: str,
    mode: str = "hybrid",
    top_k: int = 20,
    time_range: str = "",
    days: int = 0,
    category: str = "",
    region: str = "",
    language: str = "",
    min_tscore: float = 0.0,
) -> dict:
    """基于 CVC 模型的语义检索：BM25 + 向量 ANN + RRF 融合的两阶段检索。

    适用场景：
    - 竞品技术路线对比分析
    - 市场趋势深度研判
    - 客户需求语义洞察
    - 跨语种情报关联分析

    检索模式：
    - ``hybrid``（默认）：关键词检索 + 向量语义检索 + RRF 融合
    - ``semantic``：纯向量语义检索（适用于概念性、抽象性查询）
    - ``keyword``：纯关键词检索（适用于精确匹配场景）

    Args:
        cvc_model_id: CVC 模型 ID，格式 ``^cvc_[a-z0-9]{8,32}$``（必填）
        q: 查询文本，支持自然语言，如 "竞争对手在东南亚的电动化布局战略"
        mode: 检索模式，可选值：hybrid / semantic / keyword
        top_k: 返回条数上限，默认 20
        time_range: 预设时间范围（1d / 7d / 1m / 3m），与 days 二选一
        days: 自定义时间窗口（天），0 表示不限制
        category: 信源类别过滤
        region: 信源地区过滤
        language: 信源语言过滤
        min_tscore: 最低可信度（0.0-1.0）

    Returns:
        dict: SerpAPI 风格响应，含 search_information.fusion 统计
            {
                search_information: {
                    fusion: { bm25_hits, vector_hits, degraded, ... }
                },
                organic_results: [{title, description, source_id, ...}]
            }
    """
    # 校验 cvc_model_id 格式
    if not re.match(r"^cvc_[a-z0-9]{8,32}$", cvc_model_id):
        return {
            "error": "Invalid cvc_model_id format",
            "message": f"cvc_model_id must match pattern ^cvc_[a-z0-9]{{8,32}}$, got: {cvc_model_id!r}",
        }

    # 校验 mode
    valid_modes = ("hybrid", "semantic", "keyword")
    if mode not in valid_modes:
        return {
            "error": "Invalid mode",
            "message": f"mode must be one of {valid_modes}, got: {mode!r}",
        }

    kwargs: dict = {
        "cvc_model_id": cvc_model_id,
        "q": q,
        "mode": mode,
        "top_k": top_k,
    }
    if time_range:
        kwargs["time_range"] = time_range
    if days > 0:
        kwargs["days"] = days
    if category:
        kwargs["category"] = category
    if region:
        kwargs["region"] = region
    if language:
        kwargs["language"] = language
    if min_tscore > 0:
        kwargs["min_tscore"] = min_tscore

    return _sdk_call(semantic_search, **kwargs)


# ---- Tool 6: 带 CVC 归集的高级检索 ----


@mcp.tool()
def search_with_cvc(
    q: str,
    cvc_model_id: str = "",
    source_ids: list[str] | None = None,
    must_keywords: str = "",
    should_keywords: str = "",
    not_keywords: str = "",
    min_tscore: float = 0.5,
    days: int = 30,
    start_date: str = "",
    end_date: str = "",
    category: str = "",
    region: str = "",
    language: str = "",
    limit: int = 20,
) -> dict:
    """复杂逻辑情报检索（支持 CVC 归集），检索结果可归集到指定 CVC 模型库。

    与 search_intelligence 功能相同，但支持 cvc_model_id 参数。
    当传入 cvc_model_id 时，检索结果会异步归集到该模型的
    OpenSearch/Qdrant 语义检索库，供 semantic_search 二阶段使用。

    典型用法（三阶段智能检索）：
    1. 调用 find_trusted_sources 获取可信信源 ID
    2. 调用本工具，传入 cvc_model_id 归集检索结果
    3. 调用 semantic_search_tool 进行语义深度检索

    Args:
        q: 查询语法字符串，如 +(Tesla|BYD) +(EV|battery) -rumor
        cvc_model_id: CVC 模型 ID（可选），格式 ``^cvc_[a-z0-9]{8,32}$``
            传入时检索结果将归集到语义检索库；不传则行为与 search_intelligence 一致
        source_ids: 信源 ID 列表（来自 find_trusted_sources 的结果）
        must_keywords: AND 关键词（逗号分隔），如 "EV,battery"
        should_keywords: OR 关键词（逗号分隔），如 "Tesla,BYD"
        not_keywords: NOT 排除关键词（逗号分隔），如 "rumor,speculation"
        min_tscore: 最低情报可信度，0.0-1.0
        days: 时间窗口（天），与 start_date/end_date 二选一
        start_date: 精确起始日期 YYYY-MM-DD
        end_date: 精确结束日期 YYYY-MM-DD
        category: 信源类别过滤
        region: 信源地区过滤
        language: 信源语言过滤
        limit: 返回条数上限

    Returns:
        dict: {count, items: [{title, description, source_id, tscore, published_at, ...}]}
            若指定 cvc_model_id，响应中会包含 cvc_sync_status 字段
    """
    # 校验 cvc_model_id 格式
    if cvc_model_id and not re.match(r"^cvc_[a-z0-9]{8,32}$", cvc_model_id):
        return {
            "error": "Invalid cvc_model_id format",
            "message": f"cvc_model_id must match pattern ^cvc_[a-z0-9]{{8,32}}$, got: {cvc_model_id!r}",
        }

    kwargs: dict = {
        "q": q,
        "source_ids": source_ids,
        "must_keywords": must_keywords.split(",") if must_keywords else None,
        "should_keywords": should_keywords.split(",") if should_keywords else None,
        "not_keywords": not_keywords.split(",") if not_keywords else None,
        "min_tscore": min_tscore,
        "days": days,
        "start_date": start_date or None,
        "end_date": end_date or None,
        "category": category or None,
        "region": region or None,
        "language": language or None,
        "limit": limit,
    }

    # 如果指定了 cvc_model_id，添加到请求参数
    if cvc_model_id:
        kwargs["cvc_model_id"] = cvc_model_id

    result = _sdk_call(search_advanced, **kwargs)
    if isinstance(result, dict) and "error" in result:
        return result

    # 添加 CVC 归集状态提示
    if cvc_model_id:
        result["cvc_sync_status"] = {
            "cvc_model_id": cvc_model_id,
            "items_found": len(result.get("items", [])),
            "message": f"检索结果已异步归集到 CVC 模型 {cvc_model_id}，可使用 semantic_search_tool 进行语义深度检索",
        }

    return result


def main():
    """MCP Server 入口。"""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8100"))
    path = os.environ.get("MCP_PATH", "/mcp")

    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=host, port=port, streamable_http_path=path)
    elif transport == "sse":
        mcp.run(transport="sse", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()