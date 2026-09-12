"""casee_mcp_server - CaSee Intelligence MCP Server.

Exposes CaSee competitive-intelligence retrieval as MCP tools for AI agents.

Provides the following MCP tools:
- find_trusted_sources      Source lookup (tscore / category / keyword)
- search_intelligence       Complex-logic intelligence retrieval
- analyze_trend             Time-series trend analysis
- aggregate_by_source       Per-source aggregation analysis
- semantic_search_tool      CVC-model semantic search (BM25 + vector ANN + RRF)
- search_with_cvc           Advanced retrieval that collects results into a CVC model
- batch_search              Server-side batch retrieval (v1.2)
- list_cvc_models_tool      List CVC models (v1.2)
- cvc_stats_tool            CVC collection statistics (v1.2)
- cvc_cache_purge_tool      CVC cache reset (three-store cleanup) (v1.2)
- cvc_cleanup_status_tool   CVC cleanup progress + three-store reconciliation (v1.2)
- cvc_reindex_tool          Replay collection audit to rebuild a CVC store (v1.2)
- intelligence_purge_tool   Full-history intelligence purge (v1.2)
- intelligence_purge_status_tool  Purge progress + residual reconciliation (v1.2)

Startup:
    # stdio mode (recommended for WorkBuddy / Claude Desktop)
    CASEE_API_KEY=sk_xxx python -m casee_mcp_server

    # Streamable-HTTP mode (recommended for Trae Work / WorkBuddy / remote agents)
    CASEE_API_KEY=sk_xxx MCP_TRANSPORT=streamable-http python -m casee_mcp_server
"""

import os
import re
from collections import Counter, defaultdict

from mcp.server import MCPServer
from casee import (
    search_sources,
    search_advanced,
    semantic_search,
    search_batch as sdk_search_batch,
    list_cvc_models as sdk_list_cvc_models,
    cvc_stats as sdk_cvc_stats,
    cvc_cache_purge as sdk_cvc_cache_purge,
    cvc_cleanup_status as sdk_cvc_cleanup_status,
    cvc_reindex as sdk_cvc_reindex,
    intelligence_purge as sdk_intelligence_purge,
    intelligence_purge_status as sdk_intelligence_purge_status,
)
from casee.exceptions import CaseeError, AuthenticationError


mcp = MCPServer(
    name="casee",
    description="CaSee enterprise competitive-intelligence retrieval - trusted-source lookup, "
                "complex retrieval, trend analysis, semantic search and CVC administration",
)


# ---- CVC model id validation (pattern ^cvc_[a-z0-9]{8,32}$) ----

_CVC_MODEL_ID_RE = re.compile(r"^cvc_[a-z0-9]{8,32}$")


def _safe_call(fn, *args, **kwargs):
    """Invoke an SDK call, converting any exception into a structured error dict.

    Returns the raw SDK result on success; on failure returns a dict with
    ``error`` / ``message`` (and a best-effort ``hint``) instead of letting an
    opaque exception propagate to the MCP client.
    """
    try:
        return fn(*args, **kwargs)
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


def _sdk_call(fn, **kwargs):
    """Keyword-only convenience wrapper around :func:`_safe_call`."""
    return _safe_call(fn, **kwargs)


def _diagnose_sdk_error(exc: CaseeError) -> str:
    """Best-effort hint based on the error type/message."""
    msg = str(exc)
    if "HTML" in msg and ("CASEE_API_BASE_URL" in msg or "web page" in msg or "landing page" in msg):
        return (
            "CASEE_API_BASE_URL points at a web page rather than the CaSee API server. "
            "Verify that the MCP Server CASEE_API_BASE_URL environment variable targets "
            "the FastAPI service port (e.g. http://host.docker.internal:8000 or host IP:8000), "
            "not a portal/static site."
        )
    if isinstance(exc, AuthenticationError):
        return "Invalid or missing API key; check the CASEE_API_KEY environment variable."
    if "timed out" in msg.lower():
        return "Request timed out; retry later or increase CASEE_TIMEOUT."
    return ""


def _validate_cvc_model_id(cvc_model_id: str) -> str:
    """Return a validation-error dict if the id is malformed, else an empty string.

    The MCP tool layer cannot raise, so callers branch on a non-empty return.
    """
    if not _CVC_MODEL_ID_RE.match(cvc_model_id):
        return (
            f"cvc_model_id must match pattern ^cvc_[a-z0-9]{{8,32}}$; "
            f"got: {cvc_model_id!r}"
        )
    return ""


