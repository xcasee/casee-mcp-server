"""casee_mcp_server — CaSee Intelligence MCP Server.

将 CaSee 竞争情报检索能力封装为 MCP Tools，供 AI Agent 调用。

提供 5 个 MCP Tool：
- find_trusted_sources: 信源检索
- search_intelligence: 复杂逻辑情报检索
- analyze_trend: 趋势分析
- aggregate_by_source: 信源聚合分析
- get_intelligence_stats: 统计概览

启动方式:
    # stdio 模式（WorkBuddy / Claude Desktop 推荐）
    CASEE_API_KEY=sk_xxx python -m casee_mcp_server

    # Streamable-HTTP 模式（Trae Work / WorkBuddy / 远程 Agent 推荐）
    CASEE_API_KEY=sk_xxx MCP_TRANSPORT=streamable-http python -m casee_mcp_server
"""

import os
from collections import Counter, defaultdict
from mcp.server import MCPServer
from casee import search_sources, search_advanced, get_stats

mcp = MCPServer(
    name="casee",
    description="CaSee 企业竞争情报检索服务 —— 可信信源查询 + 复杂逻辑情报检索 + 趋势分析",
)

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
    return search_sources(
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
    return search_advanced(
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
    result = search_advanced(
        q=q, source_ids=source_ids, min_tscore=min_tscore,
        days=days, limit=limit,
    )
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
    result = search_advanced(
        q=q, source_ids=source_ids, min_tscore=min_tscore,
        days=days, limit=limit,
    )
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


# ---- Tool 5: 统计概览 ----


@mcp.tool()
def get_intelligence_stats() -> dict:
    """获取情报数据库整体统计信息：总情报量、信源数、今日新增等。

    Returns:
        dict: {total_intelligence, total_sources, total_sensors, today_items, ...}
    """
    return get_stats()


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