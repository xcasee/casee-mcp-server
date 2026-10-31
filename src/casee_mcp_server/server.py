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

Report tools (v1.4, OpenAPI /api/v1/openapi):
- list_report_templates     List system + tenant report templates
- plan_report               Pre-flight: sections / charts / gaps / compatibility
- generate_report           Create an async report job (idempotency supported)
- get_report_job            Poll job status, quality and artifacts (long-poll)
- list_report_jobs          Paginated job history with status filter
- share_report              Create a password-free rs- share snapshot
- backup_cvc_intelligence   Export metadata (count / ETag) before a rebuild
- rebuild_cvc_model         Dry-run or destructive rebuild (confirm required)
- casee_health              Gateway / backend version and API base

MCP prompts (v1.4):
- report_generation         list → plan → generate → poll → share → declare quality
- retrieval_quality_guard   Degraded-semantic / zero-result disclosure protocol

Startup:
    # stdio mode (recommended for WorkBuddy / Claude Desktop)
    CASEE_API_KEY=sk_xxx python -m casee_mcp_server

    # Streamable-HTTP mode (recommended for Trae Work / WorkBuddy / remote agents)
    CASEE_API_KEY=sk_xxx MCP_TRANSPORT=streamable-http python -m casee_mcp_server

    # Opt in to single-JSON responses (avoids SSE parsing issues)
    MCP_JSON_RESPONSE=true