# ---- Tool 1: trusted source lookup ----


@mcp.tool()
def find_trusted_sources(
    keyword: str = "",
    category: str = "",
    region: str = "",
    language: str = "",
    min_tscore: float = 0.1,
    sample_size: int = 2,
    limit: int = 20,
) -> dict:
    """Trusted-source lookup: find sources by keyword / category / region / language, filtered by tscore.

    tscore meaning:
    - >= 0.8: highly trusted (agencies, authoritative media)
    - 0.6-0.8: moderately trusted (mainstream / professional media)
    - < 0.6: low trust (self-media, personal blogs; use with caution)

    Suggested workflow: call this tool first to get a list of highly trusted
    source ids, then call search_intelligence with those source_ids to scope the search.

    Args:
        keyword: fuzzy keyword match (matches source_id / name / description)
        category: source category (wire=agency, mainstream, tech, market, ai, ...)
        region: region filter (us / europe / china / ...)
        language: language filter (en / zh / ja / ko / ...)
        min_tscore: minimum trust threshold, 0.0-1.0, default 0.1
        sample_size: number of sample intelligence items per source
        limit: max number of sources returned

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


# ---- Tool 2: intelligence retrieval ----


@mcp.tool()
def search_intelligence(
    q: str,
    source_ids: list[str] | None = None,
    must_keywords: str = "",
    should_keywords: str = "",
    not_keywords: str = "",
    min_tscore: float = 0.1,
    days: int = 30,
    start_date: str = "",
    end_date: str = "",
    category: str = "",
    region: str = "",
    language: str = "",
    limit: int = 20,
) -> dict:
    """Complex-logic intelligence retrieval: AND / OR / NOT / exact phrase / synonym groups.

    Query syntax (q parameter, takes precedence over everything):
    - ``+word``: must contain (AND logic)
    - ``word1 word2``: any match (OR logic)
    - ``-word``: exclude (NOT logic)
    - ``"exact phrase"``: exact phrase match
    - ``word1|word2``: synonym group (OR logic)
    - ``(a OR b)``: group logic

    Typical usage (two-stage search):
    1. Call find_trusted_sources to obtain trusted source ids
    2. Call this tool with source_ids=<step-1 result> to scope the search

    Args:
        q: query-syntax string, e.g. +(Tesla|BYD) +(EV|battery) -rumor
        source_ids: source id list (from find_trusted_sources)
        must_keywords: AND keywords (comma-separated), e.g. "EV,battery"
        should_keywords: OR keywords (comma-separated), e.g. "Tesla,BYD"
        not_keywords: NOT keywords to exclude (comma-separated), e.g. "rumor,speculation"
        min_tscore: minimum intelligence trust, 0.0-1.0
        days: time window (days); mutually exclusive with start_date/end_date
        start_date: exact start date YYYY-MM-DD
        end_date: exact end date YYYY-MM-DD
        category: source category filter
        region: source region filter
        language: source language filter
        limit: max number of items returned

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


# ---- Tool 3: trend analysis ----


@mcp.tool()
def analyze_trend(
    q: str,
    source_ids: list[str] | None = None,
    min_tscore: float = 0.1,
    days: int = 90,
    limit: int = 200,
) -> dict:
    """Time-series trend analysis of retrieval results, aggregated by day/week.

    Suitable for:
    - Tracking public-opinion heat around a topic over time
    - Discovering reporting cycles of a vendor/product
    - Identifying the timing of incidents or spikes

    Args:
        q: query-syntax string (as in search_intelligence)
        source_ids: source id list
        min_tscore: minimum trust
        days: analysis window (days)
        limit: max number of items retrieved

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


# ---- Tool 4: per-source aggregation analysis ----


@mcp.tool()
def aggregate_by_source(
    q: str,
    source_ids: list[str] | None = None,
    min_tscore: float = 0.1,
    days: int = 30,
    limit: int = 100,
) -> dict:
    """Aggregate retrieval results by source: volume, average tscore, sample titles.

    Suitable for:
    - Comparing how much attention different sources give a topic
    - Contrasting stance differences across sources
    - Selecting high-quality sources for ongoing tracking

    Args:
        q: query-syntax string (as in search_intelligence)
        source_ids: source id list
        min_tscore: minimum trust
        days: time window
        limit: max number of items retrieved

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