"""

import ast
import os
import re
import time
from collections import Counter, defaultdict
from typing import Annotated

from pydantic import Field

from mcp.server import MCPServer
from casee import (
    search_sources,
    search_advanced,
    semantic_search,
    search_batch as sdk_search_batch,
    cvc_stats as sdk_cvc_stats,
    cvc_cache_purge as sdk_cvc_cache_purge,
    cvc_cleanup_status as sdk_cvc_cleanup_status,
    cvc_reindex as sdk_cvc_reindex,
    intelligence_purge as sdk_intelligence_purge,
    intelligence_purge_status as sdk_intelligence_purge_status,
)
from casee.exceptions import (
    APIError,
    AuthenticationError,
    CaseeError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ServerError,
    TimeoutError as CaseeTimeoutError,
    ValidationError,
)

from .reports import get_report_client


mcp = MCPServer(
    name="casee",
    version="1.4.0",
    description="CaSee enterprise competitive-intelligence retrieval and report generation - "
                "trusted-source lookup, complex retrieval, trend analysis, semantic search, "
                "CVC administration, report templates / pre-flight / generation / sharing",
)

# Release version kept here (not in __init__) to avoid a circular import:
# __init__.py re-exports this value.
__version__ = "1.4.0"


# ---- CVC model id validation (pattern ^cvc_[a-z0-9]{8,32}$) ----

_CVC_MODEL_ID_RE = re.compile(r"^cvc_[a-z0-9]{8,32}$")

# Annotated type: surfaces the pattern in the tool JSON schema so MCP
# clients can validate cvc_model_id before ever calling the tool.
CvcModelId = Annotated[
    str,
    Field(pattern=r"^cvc_[a-z0-9]{8,32}$",
          description="CVC model id, format ^cvc_[a-z0-9]{8,32}$"),
]


_CONTACT_DEFAULTS = {
    "contact_email": "info@xcasee.com",
    "contact_wechat": "@casee_me",
    "contact_url": "/billing/service-contact",
}

# Stable machine-readable error contract. ``error`` keeps the legacy SDK
# exception class name; ``error_code`` is the field agents should branch on.
_ERROR_POLICY = {
    "CREDITS_INSUFFICIENT": {
        "hint": "Insufficient Credits. Top up or contact CaSee support, then retry.",
        "retryable": False,
        "action": "contact_support",
    },
    "PRODUCT_ACCESS_DENIED": {
        "hint": "The API key is valid, but the tenant has not subscribed to the required "
                "product module. Ask an administrator to enable it and retry.",
        "retryable": False,
        "action": "contact_admin",
    },
    "SCOPE_DENIED": {
        "hint": "The API key lacks the required scope. Create a key with the needed scope "
                "and retry.",
        "retryable": False,
        "action": "reauthenticate",
    },
    "TENANT_DISABLED": {
        "hint": "The tenant is disabled. Contact an administrator to restore it.",
        "retryable": False,
        "action": "contact_admin",
    },
    "AUTHENTICATION_FAILED": {
        "hint": "Invalid or missing API key; check the CASEE_API_KEY environment variable.",
        "retryable": False,
        "action": "reauthenticate",
    },
    "RATE_LIMITED": {
        "hint": "Rate limit or quota exceeded; wait for retry_after and retry.",
        "retryable": True,
        "action": "retry_later",
    },
    "TIMEOUT": {
        "hint": "Request timed out; retry later or increase CASEE_TIMEOUT.",
        "retryable": True,
        "action": "retry_later",
    },
    "NETWORK_ERROR": {
        "hint": "Network error reaching the CaSee API; check CASEE_API_BASE_URL and connectivity.",
        "retryable": True,
        "action": "retry_later",
    },
    "SERVER_ERROR": {
        "hint": "The CaSee API returned a server error; retry later or contact support.",
        "retryable": True,
        "action": "retry_later",
    },
    "VALIDATION_ERROR": {
        "hint": "Request validation failed; fix the parameters shown in details and retry.",
        "retryable": False,
        "action": "fix_request",
    },
    "NOT_FOUND": {
        "hint": "The requested resource was not found; verify cvc_model_id or other identifiers.",
        "retryable": False,
        "action": "fix_request",
    },
    "API_ERROR": {
        "hint": "The CaSee API returned an error; inspect message/details.",
        "retryable": False,
        "action": "inspect_error",
    },
    "UNEXPECTED_ERROR": {
        "hint": "Unexpected client-side error; inspect the message and retry or report it.",
        "retryable": False,
        "action": "report_bug",
    },
}

_PRODUCT_NAME_RE = re.compile(r"「([^」]{1,60})」")


def _extract_error_context(exc: Exception) -> tuple[str, int | None, int | None, dict]:
    """Return (message, status_code, upstream_code, details) from an SDK exception.

    The SDK wraps 402 into ``APIError`` (body preserved), but maps 401/403 to
    ``AuthenticationError`` without the response body. For the latter we parse
    the human-readable message and keep the legacy dict-repr fallback.
    """
    message = str(exc)
    status_code = getattr(exc, "status_code", None)
    upstream_code = None
    details: dict = {}

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        message = str(body.get("message") or message)
        upstream_code = body.get("code")
        nested = body.get("details")
        if isinstance(nested, dict):
            details.update(nested)
        elif isinstance(body.get("detail"), dict):
            details.update(body["detail"])
        if isinstance(body.get("error"), str):
            details.setdefault("error", body["error"])
        # REST v1.1 error bodies carry error_code / request_id; keep them so
        # agents can branch on the same machine-readable code as MCP errors.
        if isinstance(body.get("error_code"), str):
            details.setdefault("error", body["error_code"])
        if body.get("request_id"):
            details.setdefault("request_id", str(body["request_id"]))

    raw_detail = getattr(exc, "detail", None)
    if isinstance(raw_detail, dict):
        details.update(raw_detail)
    elif raw_detail:
        details.setdefault(
            "validation",
            raw_detail if isinstance(raw_detail, (list, str, int, float, bool))
            else str(raw_detail),
        )

    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        details.setdefault("retry_after", retry_after)

    # AuthenticationError may carry a dict repr (older SDK / detail-dict APIs).
    if not details and message.lstrip().startswith("{"):
        try:
            parsed = ast.literal_eval(message)
        except (ValueError, SyntaxError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            details.update(parsed)
            message = str(parsed.get("message") or message)
            upstream_code = parsed.get("code", upstream_code)

    return message, status_code, upstream_code, details


def _classify_error(
    exc: Exception, message: str, status_code: int | None, details: dict
) -> str:
    """Map an exception to a stable error_code, with product/quota priority."""
    marker = f"{details.get('error') or ''} {message}".upper()
    message_lower = message.lower()

    if isinstance(exc, AuthenticationError):
        if "TENANT_DISABLED" in marker or "租户账号已被禁用" in message or "tenant is disabled" in message_lower:
            return "TENANT_DISABLED"
        if (
            "MODULE_ACCESS_DENIED" in marker
            or "PRODUCT_ACCESS_DENIED" in marker
            or "未开通" in message
            or "未授权访问该产品模块" in message
            or "not subscribed" in message_lower
            or "product module" in message_lower
        ):
            return "PRODUCT_ACCESS_DENIED"
        if (
            "SCOPE_DENIED" in marker
            or "API Key 缺少权限" in message
            or "缺少权限" in message
            or "missing scope" in message_lower
            or "lacks the required scope" in message_lower
        ):
            return "SCOPE_DENIED"
        return "AUTHENTICATION_FAILED"

    if (
        "CREDITS_INSUFFICIENT" in marker
        or "点数不足" in message
        or "CREDITS 点数不足" in marker
        or "insufficient credits" in message_lower
    ):
        return "CREDITS_INSUFFICIENT"
    if "TENANT_DISABLED" in marker or "租户账号已被禁用" in message or "tenant is disabled" in message_lower:
        return "TENANT_DISABLED"
    if (
        "MODULE_ACCESS_DENIED" in marker
        or "PRODUCT_ACCESS_DENIED" in marker
        or "未开通" in message
        or "未授权访问该产品模块" in message
        or "not subscribed" in message_lower
        or "product module" in message_lower
    ):
        return "PRODUCT_ACCESS_DENIED"
    if (
        "SCOPE_DENIED" in marker
        or "API Key 缺少权限" in message
        or "missing scope" in message_lower
    ):
        return "SCOPE_DENIED"
    if isinstance(exc, RateLimitError) or status_code == 429:
        return "RATE_LIMITED"
    if status_code == 401:
        return "AUTHENTICATION_FAILED"
    if isinstance(exc, ValidationError) or status_code == 422:
        return "VALIDATION_ERROR"
    if isinstance(exc, NotFoundError) or status_code == 404:
        return "NOT_FOUND"
    if isinstance(exc, CaseeTimeoutError) or "timed out" in message.lower():
        return "TIMEOUT"
    if isinstance(exc, NetworkError):
        return "NETWORK_ERROR"
    if isinstance(exc, ServerError) or (status_code is not None and status_code >= 500):
        return "SERVER_ERROR"
    if isinstance(exc, APIError) or status_code is not None or isinstance(exc, CaseeError):
        return "API_ERROR"
    return "UNEXPECTED_ERROR"


def _error_payload(exc: Exception) -> dict:
    """Build the structured MCP error envelope (never raises)."""
    message, status_code, upstream_code, details = _extract_error_context(exc)
    if not isinstance(exc, CaseeError):
        message = f"{type(exc).__name__}: {exc}"
    code = _classify_error(exc, message, status_code, details)

    if status_code is None:
        status_code = {
            "PRODUCT_ACCESS_DENIED": 403,
            "SCOPE_DENIED": 403,
            "TENANT_DISABLED": 403,
            "AUTHENTICATION_FAILED": 401,
            "CREDITS_INSUFFICIENT": 402,
            "RATE_LIMITED": 429,
            "VALIDATION_ERROR": 422,
            "NOT_FOUND": 404,
        }.get(code)

    product = ""
    for key in ("product_name", "module_name", "product", "module_id"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            product = value.strip()
            break
    if not product:
        match = _PRODUCT_NAME_RE.search(message)
        if match:
            product = match.group(1).strip()

    policy = _ERROR_POLICY.get(code, _ERROR_POLICY["API_ERROR"])
    hint = policy["hint"]

    if code == "CREDITS_INSUFFICIENT":
        required = details.get("required_credits")
        current = details.get("current_credits")
        if required is not None or current is not None:
            hint = (
                f"Insufficient Credits (required={required}, current={current}); "
                "top up or contact CaSee support, then retry."
            )
        for key, value in _CONTACT_DEFAULTS.items():
            details.setdefault(key, value)
    elif code == "PRODUCT_ACCESS_DENIED":
        if product:
            details.setdefault("product", product)
            hint = (
                f'The API key is valid, but the tenant has not subscribed to '
                f'the required product module "{product}". '
                "Ask an administrator to enable it and retry."
            )
        for key, value in _CONTACT_DEFAULTS.items():
            details.setdefault(key, value)
    elif code == "TENANT_DISABLED":
        for key, value in _CONTACT_DEFAULTS.items():
            details.setdefault(key, value)
    elif code in ("API_ERROR", "UNEXPECTED_ERROR"):
        hint = _diagnose_sdk_error(exc) or hint

    return {
        "error": exc.__class__.__name__,
        "error_code": code,
        "message": message,
        "status_code": status_code,
        "code": upstream_code,
        "details": details,
        "hint": hint,
        "retryable": policy["retryable"],
        "action": policy["action"],
        "request_id": str(details.get("request_id") or ""),
    }


def _safe_call(fn, *args, **kwargs):
    """Invoke an SDK call, converting any exception into a structured error dict.

    Returns the raw SDK result on success; on failure returns an error envelope
    with a stable ``error_code`` plus ``details`` / ``hint`` / ``retryable`` /
    ``action`` so agents can distinguish insufficient Credits from a missing
    product subscription or an invalid API key.
    """
    try:
        return fn(*args, **kwargs)
    except CaseeError as exc:
        return _error_payload(exc)
    except Exception as exc:  # pragma: no cover - defensive
        payload = _error_payload(exc)
        payload["error"] = "UnexpectedError"
        payload["error_code"] = "UNEXPECTED_ERROR"
        return payload


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
        dict: SerpAPI-style advanced search response from the CaSee API,
            {search_metadata: {..., status}, organic_results: [{title,
            description, source_id, tscore, published_at, ...}], ...}
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
    cvc_model_id: CvcModelId,
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
        dict: SerpAPI-style advanced search response (as search_intelligence),
            plus a cvc_sync_status field when cvc_model_id is set
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
        items = (result.get("organic_results")
                 or result.get("items") or [])
        result["cvc_sync_status"] = {
            "cvc_model_id": cvc_model_id,
            "items_found": len(items),
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
def list_cvc_models_tool(
    status: str = "completed",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    sort: str = "-updated_at",
) -> dict:
    """List CVC models with status, model version, entity/intelligence counts and pagination.

    Backed by the OpenAPI ``GET /api/v1/openapi/cvc-models`` endpoint, which
    returns richer summaries than the legacy SDK route.

    Args:
        status: completed | draft | building | failed | all (default completed)
        keyword: fuzzy match on model name / description
        page: page number, 1-based
        page_size: page size, 1-100
        sort: name | -name | updated_at | -updated_at

    Returns:
        dict: {ok, data: {items: [{cvc_id, name, status, model_version,
              company_count, entity_counts, intelligence_count, graph, ...}],
              pagination: {page, page_size, total, total_pages, has_more}},
              meta: {elapsed_ms, request_id?}}
    """
    if status not in ("completed", "draft", "building", "failed", "all"):
        return _report_invalid(
            "status must be one of completed|draft|building|failed|all",
            {"status": status},
        )
    if page < 1 or not 1 <= page_size <= 100:
        return _report_invalid(
            "page must be >= 1 and page_size between 1 and 100",
            {"page": page, "page_size": page_size},
        )
    if sort not in ("name", "-name", "updated_at", "-updated_at"):
        return _report_invalid(
            "sort must be one of name|-name|updated_at|-updated_at",
            {"sort": sort},
        )
    client = get_report_client()
    return _report_call(
        lambda: client.request("GET", "/cvc-models", params={
            "status": status, "keyword": keyword, "page": page,
            "page_size": page_size, "sort": sort,
        }),
        client,
    )


# ---- Tool 9: CVC collection statistics ----


@mcp.tool()
def cvc_stats_tool(cvc_model_id: CvcModelId) -> dict:
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
def cvc_cache_purge_tool(cvc_model_id: CvcModelId) -> dict:
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
def cvc_cleanup_status_tool(cvc_model_id: CvcModelId) -> dict:
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
def cvc_reindex_tool(cvc_model_id: CvcModelId) -> dict:
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


# ---- Report tools (v1.4) ----
#
# These tools talk to the OpenAPI report surface through :mod:`reports`.
# Successful calls return ``{ok: true, data, meta}`` so agents get a uniform
# envelope, while failures keep the legacy structured error fields plus ``ok``.
# All list tools return ``items + pagination``.

_REPORT_OUTPUT_FORMATS = ("html", "md", "deck")
_REPORT_TIME_RANGES = ("week", "month", "quarter", "auto")


def _env_flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _report_meta(client, started: float) -> dict:
    meta = {"elapsed_ms": int((time.monotonic() - started) * 1000)}
    request_id = getattr(client, "last_request_id", "")
    if request_id:
        meta["request_id"] = request_id
    return meta


def _report_call(fn, client=None, *, next_poll_after_ms: int | None = None) -> dict:
    """Run a report client call and wrap the result in the v1.4 envelope."""
    client = client or get_report_client()
    started = time.monotonic()
    try:
        data = fn()
    except Exception as exc:  # noqa: BLE001 - the tool layer must never raise
        payload = _error_payload(exc)
        payload["ok"] = False
        payload["meta"] = _report_meta(client, started)
        return payload
    meta = _report_meta(client, started)
    if next_poll_after_ms is not None:
        meta["next_poll_after_ms"] = int(next_poll_after_ms)
    return {"ok": True, "data": data, "meta": meta}


def _report_invalid(message: str, details: dict | None = None) -> dict:
    """Local argument validation error in the structured error shape."""
    return {
        "ok": False,
        "error": "InvalidArgument",
        "error_code": "VALIDATION_ERROR",
        "message": message,
        "status_code": 422,
        "details": details or {},
        "hint": "Fix the parameters shown in details and retry.",
        "retryable": False,
        "action": "fix_request",
        "request_id": "",
        "meta": {"elapsed_ms": 0},
    }


def _absolutize_job(job: dict, client) -> dict:
    """Turn backend-relative artifact/share URLs into absolute portal URLs."""
    if not isinstance(job, dict):
        return job
    for artifact in job.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("url"):
            artifact["url"] = client.absolute_url(artifact["url"])
    share = job.get("share")
    if isinstance(share, dict) and share.get("url"):
        share["url"] = client.absolute_url(share["url"])
    return job


def _condense_plan(plan: dict) -> dict:
    """Keep the decision-relevant parts of a plan; drop per-chart share codes."""
    sections = []
    for section in plan.get("sections") or []:
        charts = [
            {
                "ca_code": chart.get("ca_code"),
                "name_zh": chart.get("name_zh"),
                "available": bool(chart.get("available")),
                "reason": chart.get("reason"),
            }
            for chart in (section.get("charts") or [])
        ]
        sections.append({
            "id": section.get("id"),
            "title_zh": section.get("title_zh"),
            "title_en": section.get("title_en"),
            "render": section.get("render"),
            "available_chart_count": section.get("available_chart_count"),
            "charts": charts,
        })
    return {
        "plan_id": plan.get("plan_id"),
        "template": plan.get("template"),
        "cvc": plan.get("cvc"),
        "time_range": plan.get("time_range"),
        "requested_time_range": plan.get("requested_time_range"),
        "time_range_resolution": plan.get("time_range_resolution"),
        "charts": plan.get("charts"),
        "compatibility": plan.get("compatibility"),
        "quality": plan.get("quality"),
        "recommended_time_range": plan.get("recommended_time_range"),
        "estimated_credits": plan.get("estimated_credits"),
        "estimated_seconds": plan.get("estimated_seconds"),
        "sections": sections,
        "composite": plan.get("composite"),
        "data_gaps": plan.get("data_gaps") or [],
        "report_brief": plan.get("report_brief"),
    }


@mcp.tool()
def list_report_templates(
    scope: str = "all",
    q: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """List report templates available to the current tenant.

    Call this first when you need a ``template_id`` for ``plan_report``.

    Args:
        scope: system (built-in) | tenant (custom) | all
        q: keyword filter on template id / name / summary
        page: page number, 1-based
        page_size: page size, 1-200

    Returns:
        dict: {ok, data: {items: [{template_id, name_zh, name_en, version,
              summary_zh, product, product_access, ...}],
              pagination: {...}}, meta}
    """
    if scope not in ("system", "tenant", "all"):
        return _report_invalid("scope must be one of system|tenant|all", {"scope": scope})
    if page < 1 or not 1 <= page_size <= 200:
        return _report_invalid(
            "page must be >= 1 and page_size between 1 and 200",
            {"page": page, "page_size": page_size},
        )
    client = get_report_client()

    def call():
        data = client.request("GET", "/reports/templates", params={"scope": scope, "q": q})
        items = list((data or {}).get("templates") or [])
        total = len(items)
        start = (page - 1) * page_size
        return {
            "items": items[start:start + page_size],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size if page_size else 0,
                "has_more": page * page_size < total,
            },
        }

    return _report_call(call, client)


@mcp.tool()
def plan_report(
    cvc_id: str,
    template_id: str,
    time_range: str = "auto",
    language: str = "zh",
    include_unavailable: bool = False,
) -> dict:
    """Pre-flight a report: sections, chart availability, data gaps, compatibility.

    Always run this before ``generate_report``. Do not generate when
    ``compatibility.compatible`` is false, or when ``charts.available`` is 0.

    Args:
        cvc_id: CVC model id (e.g. cvc_xxx) belonging to the current tenant
        template_id: template id from list_report_templates
        time_range: week | month | quarter | auto (auto resolves by corpus density)
        language: zh | en
        include_unavailable: keep sections that have no data (marked as gaps)

    Returns:
        dict: {ok, data: {plan_id, template, cvc, time_range, charts,
              compatibility, quality, recommended_time_range,
              estimated_credits, estimated_seconds, sections, composite,
              data_gaps, report_brief}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if not template_id:
        return _report_invalid(
            "template_id is required; call list_report_templates first",
            {"template_id": template_id},
        )
    if time_range not in _REPORT_TIME_RANGES:
        return _report_invalid(
            f"time_range must be one of {_REPORT_TIME_RANGES}", {"time_range": time_range},
        )
    if language not in ("zh", "en"):
        return _report_invalid("language must be zh or en", {"language": language})
    client = get_report_client()

    def call():
        data = client.request("POST", "/reports/plan", json_body={
            "cvc_id": cvc_id,
            "template_id": template_id,
            "time_range": time_range,
            "language": language,
            "include_unavailable": include_unavailable,
        })
        return _condense_plan(data or {})

    return _report_call(call, client)


@mcp.tool()
def generate_report(
    cvc_id: str,
    template_id: str,
    time_range: str = "auto",
    output_formats: list[str] | None = None,
    language: str = "zh",
    share: bool = False,
    ttl_seconds: int = 604800,
    idempotency_key: str = "",
) -> dict:
    """Create an asynchronous report job (costs 1 Credit; poll get_report_job).

    Run ``plan_report`` first. Retries with the same ``idempotency_key``
    return the first response for 24 hours and never double-charge.

    Args:
        cvc_id: CVC model id
        template_id: template id from list_report_templates
        time_range: week | month | quarter | auto
        output_formats: subset of ["html", "md", "deck"]; deck is the PPT edition
        language: zh | en
        share: also create a password-free share link when the job finishes
        ttl_seconds: share link TTL when share=true (300 - 2592000)
        idempotency_key: optional client-generated key (8-128 chars)

    Returns:
        dict: {ok, data: {job_id, status, estimated_seconds, credits_required,
              poll_url, time_range, time_range_resolution, ...},
              meta: {next_poll_after_ms, request_id?}}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if not template_id:
        return _report_invalid("template_id is required", {"template_id": template_id})
    if time_range not in _REPORT_TIME_RANGES:
        return _report_invalid(
            f"time_range must be one of {_REPORT_TIME_RANGES}", {"time_range": time_range},
        )
    formats = list(output_formats or ["html"])
    invalid_formats = [f for f in formats if f not in _REPORT_OUTPUT_FORMATS]
    if invalid_formats:
        return _report_invalid(
            f"output_formats must be a subset of {_REPORT_OUTPUT_FORMATS}",
            {"invalid": invalid_formats},
        )
    if language not in ("zh", "en"):
        return _report_invalid("language must be zh or en", {"language": language})
    if idempotency_key and not 8 <= len(idempotency_key) <= 128:
        return _report_invalid(
            "idempotency_key must be 8-128 characters",
            {"length": len(idempotency_key)},
        )
    client = get_report_client()

    def call():
        return client.request(
            "POST", "/reports/generate",
            json_body={
                "cvc_id": cvc_id,
                "template_id": template_id,
                "time_range": time_range,
                "output_formats": formats,
                "language": language,
                "share": bool(share),
                "ttl_seconds": int(ttl_seconds),
            },
            extra_headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )

    return _report_call(call, client, next_poll_after_ms=5000)


@mcp.tool()
def get_report_job(job_id: str, wait_ms: int = 0) -> dict:
    """Poll a report job: status, progress, quality, artifacts and share link.

    Use ``meta.next_poll_after_ms`` as the delay between polls. ``wait_ms``
    enables long-polling (0-30000) to block until a terminal status or timeout.

    Args:
        job_id: job id returned by generate_report
        wait_ms: long-poll wait in milliseconds (server caps at 30000)

    Returns:
        dict: {ok, data: {job_id, status, progress, stage, quality, charts,
              artifacts: [{format, ready, bytes, url}], share, data_gaps,
              error_info, next_poll_after_ms}, meta}
    """
    if not job_id:
        return _report_invalid("job_id is required", {"job_id": job_id})
    if wait_ms < 0 or wait_ms > 30000:
        return _report_invalid("wait_ms must be between 0 and 30000", {"wait_ms": wait_ms})
    client = get_report_client()

    def call():
        data = client.request(
            "GET", f"/reports/jobs/{job_id}", params={"wait_ms": wait_ms or None},
        )
        return _absolutize_job(data or {}, client)

    return _report_call(call, client)


@mcp.tool()
def list_report_jobs(
    cvc_id: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """List recent report jobs for the current tenant (newest first).

    Args:
        cvc_id: filter by CVC model
        status: queued | running | succeeded | partial | failed
        page: page number, 1-based
        page_size: page size, 1-100

    Returns:
        dict: {ok, data: {items, pagination}, meta}
    """
    if status and status not in ("queued", "running", "succeeded", "partial", "failed"):
        return _report_invalid(
            "status must be one of queued|running|succeeded|partial|failed",
            {"status": status},
        )
    if page < 1 or not 1 <= page_size <= 100:
        return _report_invalid(
            "page must be >= 1 and page_size between 1 and 100",
            {"page": page, "page_size": page_size},
        )
    client = get_report_client()
    return _report_call(
        lambda: client.request("GET", "/reports/jobs", params={
            "cvc_id": cvc_id, "status": status, "page": page, "page_size": page_size,
        }),
        client,
    )


@mcp.tool()
def share_report(
    job_id: str,
    ttl_seconds: int = 604800,
    idempotency_key: str = "",
) -> dict:
    """Create a password-free share snapshot for a finished report.

    Only works when the job status is succeeded or partial. The returned
    ``url`` is the human-facing page; ``pdf_url`` exists for deck jobs.

    Args:
        job_id: finished report job id
        ttl_seconds: link TTL in seconds (300 - 2592000, default 7 days)
        idempotency_key: optional client-generated key (8-128 chars)

    Returns:
        dict: {ok, data: {code, url, api_url, pdf_url?, expires_at}, meta}
    """
    if not job_id:
        return _report_invalid("job_id is required", {"job_id": job_id})
    if not 300 <= ttl_seconds <= 30 * 24 * 3600:
        return _report_invalid(
            "ttl_seconds must be between 300 and 2592000",
            {"ttl_seconds": ttl_seconds},
        )
    if idempotency_key and not 8 <= len(idempotency_key) <= 128:
        return _report_invalid(
            "idempotency_key must be 8-128 characters",
            {"length": len(idempotency_key)},
        )
    client = get_report_client()

    def call():
        data = client.request(
            "POST", f"/reports/jobs/{job_id}/share",
            json_body={"ttl_seconds": int(ttl_seconds)},
            extra_headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        for key in ("url", "api_url", "pdf_url"):
            if isinstance(data, dict) and data.get(key):
                data[key] = client.absolute_url(data[key])
        return data

    return _report_call(call, client)


@mcp.tool()
def backup_cvc_intelligence(cvc_id: str) -> dict:
    """Check the intelligence backup source for a CVC model before rebuilding.

    Streams the export endpoint and returns metadata only (row count, ETag,
    byte size); it does not download intelligence bodies into the model
    context. An actual server-side backup file is written by
    ``rebuild_cvc_model(confirm=true, backup=true)``.

    Args:
        cvc_id: CVC model id

    Returns:
        dict: {ok, data: {cvc_id, format, count, etag, bytes, export_path}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    client = get_report_client()
    return _report_call(lambda: client.export_summary(cvc_id), client)


@mcp.tool()
def rebuild_cvc_model(
    cvc_id: str,
    confirm: bool = False,
    backup: bool = True,
    dry_run: bool = True,
    mode: str = "full",
    idempotency_key: str = "",
) -> dict:
    """Destructively rebuild a CVC model's intelligence corpus (use with care).

    Without ``confirm=true`` this only calls the backend dry-run and returns
    ``will_delete`` counts; nothing is deleted. Set ``confirm=true`` and
    ``dry_run=false`` to execute. ``backup=true`` writes a JSONL backup on the
    server first. Prefer ``non_destructive`` only when the backend supports it;
    the current backend falls back to ``full + backup`` and returns a warning.

    Args:
        cvc_id: CVC model id
        confirm: must be true to execute a destructive rebuild
        backup: write a server-side JSONL backup before deleting
        dry_run: estimate only (default true); no deletion happens
        mode: full | non_destructive
        idempotency_key: optional client-generated key (8-128 chars)

    Returns:
        dict: {ok, data: {task_id?, status?, will_delete?, safety?, poll_url?},
              meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if mode not in ("full", "non_destructive"):
        return _report_invalid("mode must be full or non_destructive", {"mode": mode})
    if idempotency_key and not 8 <= len(idempotency_key) <= 128:
        return _report_invalid(
            "idempotency_key must be 8-128 characters",
            {"length": len(idempotency_key)},
        )
    client = get_report_client()
    execute = bool(confirm and not dry_run)

    def call():
        data = client.request(
            "POST", f"/cvc-models/{cvc_id}/rebuild",
            json_body={
                "confirm": bool(confirm),
                "dry_run": not execute,
                "backup": bool(backup),
                "mode": mode,
            },
            extra_headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        if not execute and isinstance(data, dict):
            data["requires_confirm"] = True
            data["executed"] = False
        elif isinstance(data, dict):
            data["executed"] = True
        return data

    return _report_call(call, client)


@mcp.tool()
def casee_health() -> dict:
    """Check connectivity, Credits balance and version compatibility.

    Returns the MCP gateway version, backend OpenAPI version and effective API
    base. Use it when diagnosing 401/403/404 or transport problems.

    Returns:
        dict: {ok, data: {gateway_version, backend_version, api_base,
              portal_base, transport, json_response, credits}, meta}
    """
    client = get_report_client()

    def call():
        credits = client.request("GET", "/credits")
        backend_version = ""
        try:
            spec = client.request("GET", "/spec")
            info = (spec or {}).get("info") if isinstance(spec, dict) else {}
            backend_version = str((info or {}).get("version") or "")
        except Exception:  # noqa: BLE001 - version is best-effort
            backend_version = ""
        return {
            "gateway_version": __version__,
            "backend_version": backend_version,
            "api_base": client.base_url,
            "portal_base": client.portal_base_url,
            "transport": os.environ.get("MCP_TRANSPORT", "stdio"),
            "json_response": _env_flag("MCP_JSON_RESPONSE", False),
            "credits": credits,
        }

    return _report_call(call, client)


# ---- MCP prompts / Skill recipes (v1.4) ----


@mcp.prompt(
    name="report_generation",
    description="CaSee report workflow: templates → plan → generate → poll → share → declare quality.",
)
def report_generation_prompt(
    cvc_id: str = "",
    template_id: str = "",
    time_range: str = "auto",
) -> str:
    """Reusable report-generation recipe exposed via prompts/list."""
    target = cvc_id or "<cvc_id>"
    template = template_id or "<template_id>"
    return f"""Use the CaSee MCP tools to produce a report for CVC `{target}`.

Follow this order and stop on the first hard failure:

1. list_report_templates — pick a template ({template} if provided).
2. plan_report(cvc_id="{target}", template_id="{template}", time_range="{time_range}").
   - If compatibility.compatible is false: STOP, report the reasons
     (MODEL_NOT_COMPLETED / NO_AVAILABLE_CHARTS) and ask whether to rebuild.
   - If charts.available is 0: STOP and report data_gaps.
   - If recommended_time_range differs: rerun plan with it before generating.
3. generate_report(..., output_formats=["html"]) with a unique idempotency_key.
4. Poll get_report_job(job_id, wait_ms=30000) until status is terminal;
   wait meta.next_poll_after_ms between calls (about 10 minutes total).
5. If status is partial: generate anyway but list the missing sections.
   If failed: report error_code, hint, retryable and request_id; retry only
   when retryable is true.
6. share_report(job_id) and give the user the absolute url (and pdf_url).
7. Declare quality using data.quality: charts_rendered/charts_total,
   gaps_ratio, intelligence_count, model_version, template_version.
   Never claim sections that are in data_gaps.

Never invent numbers: report content must come from the generated artifacts.
"""


@mcp.prompt(
    name="retrieval_quality_guard",
    description="Disclosure protocol when semantic search degrades or returns zero results.",
)
def retrieval_quality_guard_prompt() -> str:
    """Reusable retrieval-quality recipe exposed via prompts/list."""
    return """Apply this guard whenever you use CaSee retrieval tools:

1. semantic_search_tool degraded: if search_information.degraded is non-empty
   or fusion.vector_hits == 0, say "keyword retrieval mode" — never call it
   semantic search.
2. tscore is source trust, not topic relevance. High tscore does not mean a
   high-relevance match; use relevance_score when present and say so when it
   is missing.
3. batch_search returning zero: retry the same conditions as single
   search_intelligence calls (one group at a time) before reporting no data.
4. Exact phrases or multi-word must_keywords returning zero: first try
   `+A +B` (AND on individual words), then the phrase; report the query_plan
   you used.
5. When results are empty or degraded, state the limitation and the affected
   source_ids; never fill the gap with invented facts.
"""


def main():
    """MCP server entry point."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8100"))
    path = os.environ.get("MCP_PATH", "/mcp")

    if transport == "streamable-http":
        import uvicorn

        from .http_auth import build_authenticated_app

        app = build_authenticated_app(
            mcp, path=path,
            json_response=_env_flag("MCP_JSON_RESPONSE", False),
        )
        uvicorn.run(app, host=host, port=port, log_level="info")
    elif transport == "sse":
        mcp.run(transport="sse", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()