# ---- Tool 5: CVC semantic search ----


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
    min_tscore: float = 0.1,
) -> dict:
    """CVC-model semantic search: a two-stage hybrid of BM25 + vector ANN + RRF fusion.

    Suitable for:
    - Competitive technical-route comparison
    - Deep market-trend insight
    - Customer-requirement semantic analysis
    - Cross-language intelligence correlation

    Search modes:
    - ``hybrid`` (default): keyword + vector semantic + RRF fusion
    - ``semantic``: pure vector semantic (for conceptual / abstract queries)
    - ``keyword``: pure keyword (for exact-match cases)

    Args:
        cvc_model_id: CVC model id, format ``^cvc_[a-z0-9]{8,32}$`` (required)
        q: query text, supports natural language, e.g. "competitor's SEA electrification strategy"
        mode: search mode; one of hybrid / semantic / keyword
        top_k: max number of results, default 20
        time_range: preset range (1d / 7d / 1m / 3m); mutually exclusive with days
        days: custom window (days), 0 = unlimited
        category: source category filter
        region: source region filter
        language: source language filter
        min_tscore: minimum trust (0.0-1.0)

    Returns:
        dict: SerpAPI-style response including search_information.fusion statistics
            {
                search_information: { fusion: { bm25_hits, vector_hits, degraded, ... } },
                organic_results: [{title, description, source_id, ...}]
            }
    """
    err = _validate_cvc_model_id(cvc_model_id)
    if err:
        return {"error": "InvalidCvcModelId", "message": err}

    valid_modes = ("hybrid", "semantic", "keyword")
    if mode not in valid_modes:
        return {
            "error": "InvalidMode",
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


# ---- Tool 6: advanced retrieval with CVC collection ----


@mcp.tool()
def search_with_cvc(
    q: str,
    cvc_model_id: str = "",
    source_ids: list[str] | None = None,
    must_keywords: str = "",
    should_keywords: str = "",
    not_keywords: str = "",
    min_tscore: float = 0.1,
    days: int = 30,
    start_date: str = "",
    end_date: str = "",
    category: str = "",
    region: str = "",
    language: str = "",
    limit: int = 20,
) -> dict:
    """Complex-logic intelligence retrieval that collects results into a CVC model.

    Same behavior as search_intelligence, but additionally accepts a
    cvc_model_id. When provided, results are asynchronously collected into the
    model's OpenSearch / Qdrant semantic store, feeding a later semantic_search_tool.

    Typical usage (three-stage intelligent search):
    1. Call find_trusted_sources to get trusted source ids
    2. Call this tool with cvc_model_id to collect results
    3. Call semantic_search_tool for deep semantic search

    Args:
        q: query-syntax string, e.g. +(Tesla|BYD) +(EV|battery) -rumor
        cvc_model_id: CVC model id (optional), format ``^cvc_[a-z0-9]{8,32}$``;
            when set, results are collected into the semantic store
        source_ids: source id list (from find_trusted_sources)
        must_keywords: AND keywords (comma-separated), e.g. "EV,battery"
        should_keywords: OR keywords (comma-separated), e.g. "Tesla,BYD"
        not_keywords: NOT keywords to exclude (comma-separated), e.g. "rumor,speculation"
        min_tscore: minimum intelligence trust, 0.0-1.0
        days: time window (days); mutually exclusive with start_date/end_date
        start_date: exact start date YYYY-MM-DD
        end_date: exact end date YYYY-MM-DD
        category: source category filter
        region: source region filter
        language: source language filter
        limit: max number of items returned

    Returns:
        dict: {count, items: [{title, description, source_id, tscore, published_at, ...}]}
            includes a cvc_sync_status field when cvc_model_id is set
    """
    if cvc_model_id:
        err = _validate_cvc_model_id(cvc_model_id)
        if err:
            return {"error": "InvalidCvcModelId", "message": err}

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

    # Attach cvc_model_id to the request when provided
    if cvc_model_id:
        kwargs["cvc_model_id"] = cvc_model_id

    result = _sdk_call(search_advanced, **kwargs)
    if isinstance(result, dict) and "error" in result:
        return result

    # Annotate with a CVC collection status hint
    if cvc_model_id:
        result["cvc_sync_status"] = {
            "cvc_model_id": cvc_model_id,
            "items_found": len(result.get("items", [])),
            "message": f"Results collected into CVC model {cvc_model_id}; "
                       f"use semantic_search_tool for deep semantic search",
        }

    return result


# ---- Tool 7: server-side batch search ----


@mcp.tool()
def batch_search(
    groups: list | None = None,
    dedupe: bool = True,
    max_concurrency: int = 4,
) -> dict:
    """Server-side batch search: run multiple advanced queries in a single round-trip.

    The server executes the groups concurrently and deduplicates across groups,
    which is lower-latency and better suited to large batches than client-side
    orchestration.

    Each group is a dict that mirrors the advanced-search parameters, e.g.:
        {"id": "must_ai", "must_keywords": ["AI"], "should_keywords": ["Nvidia", "chip"],
         "days": 1000, "limit": 20}
        {"id": "since_inc", "q": "+AI", "days": 30, "since_ts": "2026-09-08T00:00:00Z", "limit": 10}
        {"id": "quick", "q": "smartwatch", "days": 30, "limit": 5}

    - Quick groups (bare comma-separated q, no boolean terms) behave like GET /v1/search (comma = OR);
    - Advanced groups behave like GET /v1/searchx.
    - The server never triggers Google News backfill in batch mode.
    - A single group failure is isolated via an ``error`` field and does not block others.
    - At most 50 groups per call.

    Args:
        groups: list of group dicts (``id`` required)
        dedupe: dedupe across groups by URL/title (default True)
        max_concurrency: server-side concurrency cap (1-8)

    Returns:
        dict: {"meta": {groups, returned_total, dedupe, cross_group_dropped, elapsed_ms},
               "results": [{id, count, data, ...}]}
    """
    if not groups:
        return {"error": "EmptyGroupList", "message": "groups must be a non-empty list."}
    if max_concurrency < 1 or max_concurrency > 8:
        return {
            "error": "InvalidMaxConcurrency",
            "message": f"max_concurrency must be between 1 and 8, got: {max_concurrency!r}",
        }
    return _safe_call(
        sdk_search_batch,
        groups,
        dedupe=dedupe,
        max_concurrency=max_concurrency,
    )


# ---- Tool 8: list CVC models ----


@mcp.tool()
def list_cvc_models_tool() -> dict:
    """List all CVC models with their collection statistics.

    Provides the candidate set for selecting a model for semantic search or
    for multi-select deletion.

    Returns:
        dict: {count, models: [{cvc_model_id, doc_count, query_count,
                                first_collected_at, last_collected_at, ...}]}
    """
    return _safe_call(sdk_list_cvc_models)


# ---- Tool 9: CVC collection statistics ----


@mcp.tool()
def cvc_stats_tool(cvc_model_id: str) -> dict:
    """Collection statistics for a single CVC model (docs / vectors / source & language distribution / time span).

    Args:
        cvc_model_id: CVC model id, format ``^cvc_[a-z0-9]{8,32}$``

    Returns:
        dict: collection statistics summary for the model
    """
    err = _validate_cvc_model_id(cvc_model_id)
    if err:
        return {"error": "InvalidCvcModelId", "message": err}
    return _safe_call(sdk_cvc_stats, cvc_model_id)


# ---- Tool 10: CVC cache reset ----


@mcp.tool()
def cvc_cache_purge_tool(cvc_model_id: str) -> dict:
    """Reset a CVC model's cache: clear its OpenSearch / Qdrant / Redis collection data.

    - The CVC model itself is preserved (cvc_search_logs / tenant bindings untouched);
    - MongoDB source_feed original intelligence is never cleared;
    - Non-empty collections are cleared asynchronously (202, returns task_id);
      poll cvc_cleanup_status_tool for progress and three-store reconciliation;
    - A completed/empty model returns status="completed" idempotently.

    To recover afterward, call cvc_reindex_tool to replay the collection audit.

    Args:
        cvc_model_id: CVC model id, format ``^cvc_[a-z0-9]{8,32}$``

    Returns:
        dict: purge task / status response (may include async task_id)
    """
    err = _validate_cvc_model_id(cvc_model_id)
    if err:
        return {"error": "InvalidCvcModelId", "message": err}
    return _safe_call(sdk_cvc_cache_purge, cvc_model_id)


# ---- Tool 11: CVC cleanup progress ----


@mcp.tool()
def cvc_cleanup_status_tool(cvc_model_id: str) -> dict:
    """Cleanup progress and three-store reconciliation for a CVC model.

    Reports the progress of the most recent cleanup task (cache/purge reset or
    model deletion) and live residual counts across OpenSearch / Qdrant / Redis docids.

    Args:
        cvc_model_id: CVC model id, format ``^cvc_[a-z0-9]{8,32}$``

    Returns:
        dict: cleanup progress + per-store residual counts
    """
    err = _validate_cvc_model_id(cvc_model_id)
    if err:
        return {"error": "InvalidCvcModelId", "message": err}
    return _safe_call(sdk_cvc_cleanup_status, cvc_model_id)


# ---- Tool 12: CVC reindex - rebuild collection store ----


@mcp.tool()
def cvc_reindex_tool(cvc_model_id: str) -> dict:
    """Rebuild a CVC collection store by replaying the collection audit.

    Reads all collection-audit records (cvc_search_logs) for the model and
    re-collects the referenced docs from MongoDB into OpenSearch + Qdrant.

    - Typically used to recover a store after cvc_cache_purge_tool (cache reset);
    - After a model deletion the logs are gone, so rebuild equals collecting from scratch.

    Args:
        cvc_model_id: CVC model id, format ``^cvc_[a-z0-9]{8,32}$``

    Returns:
        dict: reindex result summary
    """
    err = _validate_cvc_model_id(cvc_model_id)
    if err:
        return {"error": "InvalidCvcModelId", "message": err}
    return _safe_call(sdk_cvc_reindex, cvc_model_id)


# ---- Tool 13: full-history intelligence purge ----


@mcp.tool()
def intelligence_purge_tool(
    older_than_days: int | None = None,
    end_date: str = "",
    cvc_model_ids: list | None = None,
    include_mongodb: bool = True,
    dry_run: bool = False,
) -> dict:
    """Full-history intelligence purge across all stores by time cutoff.

    Clears old history from MongoDB source_feed (optional) → OpenSearch →
    Qdrant → Redis invalidation. This is the only entry point allowed to delete
    intelligence bodies (MongoDB primary records).

    - ``older_than_days`` / ``end_date``: provide exactly one (cutoff time lower bound);
    - ``cvc_model_ids``: limit the collection-store scope (default: all);
    - ``include_mongodb``: whether to purge source_feed original intelligence (default True);
    - ``dry_run``: only estimate per-store counts (synchronous 200, no deletion).

    Warning: an actual purge deletes intelligence data; use with caution.

    Args:
        older_than_days: purge items older than this many days
        end_date: purge items published strictly before this cutoff date
        cvc_model_ids: optional list of CVC model ids to scope the collection stores
        include_mongodb: whether to clear source_feed original intelligence
        dry_run: estimate only (no deletion side effects)

    Returns:
        dict: dry_run → per-store estimated counts; otherwise an async task with task_id
    """
    if older_than_days is None and not end_date:
        return {
            "error": "MissingCutoff",
            "message": "Provide one of older_than_days or end_date as the cutoff criterion.",
        }
    return _safe_call(
        sdk_intelligence_purge,
        older_than_days=older_than_days,
        end_date=end_date or None,
        cvc_model_ids=cvc_model_ids,
        include_mongodb=include_mongodb,
        dry_run=dry_run,
    )


# ---- Tool 14: full-history purge progress ----


@mcp.tool()
def intelligence_purge_status_tool(task_id: str = "") -> dict:
    """Full-history purge progress and residual reconciliation.

    Reports the task state machine (pending → estimating → mongo_purging →
    os_purging → qd_purging → redis_invalidating → verifying → completed/failed)
    plus live per-store count(published_at < cutoff) residuals.

    Args:
        task_id: task id (default: most recent task)

    Returns:
        dict: task progress + per-store residual counts
    """
    return _safe_call(sdk_intelligence_purge_status, task_id or None)


def main():
    """MCP server entry point."""
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