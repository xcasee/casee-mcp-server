"""casee_mcp_server - CaSee Intelligence MCP Server.

Exposes CaSee competitive-intelligence retrieval as MCP tools for AI agents.

Provides the following MCP tools:
- search_intelligence       Complex-logic intelligence retrieval
- analyze_trend             Time-series trend analysis
- aggregate_intelligence    Aggregation by category / region / language
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

CVC lifecycle tools (v1.6, OpenAPI /api/v1/openapi):
- search_cvc_companies      Search candidate companies (async job + bounded poll)
- create_cvc_model          Create a CVC draft (async job + bounded poll)
- get_company_search_job    Poll a company-search job by job_id
- get_cvc_creation_job      Poll a CVC-creation job by job_id
- get_cvc_build_status      Poll build phases / current step / failure hints

OpenAPI coverage tools (v1.7, registered from openapi_tools.py):
- Billing:     casee_get_credits, casee_get_credit_ledger, casee_get_quota
- Discovery:   casee_list_endpoints, casee_get_endpoint, casee_list_mcp_tools
- Charts:      casee_search_charts, casee_get_chart, casee_preview_chart,
               casee_get_chart_bundle
- Templates:   casee_get_report_template, casee_download_report_template,
               casee_create_report_template, casee_update_report_template,
               casee_delete_report_template, casee_validate_report_template
- Reports:     casee_download_report, casee_issue_report_access_code
- CVC:         casee_home_overview, casee_get_cvc_model, casee_list_cvc_companies,
               casee_list_cvc_entities, casee_get_cvc_entity,
               casee_get_ontology_schema, casee_export_ontology,
               casee_search_cvc_intelligence, casee_get_intelligence_detail,
               casee_export_intelligence, casee_get_cvc_dashboard,
               casee_get_market_trends, casee_get_enterprise_insight,
               casee_get_market_insight, casee_get_customer_insight

MCP prompts (v1.6):
- cvc_creation              search → user confirms (all by default) → create →
                            rebuild → tell user ~10 min + completion email
- report_generation         list → plan → generate → poll → share → declare quality
- retrieval_quality_guard   Degraded-semantic / zero-result disclosure protocol

Startup:
    # stdio mode (recommended for WorkBuddy / Claude Desktop)
    CASEE_IS_API_KEY=casee_xxx python -m casee_mcp_server

    # Streamable-HTTP mode (recommended for Trae Work / WorkBuddy / remote agents)
    CASEE_IS_API_KEY=casee_xxx MCP_TRANSPORT=streamable-http python -m casee_mcp_server

    # Opt in to single-JSON responses (avoids SSE parsing issues)
    MCP_JSON_RESPONSE=true
"""

import ast
import hashlib
import os
import re
import time
from collections import Counter, defaultdict
from typing import Annotated

from pydantic import Field

from mcp.server import MCPServer
from casee import (
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

from .reports import get_report_client, resolve_is_api_key
from .request_context import bind_caller_api_key, get_caller_api_key, get_caller_session
from .sanitize import sanitize_payload
from .version import __version__


_INSTRUCTIONS = """\
CaSee 企业竞争情报平台使用规则（服务级指引，优先于单个工具描述）

你面对的是「公开情报检索 → 价值链建模 → 语义检索 → 经营洞察 → 报告交付」的完整链路，
不要只做一次检索就结束。每次任务先选路线，再调工具：

0) 会话开始、用户问“你能做什么”，或不确定该用哪个能力 → 先调用 casee_guide（免费）。
   目标模糊但像商业任务（“帮我盯…”、“这家公司靠不靠谱”、“出个报告”）→ 先调用
   casee_plan_task（免费），把 2-3 个方案和成本/时长转述给用户，让用户选。
1) 只想看最近发生了什么 → search_intelligence（免费、秒级）。
2) 要持续跟踪 / 跨时间对比 / 盯竞争对手 → 必须建价值链（CVC）：
   search_cvc_companies（免费，确认主体）→ create_cvc_model（首条免费；lite 1.0 credit
   约 3-5 分钟 / full 2.5 credits 约 10 分钟）→ rebuild_cvc_model → get_cvc_build_status。
   建链是异步的：同一轮对话最多轮询 3 次；明确告诉用户时长 + 完成邮件，不要阻塞等待。
   想先看效果再建链 → 打开公共样本 cvc_demoevbat01（只读、0 credit，看 dashboard/洞察）。
3) 已有 cvc_id，要按语义找证据 → search_with_cvc 收集 → semantic_search_tool 检索。
4) 已有 cvc_id，要做判断（画像/风险/差距/市场机会/客户声音）→
   casee_get_enterprise_insight / casee_get_market_insight / casee_get_customer_insight
   （0.5 credit/次、8-20 秒）；先 dry_run=true 预检，不扣费。
5) 要交付 → list_report_templates → plan_report（免费）→ generate_report（1 credit）。

硬性规则：
- 任何收费或超过 30 秒的操作，先在回复中说明 cost_credits、eta_seconds、是否可取消，
  并等待用户确认后再执行。
- 涉及公司主体的洞察，company_name 必须来自 casee_list_cvc_companies 的返回值。
- 工具返回的 recommendations / next_actions / alternatives 是下一步的权威建议，优先采用。
- 遇到 PRODUCT_ACCESS_DENIED、0 结果或语义降级：不要静默重试，按 alternatives 给出
  免费替代路径（dashboard / market_trends / 普通检索），并说明如何开通。
- 不要臆造 cvc_id、template_id、chart code 或示例公司名；先查再传。
"""


mcp = MCPServer(
    name="casee",
    version=__version__,
    instructions=_INSTRUCTIONS,
    description="CaSee enterprise competitive-intelligence retrieval and report generation - "
                "complex retrieval, trend analysis, semantic search, "
                "CVC creation / build tracking / inspection / insights, credits & ledger, "
                "charts, report templates / pre-flight / generation / download / sharing",
    # Bind X-API-Key per inbound MCP message so outbound CaSee calls run as the
    # calling tenant. Must be an MCP middleware: tool handlers execute in the
    # session receive task, where ASGI-layer context does not apply.
    middleware=[bind_caller_api_key],
)

# Release version is defined in .version; __init__.py re-exports this value.


# ---- CVC model id validation (pattern ^cvc_[a-z0-9]{8,32}$) ----

_CVC_MODEL_ID_RE = re.compile(r"^cvc_[a-z0-9]{8,32}$")

# Annotated type: the pattern lives in the description (not as a schema
# constraint) so a malformed id reaches the tool body and becomes a structured
# {ok:false, error_code:"VALIDATION_ERROR", hint, action} payload. A JSON-Schema
# pattern would make the MCP layer reject the call with raw protocol text,
# which agent platforms cannot render or recover from.
CvcModelId = Annotated[
    str,
    Field(description="CVC model id, format ^cvc_[a-z0-9]{8,32}$"),
]


_CONTACT_DEFAULTS = {
    "contact_email": "info@xcasee.com",
    "contact_wechat": "@casee_me",
    "contact_url": "/billing/service-contact",
}

# Structured fallbacks attached to errors so the agent always has a next step
# that does not require the user to fix permissions or top up first.
_ALTERNATIVES = {
    "PRODUCT_ACCESS_DENIED": [
        {"tool": "casee_get_cvc_dashboard", "args": {"cvc_id": "<cvc_id>"},
         "why": "免费看板：已入库资讯、实体与趋势", "cost_credits": 0, "eta_seconds": 5},
        {"tool": "casee_get_market_trends", "args": {"cvc_id": "<cvc_id>"},
         "why": "免费市场趋势：热度、来源与话题分布", "cost_credits": 0, "eta_seconds": 5},
        {"tool": "search_intelligence", "args": {"q": "<topic>", "days": 30},
         "why": "普通公开情报检索，无需产品模块", "cost_credits": 0, "eta_seconds": 8},
    ],
    "CREDITS_INSUFFICIENT": [
        {"tool": "casee_get_credits", "args": {},
         "why": "查看余额与首条 CVC 免费额度", "cost_credits": 0, "eta_seconds": 2},
        {"tool": "search_intelligence", "args": {"q": "<topic>", "days": 30},
         "why": "免费检索仍可继续", "cost_credits": 0, "eta_seconds": 8},
    ],
    "VALIDATION_ERROR": [
        {"tool": "casee_list_cvc_companies", "args": {},
         "why": "获取合法 cvc_id / company_name，不要臆造", "cost_credits": 0, "eta_seconds": 2},
        {"tool": "casee_guide", "args": {},
         "why": "确认参数与前置条件", "cost_credits": 0, "eta_seconds": 1},
    ],
    "NOT_FOUND": [
        {"tool": "casee_list_cvc_companies", "args": {},
         "why": "确认当前租户可见的价值链", "cost_credits": 0, "eta_seconds": 2},
        {"tool": "casee_guide", "args": {},
         "why": "查看正确的前置步骤", "cost_credits": 0, "eta_seconds": 1},
    ],
    # 只读资源（公共 Demo / 行业模版）不需要重建 API Key，而是继续免费体验或
    # 基于样本创建自己的专属分析，避免把「只读」误判成「密钥缺权限」。
    "SCOPE_DENIED_WRITE": [
        {"tool": "casee_get_cvc_dashboard", "args": {"cvc_id": "<cvc_id>"},
         "why": "只读样本可直接免费查看看板", "cost_credits": 0, "eta_seconds": 5},
        {"tool": "search_cvc_companies", "args": {"query": "<topic>", "limit": 8},
         "why": "基于样本创建专属分析：search_cvc_companies → create_cvc_model（lite 1.0 credit）",
         "cost_credits": 0, "eta_seconds": 30},
        {"tool": "casee_plan_task", "args": {"goal": "<goal>"},
         "why": "让服务端给出体验/建链/洞察的可选方案", "cost_credits": 0, "eta_seconds": 1},
    ],
}

# Session-scoped memory for "show once" recommendations and repeated queries.
_REC_SEEN: dict = {}
_QUERY_HITS: dict = {}
_REC_TTL_SECONDS = 7 * 24 * 3600
_QUERY_TTL_SECONDS = 24 * 3600


def _recommendation(
    rec_id: str,
    *,
    kind: str,
    priority: str,
    message: str,
    tool: str,
    args: dict | None = None,
    cost_credits: float = 0,
    eta_seconds: int = 0,
    requires_user_confirm: bool = False,
    show_once_per_session: bool = True,
) -> dict:
    """Build one entry of the unified ``recommendations`` contract."""
    return {
        "id": rec_id,
        "kind": kind,  # next_step | capability | fallback | quality_warning
        "priority": priority,  # high | medium | low
        "message": message,
        "tool": tool,
        "args": args or {},
        "cost_credits": cost_credits,
        "eta_seconds": eta_seconds,
        "requires_user_confirm": requires_user_confirm,
        "show_once_per_session": show_once_per_session,
    }


def _session_key() -> str:
    """Stable per-key + per-session bucket for dedupe (never logs the key)."""
    raw = f"{get_caller_api_key()}|{get_caller_session()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _attach_recommendations(payload, recs: list[dict], *, into_data: bool = False):
    """Attach deduped recommendations (max 3) to a tool result."""
    if not isinstance(payload, dict) or not recs:
        return payload
    now = time.time()
    key_prefix = _session_key()
    fresh = []
    for rec in recs:
        marker = f"{key_prefix}:{rec['id']}"
        seen_at = _REC_SEEN.get(marker, 0)
        if seen_at and now - seen_at < _REC_TTL_SECONDS:
            continue
        _REC_SEEN[marker] = now
        fresh.append({k: v for k, v in rec.items() if k != "show_once_per_session"})
    if not fresh:
        return payload
    target = payload
    if into_data:
        data = payload.get("data")
        if isinstance(data, dict):
            target = data
    existing = target.get("recommendations") or []
    target["recommendations"] = (fresh + list(existing))[:3]
    return payload


def _record_query(q: str) -> int:
    """Count how often this tenant asked the same query in the last 24h."""
    marker = f"{_session_key()}:{' '.join((q or '').lower().split())}"
    now = time.time()
    hits = [ts for ts in _QUERY_HITS.get(marker, []) if now - ts < _QUERY_TTL_SECONDS]
    hits.append(now)
    _QUERY_HITS[marker] = hits
    return len(hits)


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
        "hint": "Invalid or missing API key; check the CASEE_IS_API_KEY environment variable.",
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
    "TEMPLATE_TOO_LARGE": {
        "hint": "The report or template exceeds the share/size limit. Do not retry the same "
                "request; for reports call casee_download_report(job_id=..., format='html') "
                "and distribute the file, or reduce sections/charts and regenerate.",
        "retryable": False,
        "action": "fix_request",
    },
    "TEMPLATE_HEADER_INVALID": {
        "hint": "Fix the template Markdown front-matter (YAML header) and retry.",
        "retryable": False,
        "action": "fix_request",
    },
    "TEMPLATE_SECTION_INVALID": {
        "hint": "Give every template section a unique id and retry.",
        "retryable": False,
        "action": "fix_request",
    },
    "TEMPLATE_CHART_INVALID": {
        "hint": "Use chart codes returned by casee_search_charts (agent_supported=true).",
        "retryable": False,
        "action": "fix_request",
    },
    "TEMPLATE_PLACEHOLDER_INVALID": {
        "hint": "Use only declared placeholders such as {report.cvc_name}.",
        "retryable": False,
        "action": "fix_request",
    },
    "TEMPLATE_NAMESPACE_INVALID": {
        "hint": "Tenant templates must use the tenant.<id> namespace prefix.",
        "retryable": False,
        "action": "fix_request",
    },
    "TEMPLATE_NOT_FOUND": {
        "hint": "Call list_report_templates to get an available template_id.",
        "retryable": False,
        "action": "fix_request",
    },
    "TEMPLATE_CONFLICT": {
        "hint": "The template_id collides with a system template; choose another id.",
        "retryable": False,
        "action": "fix_request",
    },
    "TEMPLATE_NO_CHARTS": {
        "hint": "Switch template or widen time_range (e.g. quarter) and rerun plan_report.",
        "retryable": False,
        "action": "fix_request",
    },
    "CHART_UNSUPPORTED": {
        "hint": "Call casee_search_charts to get registered, agent-supported ca_code values.",
        "retryable": False,
        "action": "fix_request",
    },
    "CHART_DATA_UNAVAILABLE": {
        "hint": "The chart has no data for this CVC/time_range; choose another chart or "
                "widen time_range.",
        "retryable": False,
        "action": "fix_request",
    },
    "JOB_NOT_READY": {
        "hint": "The report job is not finished (or concurrent jobs are pending). Poll "
                "get_report_job until status is succeeded/partial, then retry.",
        "retryable": True,
        "action": "retry_later",
    },
    "CVC_BUILD_IN_PROGRESS": {
        "hint": "A build is already running; poll get_cvc_build_status and retry after it "
                "completes.",
        "retryable": True,
        "action": "retry_later",
    },
    "REBUILD_IN_PROGRESS": {
        "hint": "A rebuild is already running; poll get_cvc_build_status and retry later.",
        "retryable": True,
        "action": "retry_later",
    },
    "REBUILD_CONFIRMATION_REQUIRED": {
        "hint": "Call the tool again with confirm=true (and backup=true) to execute.",
        "retryable": False,
        "action": "fix_request",
    },
    "CVC_NAME_CONFLICT": {
        "hint": "Use a different CVC name, or pass overwrite=true to replace it.",
        "retryable": False,
        "action": "fix_request",
    },
    "CVC_SIMILAR_CONFLICT": {
        "hint": "A similar CVC already exists; choose another topic or pass overwrite=true "
                "after user confirmation.",
        "retryable": False,
        "action": "fix_request",
    },
    "MODEL_VERSION_INCOMPATIBLE": {
        "hint": "The CVC model version does not match the template; rebuild the model or "
                "choose another template.",
        "retryable": False,
        "action": "contact_admin",
    },
    "SHARE_EXPIRED": {
        "hint": "The share/access code expired or was used; create a new one.",
        "retryable": False,
        "action": "fix_request",
    },
    "SCOPE_DENIED_WRITE": {
        "hint": "The API key lacks a write scope (template:write / cvc:write). Recreate it "
                "with the required scope.",
        "retryable": False,
        "action": "reauthenticate",
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
        nested_detail = body.get("detail")
        if isinstance(nested_detail, dict):
            # FastAPI HTTPException(detail={code, message, details}) shape used by
            # the report / charts / template routers.
            message = str(nested_detail.get("message") or message)
            if nested_detail.get("code") is not None:
                upstream_code = nested_detail.get("code")
            nested = nested_detail.get("details")
            if isinstance(nested, dict):
                details.update(nested)
        else:
            message = str(body.get("message") or message)
            upstream_code = body.get("code")
            nested = body.get("details")
            if isinstance(nested, dict):
                details.update(nested)
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


# Design error codes (eirs/api_meta/endpoint_meta.py) that are more actionable
# than a generic HTTP-status mapping. Codes absent here keep the legacy mapping
# so existing agent contracts (NOT_FOUND / SERVER_ERROR / RATE_LIMITED) hold.
_UPSTREAM_ERROR_CODES = {
    40005: "CHART_UNSUPPORTED",
    40011: "TEMPLATE_HEADER_INVALID",
    40012: "TEMPLATE_SECTION_INVALID",
    40013: "TEMPLATE_CHART_INVALID",
    40014: "TEMPLATE_PLACEHOLDER_INVALID",
    40015: "TEMPLATE_TOO_LARGE",
    40016: "TEMPLATE_NAMESPACE_INVALID",
    40102: "AUTHENTICATION_FAILED",
    40201: "CREDITS_INSUFFICIENT",
    40301: "SCOPE_DENIED",
    40302: "SCOPE_DENIED_WRITE",
    40303: "PRODUCT_ACCESS_DENIED",
    40406: "TEMPLATE_NOT_FOUND",
    40407: "TEMPLATE_NOT_FOUND",
    40901: "CHART_DATA_UNAVAILABLE",
    40902: "TEMPLATE_NO_CHARTS",
    40903: "JOB_NOT_READY",
    40904: "CVC_NAME_CONFLICT",
    40905: "CVC_BUILD_IN_PROGRESS",
    40907: "TEMPLATE_CONFLICT",
    40908: "CVC_SIMILAR_CONFLICT",
    40910: "MODEL_VERSION_INCOMPATIBLE",
    40911: "REBUILD_CONFIRMATION_REQUIRED",
    40912: "REBUILD_IN_PROGRESS",
    41001: "SHARE_EXPIRED",
    41002: "SHARE_EXPIRED",
}


def _classify_error(
    exc: Exception,
    message: str,
    status_code: int | None,
    details: dict,
    upstream_code: int | str | None = None,
) -> str:
    """Map an exception to a stable error_code, with product/quota priority."""
    marker = f"{details.get('error') or ''} {message}".upper()
    message_lower = message.lower()

    # An explicit machine code from the response always wins.
    explicit = str(details.get("error") or "").strip().upper()
    if explicit in _ERROR_POLICY:
        return explicit

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
    if upstream_code is not None:
        try:
            mapped = _UPSTREAM_ERROR_CODES.get(int(upstream_code))
        except (TypeError, ValueError):
            mapped = None
        if mapped:
            return mapped
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
    code = _classify_error(exc, message, status_code, details, upstream_code)

    if status_code is None:
        status_code = {
            "PRODUCT_ACCESS_DENIED": 403,
            "SCOPE_DENIED": 403,
            "SCOPE_DENIED_WRITE": 403,
            "TENANT_DISABLED": 403,
            "AUTHENTICATION_FAILED": 401,
            "CREDITS_INSUFFICIENT": 402,
            "RATE_LIMITED": 429,
            "VALIDATION_ERROR": 422,
            "NOT_FOUND": 404,
            "TEMPLATE_NOT_FOUND": 404,
            "TEMPLATE_TOO_LARGE": 413,
            "TEMPLATE_HEADER_INVALID": 400,
            "TEMPLATE_SECTION_INVALID": 400,
            "TEMPLATE_CHART_INVALID": 400,
            "TEMPLATE_PLACEHOLDER_INVALID": 400,
            "TEMPLATE_NAMESPACE_INVALID": 400,
            "TEMPLATE_CONFLICT": 409,
            "TEMPLATE_NO_CHARTS": 409,
            "CHART_UNSUPPORTED": 400,
            "CHART_DATA_UNAVAILABLE": 409,
            "JOB_NOT_READY": 409,
            "CVC_NAME_CONFLICT": 409,
            "CVC_SIMILAR_CONFLICT": 409,
            "CVC_BUILD_IN_PROGRESS": 409,
            "REBUILD_IN_PROGRESS": 409,
            "MODEL_VERSION_INCOMPATIBLE": 409,
            "REBUILD_CONFIRMATION_REQUIRED": 409,
            "SHARE_EXPIRED": 410,
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
    action = policy["action"]

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
            # [P2] 市场情报平台未订阅时给出功能等价替代指引（本地聚合降级）
            if "market" in product.lower() or "市场情报" in product:
                hint += (
                    " Meanwhile, use aggregate_intelligence / analyze_trend "
                    "to compute equivalent heat / sources / topics locally "
                    "from retrieved intelligence items."
                )
        for key, value in _CONTACT_DEFAULTS.items():
            details.setdefault(key, value)
    elif code == "TENANT_DISABLED":
        for key, value in _CONTACT_DEFAULTS.items():
            details.setdefault(key, value)
    elif code == "SCOPE_DENIED_WRITE" and str(details.get("reason") or "") in (
        "public_read_only", "template_read_only",
    ):
        # 公共 Demo / 行业模版只读：写入被拒是产品设计，不是密钥缺权限。
        hint = (
            "该 CVC 为只读样本（公共 Demo / 行业模版），不能写入或重建。"
            "免费体验：casee_get_cvc_dashboard / casee_get_enterprise_insight(dry_run=true)。"
            "需要专属分析：search_cvc_companies → create_cvc_model(build_profile='lite', "
            "1.0 credit) → rebuild_cvc_model。"
        )
        action = "use_alternative"
    elif code == "AUTHENTICATION_FAILED" and get_caller_api_key():
        hint = (
            "The caller X-API-Key was rejected by CaSee (40102). Use a "
            "casee_... tenant key created at https://www.casee.me "
            "(系统配置 → API Keys): it is registered in both is-server and "
            "EIRS. IS-only sk_... keys cannot access report / "
            "CVC-management endpoints."
        )
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
        "action": action,
        "alternatives": list(_ALTERNATIVES.get(code, [])),
        "request_id": str(details.get("request_id") or ""),
    }


def _safe_call(fn, *args, **kwargs):
    """Invoke an SDK call, converting any exception into a structured error dict.

    Success results are passed through :func:`sanitize.sanitize_payload` so the
    agent never sees source catalog entries (``source_url`` / ``source_domain`` /
    ``properties`` / raw ``source_id``) or collection-channel traces
    (Google News links and ``gnews`` markers). On failure it returns an error
    envelope with a stable ``error_code`` plus ``details`` / ``hint`` /
    ``retryable`` / ``action`` so agents can distinguish insufficient Credits
    from a missing product subscription or an invalid API key.
    """
    try:
        return sanitize_payload(fn(*args, **kwargs))
    except CaseeError as exc:
        return sanitize_payload(_error_payload(exc))
    except Exception as exc:  # pragma: no cover - defensive
        payload = _error_payload(exc)
        payload["error"] = "UnexpectedError"
        payload["error_code"] = "UNEXPECTED_ERROR"
        return sanitize_payload(payload)


def _caller_key_kwargs() -> dict:
    """Return the API key to use for an outbound CaSee call.

    Prefers the verified caller key so streamable-HTTP requests run as the
    calling tenant. Outside such requests (stdio / anonymous fallback) it
    returns the deployment ``CASEE_IS_API_KEY``; passing it explicitly keeps
    the SDK from depending on its own ``CASEE_API_KEY`` env lookup.
    """
    key = get_caller_api_key() or resolve_is_api_key()
    return {"api_key": key} if key else {}


def _sdk_call(fn, **kwargs):
    """Keyword-only convenience wrapper around :func:`_safe_call`.

    Binds the verified caller key so retrieval runs as the calling tenant.
    """
    kwargs.update(_caller_key_kwargs())
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
        return "Invalid or missing API key; check the CASEE_IS_API_KEY environment variable."
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


def _item_dim_value(item: dict, dim: str) -> str:
    """Read a public dimension (category/region/language) from an item.

    is_storage normalizes items to ``source_{dim}`` naming
    (``source_category`` / ``source_region`` / ``source_language``); fall back
    to legacy ``{dim}`` and nested ``source.{dim}`` so aggregation stays
    correct across schema generations.
    """
    return (
        item.get(f"source_{dim}")
        or item.get(dim)
        or (item.get("source") or {}).get(dim)
        or ""
    )


# ---- Tool 1: intelligence retrieval ----


@mcp.tool(
    title="公开情报检索（免费、秒级）",
    annotations={"readOnlyHint": True, "openWorldHint": True, "idempotentHint": True},
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 8},
)
def search_intelligence(
    q: str,
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
    """公开情报检索（免费、秒级）：一次性问答、看最近动态的第一步。

    USE WHEN: 用户只想知道“最近发生了什么 / 有什么新闻 / 有什么动态”，
    不需要持续跟踪、不需要历史沉淀、不需要报告。

    DO NOT USE WHEN:
    - 用户要“持续跟踪 / 盯住某家公司 / 跨时间对比 / 每周提醒” → 建链
      （search_cvc_companies → create_cvc_model → rebuild_cvc_model）。
    - 用户已有 cvc_id，要按语义找证据 → search_with_cvc / semantic_search_tool。
    - 用户要判断/画像/风险/差距 → casee_get_enterprise_insight。

    PREREQUISITES: 无。COST: 0 credit。LATENCY: 约 8 秒。
    RETURNS: organic_results + recommendations（下一步建议，务必优先采用）。
    NEXT: 结果里出现 recommendations 时按其中 tool/args 继续；若同一问题反复出现，
    建议建链以沉淀历史。

    Query syntax (q parameter, takes precedence over everything):
    - ``+word``: must contain (AND logic)
    - ``word1 word2``: any match (OR logic)
    - ``-word``: exclude (NOT logic)
    - ``"exact phrase"``: exact phrase match
    - ``word1|word2``: synonym group (OR logic)
    - ``(a OR b)``: group logic

    Scope results with ``category`` / ``region`` / ``language`` / ``min_tscore``;
    source-level identifiers are intentionally not part of the public contract.

    Citation discipline (IMPORTANT):
    - Quote ONLY from ``title`` / ``description`` / ``abstract`` and always
      attach the item's original ``citation.url``.
    - ``summary`` may be AI-expanded text (``summary_origin=llm_expanded``):
      it is for reading comprehension only and MUST NOT be quoted. Legacy
      items without ``summary_origin`` (unknown) follow the same rule.
    - Items whose ``content_flags`` include ``anti_bot_page`` returned an
      anti-bot detection page instead of the real article; do not quote their
      body text.

    Args:
        q: query-syntax string, e.g. +(Tesla|BYD) +(EV|battery) -rumor
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
        dict: advanced search response from the CaSee API,
            {search_metadata: {..., status}, organic_results: [{title,
            description, source, tscore, published_at, ...}], ...}
    """
    result = _sdk_call(
        search_advanced,
        q=q,
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
    if isinstance(result, dict) and not result.get("error_code") and not result.get("error"):
        items = result.get("organic_results") or result.get("items") or []
        hits = _record_query(q)
        recs = []
        if hits >= 3:
            recs.append(_recommendation(
                "repeat_query_build_cvc",
                kind="capability", priority="high",
                message=(f"同一问题今天已检索 {hits} 次。建链后历史会沉淀，"
                         "后续可用语义检索与洞察，不必重复全站检索。"),
                tool="search_cvc_companies", args={"query": q, "limit": 8},
                cost_credits=0, eta_seconds=8, requires_user_confirm=True,
            ))
        if not items:
            recs.append(_recommendation(
                "widen_window",
                kind="fallback", priority="high",
                message="0 条结果：把时间窗口放宽到 90 天，或把 min_tscore 降为 0 再试。",
                tool="search_intelligence",
                args={"q": q, "days": 90, "min_tscore": 0.0},
                eta_seconds=8,
            ))
        recs.append(_recommendation(
            "plan_next_step",
            kind="next_step", priority="low",
            message="若要持续跟踪、跨时间对比或出报告，可让 CaSee 给出 2-3 个方案（免费）。",
            tool="casee_plan_task", args={"goal": q}, eta_seconds=1,
        ))
        _attach_recommendations(result, recs)
    return result


# ---- Tool 3: trend analysis ----


@mcp.tool(
    title="时间序列趋势分析（免费）",
    annotations={"readOnlyHint": True, "openWorldHint": True, "idempotentHint": True},
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 10},
)
def analyze_trend(
    q: str,
    min_tscore: float = 0.1,
    days: int = 90,
    limit: int = 200,
) -> dict:
    """时间序列趋势分析：按日/周聚合检索结果，看热度、周期与拐点。

    USE WHEN: 用户问“最近热度怎么样 / 什么时候开始火的 / 有没有明显拐点”。
    DO NOT USE WHEN: 需要解释原因或形成判断（→ 洞察工具）；需要沉淀历史（→ 建链）。
    PREREQUISITES: 无。COST: 0 credit。LATENCY: 约 10 秒。
    NEXT: 出现拐点后可用 casee_get_market_insight 解释（0.5 credit，先 dry_run）。

    Time-series trend analysis of retrieval results, aggregated by day/week.

    Suitable for:
    - Tracking public-opinion heat around a topic over time
    - Discovering reporting cycles of a vendor/product
    - Identifying the timing of incidents or spikes

    Args:
        q: query-syntax string (as in search_intelligence)
        min_tscore: minimum trust
        days: analysis window (days)
        limit: max number of items retrieved

    Returns:
        dict: {trend: [{date, count, top_categories, top_keywords}], summary: {...}}
    """
    result = _sdk_call(
        search_advanced,
        q=q, min_tscore=min_tscore,
        days=days, limit=limit,
    )
    if isinstance(result, dict) and "error" in result:
        return result
    items = result.get("organic_results", []) or result.get("items", [])

    by_date = defaultdict(lambda: {"count": 0, "categories": Counter(), "keywords": Counter()})
    for item in items:
        pub = (item.get("published_at") or item.get("pub_date") or "")[:10]
        if not pub:
            continue
        by_date[pub]["count"] += 1
        cat = _item_dim_value(item, "category")
        if cat:
            by_date[pub]["categories"][cat] += 1
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
            "top_categories": [c for c, _ in d["categories"].most_common(3)],
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
        },
    }


# ---- Tool 3: dimension aggregation analysis ----


@mcp.tool(
    title="维度聚合（免费）",
    annotations={"readOnlyHint": True, "openWorldHint": True, "idempotentHint": True},
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 10},
)
def aggregate_intelligence(
    q: str,
    dimension: str = "category",
    min_tscore: float = 0.1,
    days: int = 30,
    limit: int = 100,
) -> dict:
    """按维度聚合检索结果（category / region / language）。

    USE WHEN: 用户问“主要在哪些地区/哪些品类/哪些语言在讨论”。
    DO NOT USE WHEN: 需要趋势（→ analyze_trend）或判断（→ 洞察工具）。
    PREREQUISITES: 无。COST: 0 credit。LATENCY: 约 10 秒。
    NEXT: 选定重点维度后可建链持续跟踪。

    Aggregate retrieval results by a public dimension: category / region / language.

    Suitable for:
    - Comparing how much attention different topic categories attract
    - Contrasting regional coverage differences
    - Selecting dimensions for ongoing tracking

    Args:
        q: query-syntax string (as in search_intelligence)
        dimension: aggregation dimension, one of ``category`` / ``region`` / ``language``
        min_tscore: minimum trust
        days: time window
        limit: max number of items retrieved

    Returns:
        dict: {groups: [{key, count, avg_tscore, sample_titles}]}
    """
    dim = (dimension or "category").strip().lower()
    if dim not in ("category", "region", "language"):
        return _report_invalid(
            "dimension must be one of: category, region, language",
            {"dimension": dimension},
        )

    result = _sdk_call(
        search_advanced,
        q=q, min_tscore=min_tscore,
        days=days, limit=limit,
    )
    if isinstance(result, dict) and "error" in result:
        return result
    items = result.get("organic_results", []) or result.get("items", [])

    groups = defaultdict(lambda: {"count": 0, "tscores": [], "titles": []})
    for item in items:
        key = _item_dim_value(item, dim) or "unknown"
        ts = item.get("tscore") or item.get("source", {}).get("tscore", 0)
        groups[str(key)]["count"] += 1
        groups[str(key)]["tscores"].append(float(ts) if ts else 0)
        groups[str(key)]["titles"].append(item.get("title", "")[:80])

    output = []
    for key, data in sorted(groups.items(), key=lambda x: -x[1]["count"]):
        tscores = data["tscores"]
        output.append({
            "key": key,
            "count": data["count"],
            "avg_tscore": round(sum(tscores) / len(tscores), 3) if tscores else 0,
            "sample_titles": data["titles"][:3],
        })

    return {
        "query": q,
        "dimension": dim,
        "total_items": len(items),
        "groups": output,
    }


# ---- Tool 5: CVC semantic search ----


@mcp.tool(
    title="价值链语义检索（需已完成建链）",
    annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T1", "cost_credits": 0, "eta_seconds": 5, "requires": "completed CVC"},
)
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
    """价值链语义检索（BM25 + 向量 ANN + RRF 融合）：在已建链模型内找证据。

    USE WHEN: 用户已有 cvc_id（来自 casee_list_cvc_companies / casee_home_overview），
    要按语义而不是关键词找证据，例如“竞争对手在东南亚的电动化策略”、
    “客户对续航的真实抱怨”。

    DO NOT USE WHEN:
    - 还没有 cvc_id → 先 search_cvc_companies → create_cvc_model →
      rebuild_cvc_model（lite 约 3-5 分钟 / full 约 10 分钟）。
    - 只想看最近新闻 → search_intelligence（免费，不需要建链）。
    - 模型刚建好但还没完成采集 → 先 get_cvc_build_status，完成后再检索。

    PREREQUISITES: cvc_id 对应模型已 completed 且已采集语料；
    后端 semantic_stage2_enabled 关闭时向量侧会降级（返回里 degraded=true）。
    COST: 0 credit。LATENCY: 秒级。RETURNS: organic_results +
    search_information.fusion（含 degraded）+ recommendations。
    NEXT: 0 结果或 degraded 时按 recommendations 先用 search_with_cvc 收集，
    或退回 search_intelligence；需要判断时调用洞察工具。

    ``semantic_search_tool`` 的两阶段混合检索：
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
        return _report_invalid(err, {"cvc_model_id": cvc_model_id})

    valid_modes = ("hybrid", "semantic", "keyword")
    if mode not in valid_modes:
        return _report_invalid(
            f"mode must be one of {valid_modes}, got: {mode!r}", {"mode": mode},
        )

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

    result = _sdk_call(semantic_search, **kwargs)
    if isinstance(result, dict) and not result.get("error_code") and not result.get("error"):
        items = result.get("organic_results") or result.get("items") or []
        fusion = (result.get("search_information") or {}).get("fusion") or {}
        recs = []
        if fusion.get("degraded"):
            recs.append(_recommendation(
                "semantic_degraded",
                kind="quality_warning", priority="high",
                message=("向量检索已降级为关键词检索，结果可能漏掉语义相关内容；"
                         "向用户说明这一点，并可用 search_with_cvc 先补充语料。"),
                tool="search_with_cvc",
                args={"q": q, "cvc_model_id": cvc_model_id},
                eta_seconds=8,
            ))
        if not items:
            recs.append(_recommendation(
                "collect_then_retry",
                kind="fallback", priority="high",
                message=("该模型还没有命中语料：先用 search_with_cvc 把相关资讯采集进模型，"
                         "再重试语义检索；也可以先用 search_intelligence 直接看公开情报。"),
                tool="search_with_cvc",
                args={"q": q, "cvc_model_id": cvc_model_id},
                eta_seconds=8,
            ))
        else:
            recs.append(_recommendation(
                "deep_dive_insight",
                kind="next_step", priority="medium",
                message="若要形成判断（画像/风险/差距/市场机会），可对同一 cvc_id 生成洞察（0.5 credit）。",
                tool="casee_get_enterprise_insight",
                args={"cvc_id": cvc_model_id, "company_name": "<from casee_list_cvc_companies>",
                      "dry_run": True},
                cost_credits=0.5, eta_seconds=20, requires_user_confirm=True,
            ))
        _attach_recommendations(result, recs)
    return result


# ---- Tool 6: advanced retrieval with CVC collection ----


@mcp.tool(
    title="布尔检索 + 采集进价值链",
    annotations={"readOnlyHint": False, "openWorldHint": True, "idempotentHint": False},
    meta={"tier": "T1", "cost_credits": 0, "eta_seconds": 8, "side_effects": "writes CVC store"},
)
def search_with_cvc(
    q: str,
    cvc_model_id: str = "",
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
    """布尔检索，并把结果异步采集进指定价值链（为语义检索/AI 洞察准备语料）。

    USE WHEN: 已有 cvc_id，需要针对一个主题把公开情报收进模型，
    供后续 semantic_search_tool / 洞察复用。

    DO NOT USE WHEN:
    - 没有 cvc_id：不带 cvc_model_id 调用只会返回普通检索结果，不会采集；
      先 search_cvc_companies → create_cvc_model → rebuild_cvc_model。
    - 已经采集过同一主题：直接 semantic_search_tool（语义检索）。
    - 只要一次性答案：search_intelligence。

    PREREQUISITES: cvc_id（格式 ^cvc_[a-z0-9]{8,32}$）。COST: 0 credit。
    LATENCY: 约 8 秒（采集异步）。RETURNS: 检索结果 + cvc_sync_status + recommendations。
    NEXT: 采集后调用 semantic_search_tool；再需要判断时用洞察工具。

    ``search_with_cvc`` 的行为与 search_intelligence 相同，但额外接受 cvc_model_id：

    Typical usage (two-stage intelligent search):
    1. Call this tool with cvc_model_id to collect results
    2. Call semantic_search_tool for deep semantic search

    Args:
        q: query-syntax string, e.g. +(Tesla|BYD) +(EV|battery) -rumor
        cvc_model_id: CVC model id (optional), format ``^cvc_[a-z0-9]{8,32}$``;
            when set, results are collected into the semantic store
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
            return _report_invalid(err, {"cvc_model_id": cvc_model_id})

    kwargs: dict = {
        "q": q,
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
        recs = [_recommendation(
            "semantic_after_collect",
            kind="next_step", priority="high",
            message="语料已采集：现在用 semantic_search_tool 做语义检索，效果优于关键词检索。",
            tool="semantic_search_tool", args={"q": q, "cvc_model_id": cvc_model_id},
            eta_seconds=5,
        )]
        if not items:
            recs.append(_recommendation(
                "widen_collect_window",
                kind="fallback", priority="high",
                message="本次没有采集到内容：放宽 days 到 90 或降低 min_tscore 后重试。",
                tool="search_with_cvc",
                args={"q": q, "cvc_model_id": cvc_model_id, "days": 90, "min_tscore": 0.0},
                eta_seconds=8,
            ))
        _attach_recommendations(result, recs)
    elif isinstance(result, dict) and not result.get("error_code"):
        _attach_recommendations(result, [_recommendation(
            "no_cvc_collection",
            kind="capability", priority="medium",
            message=("未指定 cvc_model_id，本次结果不会沉淀。若要持续跟踪，"
                     "先建链再把 cvc_id 传入。"),
            tool="casee_plan_task", args={"goal": q}, eta_seconds=1,
        )])

    return result


# ---- Tool 7: server-side batch search ----


@mcp.tool(
    title="批量检索（免费）",
    annotations={"readOnlyHint": True, "openWorldHint": True, "idempotentHint": True},
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 20},
)
def batch_search(
    groups: list | None = None,
    dedupe: bool = True,
    max_concurrency: int = 4,
) -> dict:
    """批量检索：一次跑多组查询并跨组去重，适合对比多个主题/公司。

    USE WHEN: 需要同时看 3-10 个主题或公司，且只要一次性结果。
    DO NOT USE WHEN: 需要每条主题长期跟踪（→ 建链）；需要语义证据（→ semantic_search_tool）。
    PREREQUISITES: 无。COST: 0 credit。LATENCY: 约 20 秒。
    NEXT: 结果差异明显时，可让 casee_plan_task 给出建链/洞察方案。

    Server-side batch search: run multiple advanced queries in a single round-trip.

    The server executes the groups concurrently and deduplicates across groups,
    which is lower-latency and better suited to large batches than client-side
    orchestration.

    Each group is a dict that mirrors the advanced-search parameters, e.g.:
        {"id": "must_ai", "must_keywords": ["AI"], "should_keywords": ["Nvidia", "chip"],
         "days": 1000, "limit": 20}
        {"id": "since_inc", "q": "+AI", "days": 30, "since_ts": "2026-09-08T00:00:00Z", "limit": 10}
        {"id": "quick", "q": "smartwatch", "days": 30, "limit": 5}

    - Quick groups (bare comma-separated q, no boolean terms) use quick retrieval (comma = OR);
    - Advanced groups use advanced boolean retrieval.
    - Batch mode never triggers supplementary backfill.
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
        **_caller_key_kwargs(),
    )


# ---- Tool 8: list CVC models ----


@mcp.tool(
    title="列出价值链（免费）",
    annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 2},
)
def list_cvc_models_tool(
    status: str = "completed",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    sort: str = "-updated_at",
) -> dict:
    """列出当前租户的价值链（含状态、实体数、资讯数）。

    USE WHEN: 不确定用户是否已有 cvc_id；用户说“我之前建的那个模型”；
    建链后回来继续（跨会话恢复）。
    DO NOT USE WHEN: 只是要找公司主体（→ search_cvc_companies）。
    PREREQUISITES: 无。COST: 0 credit。LATENCY: 约 2 秒。
    NEXT: 有 completed 模型 → semantic_search_tool / 洞察；有 building → get_cvc_build_status。

    List CVC models with status, model version, entity/intelligence counts and pagination.

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


# ---- CVC lifecycle tools (v1.6) ----

_CVC_JOB_STATUS_PATHS = {
    "company_search": "/companies/search-jobs/{job_id}",
    "cvc_create": "/cvc-models/creation-jobs/{job_id}",
}
_CVC_JOB_POLL_INTERVAL = 2.0


def _cvc_job_status_path(kind: str, job_id: str) -> str:
    return _CVC_JOB_STATUS_PATHS[kind].format(job_id=job_id)


def _poll_cvc_job(client, kind: str, job_id: str, wait_seconds: int) -> dict:
    """Poll a background job until terminal state or the wait budget is exhausted."""
    deadline = time.monotonic() + max(0, int(wait_seconds))
    view: dict = {}
    while True:
        view = client.request(
            "GET", _cvc_job_status_path(kind, job_id), timeout=30.0,
        ) or {}
        status = str(view.get("status") or "")
        if status in ("succeeded", "failed") or time.monotonic() >= deadline:
            return view
        time.sleep(min(_CVC_JOB_POLL_INTERVAL, max(0.2, deadline - time.monotonic())))


def _job_pending_payload(view: dict, *, tool: str, kind: str) -> dict:
    """Return a job view plus a next action when the job is still running/failed."""
    data = dict(view or {})
    data.setdefault("kind", kind)
    status = str(data.get("status") or "")
    if status == "failed":
        error = data.get("error") or {}
        data["user_message"] = (
            f"任务失败：{error.get('message') or '未知错误'}；"
            f"{error.get('hint') or '请读取 error 后决定是否重试'}"
        )
    else:
        data["user_message"] = "任务仍在执行；请用任务查询工具轮询，直到 succeeded/failed。"
        data["next_actions"] = [{
            "tool": tool,
            "args": {"job_id": data.get("job_id")},
            "hint": "每 5 秒查询一次；succeeded 后使用 result 继续下一步",
        }]
    return data


def _is_async_unavailable(exc: Exception) -> bool:
    """Old deployments without async job endpoints return 404/405."""
    return getattr(exc, "status_code", None) in (404, 405) or type(exc).__name__ == "NotFoundError"


@mcp.tool(
    title="搜索候选公司（免费，建链第一步）",
    annotations={"readOnlyHint": True, "openWorldHint": True, "idempotentHint": True},
    meta={"tier": "T1", "cost_credits": 0, "eta_seconds": 8,
          "next": "create_cvc_model"},
)
def search_cvc_companies(query: str, language: str = "zh", limit: int = 8,
                         wait_seconds: int = 240) -> dict:
    """搜索候选公司：建链前的必经第一步，结果必须让用户确认。

    USE WHEN: 用户要持续跟踪/深度分析但没有 company_id；已有明确公司名但不确定主体。
    DO NOT USE WHEN: 只想看新闻（→ search_intelligence）；已有 cvc_id（→ 直接检索/洞察）。
    PREREQUISITES: 无。COST: 0 credit。LATENCY: 约 8 秒（异步任务，最多等 wait_seconds）。
    RETURNS: candidates（company_id/name/简介）+ next_actions。
    NEXT: 让用户勾选公司 → create_cvc_model（lite 1.0 credit / full 2.5 credits，首条免费，
    构建 lite 约 3-5 分钟 / full 约 10 分钟）。

    Search candidate companies before creating a CVC model.

    Uses the async job endpoint first (``POST /companies/search-jobs``) and
    polls up to ``wait_seconds``. If the job is still running, the result is a
    job view; call ``get_company_search_job`` with its ``job_id`` to continue.
    Old deployments without async endpoints automatically fall back to the
    synchronous ``POST /companies/search``.

    Use this when the user gives an analysis goal but no ``cvc_id``, and no
    existing CVC matches the goal. Show the returned companies to the user;
    they are **selected by default** (``default_selection="all"``), so the
    user only needs to exclude indices.

    Args:
        query: analysis goal / company name / industry / product category
        language: zh | en
        limit: max number of companies, 1-20 (default 8; 3-8 works best)
        wait_seconds: max seconds to wait for the job, 0-240 (0 returns job_id immediately)

    Returns:
        dict: {ok, data: {query, query_type, query_topic, suggested_cvc_name,
              items: [{name, name_en, website, description, main_business,
              products[], markets[], competitors[], customers[], selected}],
              total, default_selection, selection_hint, not_found,
              next_actions}, meta}
        If the job is still running: {ok, data: {job_id, kind, status, poll,
              next_actions, user_message}, meta}
    """
    q = (query or "").strip()
    if len(q) < 2:
        return _report_invalid("query must contain at least 2 characters", {"query": query})
    if language not in ("zh", "en"):
        return _report_invalid("language must be zh or en", {"language": language})
    if not 1 <= limit <= 20:
        return _report_invalid("limit must be between 1 and 20", {"limit": limit})
    if not 0 <= wait_seconds <= 240:
        return _report_invalid("wait_seconds must be between 0 and 240", {"wait_seconds": wait_seconds})
    client = get_report_client()

    def call():
        payload = {"query": q, "language": language, "limit": int(limit)}
        try:
            created = client.request(
                "POST", "/companies/search-jobs", json_body=payload, timeout=30.0,
            ) or {}
        except Exception as exc:  # noqa: BLE001 - old deployment fallback
            if not _is_async_unavailable(exc):
                raise
            # 旧部署兜底：同步接口最长 240s
            return client.request(
                "POST", "/companies/search", json_body=payload, timeout=240.0,
            )
        job_id = str(created.get("job_id") or "")
        if not job_id or wait_seconds <= 0:
            return _job_pending_payload(created, tool="get_company_search_job", kind="company_search")
        view = _poll_cvc_job(client, "company_search", job_id, wait_seconds)
        if str(view.get("status") or "") == "succeeded":
            return view.get("result") or {}
        return _job_pending_payload(view, tool="get_company_search_job", kind="company_search")

    return _report_call(call, client)


@mcp.tool(
    title="创建价值链（付费、异步；lite 约 3-5 分钟 / full 约 10 分钟）",
    annotations={"readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
    meta={"tier": "T2", "cost_credits": 1.0, "eta_seconds": 300,
          "requires_user_confirm": True, "first_free": True,
          "requires": "search_cvc_companies first"},
)
def create_cvc_model(
    name: str,
    companies: list,
    description: str = "",
    query_type: str = "industry",
    query_topic: str = "",
    search_input: str = "",
    language: str = "zh",
    overwrite: bool = False,
    build_profile: str = "lite",
    idempotency_key: str = "",
    wait_seconds: int = 30,
) -> dict:
    """创建价值链草稿（付费、异步）：持续跟踪/语义检索/洞察的基础设施。

    USE WHEN: 用户要“持续跟踪一家公司/行业”“每周盯一下”“做竞争分析/出报告”，
    并且已经从 search_cvc_companies 得到用户确认的公司主体。

    DO NOT USE WHEN:
    - 用户只要一次性看最近动态 → search_intelligence（免费）。
    - 还没有 search_cvc_companies 的结果 → 先搜索并让用户确认主体。
    - 已有同名价值链 → 用 list_cvc_models_tool 找到 cvc_id 后 rebuild。

    PREREQUISITES: 至少 1 家公司（来自 search_cvc_companies）；cvc:write 权限。
    COST: lite 1.0 credit（约 3-5 分钟）/ full 2.5 credits（约 10 分钟）；每个租户首条免费。
    LATENCY: 创建秒级，构建异步（lite ≈3-5 分钟 / full ≈10 分钟）。
    RETURNS: cvc_id + next_step(rebuild) + free_warmup（构建期间可先看的免费能力）
    + recommendations。
    NEXT: 立即调用 rebuild_cvc_model 启动构建，并告诉用户“约 3/10 分钟 + 完成邮件”，
    不要在同一轮对话里阻塞等待；期间可先看 casee_get_cvc_dashboard /
    casee_get_market_trends。

    Create a CVC model draft (then call rebuild_cvc_model to start building).

    Uses the async job endpoint first (``POST /cvc-models/creation-jobs``) and
    polls up to ``wait_seconds``. If the job is still running, call
    ``get_cvc_creation_job`` with its ``job_id``. Old deployments without async
    endpoints automatically fall back to the synchronous ``POST /cvc-models``.

    ⚠️ Building takes about **3 minutes** (lite) or **10 minutes** (full); the
    platform emails the tenant's registered address when it finishes. Tell the
    user this and do not block the conversation waiting for completion.

    Args:
        name: CVC name (unique per tenant)
        companies: list of company names, or objects
            {name, name_en, website, products[], markets[], competitors[],
            customers[], partners[]} — pass the companies the user confirmed
            (all by default) from ``search_cvc_companies``. Company names are
            enriched by the backend from the latest search candidates; pass an
            empty list to explicitly remove that entity category.
        description: research goal / description
        query_type: industry | company | product (metadata only)
        query_topic: industry/theme keywords (recommended; required when only
            company names are supplied, otherwise phase-1 relevance filtering
            may drop every intelligence item)
        search_input: retrieval keywords (defaults to name)
        language: zh | en
        overwrite: replace an existing CVC with the same name (dangerous)
        build_profile: lite (default, 1.0 credit, ~3 min, 5 core companies) |
            full (2.5 credits, ~10 min, exhaustive collection)
        idempotency_key: optional client-generated key (8-128 chars)
        wait_seconds: max seconds to wait for creation, 0-180 (0 returns job_id immediately)

    Returns:
        dict: {ok, data: {cvc_id, status, build_readiness, warnings,
              build_status_url, phase_plan, poll, next_actions, user_message,
              next_step}, meta}
        If the job is still running: {ok, data: {job_id, kind, status, poll,
              next_actions, user_message}, meta}
    """
    title = (name or "").strip()
    if not title:
        return _report_invalid("name is required", {"name": name})
    normalized: list = []
    for idx, item in enumerate(companies or []):
        if isinstance(item, str) and item.strip():
            normalized.append(item.strip())
        elif isinstance(item, dict) and str(item.get("name") or "").strip():
            normalized.append(item)
        else:
            return _report_invalid(
                f"companies[{idx}] must be a non-empty name or an object with name",
                {"index": idx},
            )
    if not normalized:
        return _report_invalid(
            "companies must contain at least 1 company; call search_cvc_companies first",
            {"companies": companies},
        )
    if len(normalized) > 20:
        return _report_invalid("companies supports at most 20 entries", {"count": len(normalized)})
    if language not in ("zh", "en"):
        return _report_invalid("language must be zh or en", {"language": language})
    if idempotency_key and not 8 <= len(idempotency_key) <= 128:
        return _report_invalid(
            "idempotency_key must be 8-128 characters", {"length": len(idempotency_key)},
        )
    if not 0 <= wait_seconds <= 180:
        return _report_invalid("wait_seconds must be between 0 and 180", {"wait_seconds": wait_seconds})
    profile = "lite" if str(build_profile or "").strip().lower() == "lite" else "full"
    eta_minutes = 5 if profile == "lite" else 10
    client = get_report_client()

    def _decorate(data):
        if isinstance(data, dict):
            if profile == "lite":
                data["user_message"] = (
                    "价值链已创建并等待构建：lite 快速建链约 3-5 分钟"
                    "（full 全量约 10 分钟）；完成后系统会向注册邮箱发送邮件通知。"
                )
            else:
                data["user_message"] = (
                    "价值链已创建并等待构建：full 全量约 10 分钟；"
                    "完成后系统会向注册邮箱发送邮件通知。"
                )
            data["build_profile"] = profile
            data["next_step"] = {
                "tool": "rebuild_cvc_model",
                "args": {
                    "cvc_id": data.get("cvc_id"),
                    "confirm": True,
                    "dry_run": False,
                    "backup": True,
                },
                "hint": f"启动构建后立即告知用户约 {eta_minutes} 分钟 + 邮件通知，不要阻塞等待",
            }
            cvc_id = data.get("cvc_id")
            if cvc_id:
                data["free_warmup"] = [
                    {"tool": "casee_get_cvc_dashboard", "args": {"cvc_id": cvc_id},
                     "why": "构建期间先看已入库资讯与实体（免费）",
                     "cost_credits": 0, "eta_seconds": 5},
                    {"tool": "casee_get_market_trends", "args": {"cvc_id": cvc_id},
                     "why": "构建期间先看市场热度与话题分布（免费）",
                     "cost_credits": 0, "eta_seconds": 5},
                ]
        return data

    def call():
        body = {
            "name": title,
            "description": description or "",
            "search_input": search_input or title,
            "query_type": query_type or "industry",
            "query_topic": query_topic or "",
            "language": language,
            "overwrite": bool(overwrite),
            "build_profile": profile,
            "companies": normalized,
        }
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            created = client.request(
                "POST", "/cvc-models/creation-jobs",
                json_body=body, extra_headers=headers, timeout=30.0,
            ) or {}
        except Exception as exc:  # noqa: BLE001 - old deployment fallback
            if not _is_async_unavailable(exc):
                raise
            return _decorate(client.request(
                "POST", "/cvc-models", json_body=body,
                extra_headers=headers, timeout=120.0,
            ))
        job_id = str(created.get("job_id") or "")
        if not job_id or wait_seconds <= 0:
            return _job_pending_payload(created, tool="get_cvc_creation_job", kind="cvc_create")
        view = _poll_cvc_job(client, "cvc_create", job_id, wait_seconds)
        if str(view.get("status") or "") == "succeeded":
            return _decorate(view.get("result") or {})
        return _job_pending_payload(view, tool="get_cvc_creation_job", kind="cvc_create")

    result = _report_call(call, client)
    if isinstance(result, dict) and result.get("ok"):
        data = result.get("data") or {}
        cvc_id = data.get("cvc_id") or ""
        _attach_recommendations(result, [
            _recommendation(
                "start_build",
                kind="next_step", priority="high",
                message=(f"立即调用 rebuild_cvc_model 启动构建；告知用户约 {eta_minutes} 分钟 + "
                         "完成邮件，不要阻塞对话。"),
                tool="rebuild_cvc_model",
                args={"cvc_id": cvc_id or "<cvc_id>", "confirm": True,
                      "dry_run": False, "backup": True,
                      "build_profile": profile},
                eta_seconds=300 if profile == "lite" else 600,
                show_once_per_session=False,
            ),
            _recommendation(
                "warmup_while_building",
                kind="capability", priority="medium",
                message="构建期间可先看免费看板与市场趋势，避免用户空等。",
                tool="casee_get_cvc_dashboard",
                args={"cvc_id": cvc_id or "<cvc_id>"},
                cost_credits=0, eta_seconds=5,
            ),
        ], into_data=True)
    return result


@mcp.tool()
def get_company_search_job(job_id: str) -> dict:
    """Poll an async company-search job created by ``search_cvc_companies``.

    Args:
        job_id: job id returned by ``search_cvc_companies`` (csjob-...)

    Returns:
        dict: {ok, data: {job_id, kind, status, result, error, poll}, meta}
        ``result`` is the same payload as the synchronous search when
        ``status="succeeded"``.
    """
    if not job_id or not str(job_id).strip():
        return _report_invalid("job_id is required", {"job_id": job_id})
    client = get_report_client()
    return _report_call(
        lambda: client.request("GET", f"/companies/search-jobs/{job_id}", timeout=30.0),
        client,
    )


@mcp.tool()
def get_cvc_creation_job(job_id: str) -> dict:
    """Poll an async CVC-creation job created by ``create_cvc_model``.

    Args:
        job_id: job id returned by ``create_cvc_model`` (cvcjob-...)

    Returns:
        dict: {ok, data: {job_id, kind, status, result, error, poll}, meta}
        ``result`` contains ``cvc_id`` / ``build_readiness`` when
        ``status="succeeded"``.
    """
    if not job_id or not str(job_id).strip():
        return _report_invalid("job_id is required", {"job_id": job_id})
    client = get_report_client()
    return _report_call(
        lambda: client.request("GET", f"/cvc-models/creation-jobs/{job_id}", timeout=30.0),
        client,
    )


@mcp.tool()
def get_cvc_build_status(
    cvc_id: str,
    wait_seconds: int = 0,
    include_logs: bool = True,
    log_since: int = 0,
) -> dict:
    """Poll a CVC build: phase progress, current step, failure hints and next actions.

    Use ``wait_seconds`` (0-25) for long-polling. Report progress to the user
    with ``status_label`` + ``phases[]`` + ``phase_detail``; on failure read
    ``failure.hint`` / ``next_actions`` and do not rebuild without consent.

    Args:
        cvc_id: CVC model id (cvc_xxx)
        wait_seconds: long-poll wait, 0-25 seconds (state change returns early)
        include_logs: include build logs (last 20 entries are returned)
        log_since: incremental log cursor; use ``log_next_since`` from the last call

    Returns:
        dict: {ok, data: {status, status_label, build_progress, current_phase,
              phase_name, phase_progress, phase_detail, phases[], failure,
              build_readiness, poll, next_actions, build_logs, log_total,
              log_next_since, intelligence_count, fuseki_triples,
              visibility, is_read_only, owner_tenant_id, access_role},
              meta}

        ``tenant_id`` is the calling API key's tenant; ``owner_tenant_id`` is the
        data owner (public Demo sample -> ``demo_public`` with ``is_read_only``).
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if not 0 <= wait_seconds <= 25:
        return _report_invalid("wait_seconds must be between 0 and 25", {"wait_seconds": wait_seconds})
    if log_since < 0:
        return _report_invalid("log_since must be >= 0", {"log_since": log_since})
    client = get_report_client()

    def call():
        data = client.request(
            "GET", f"/cvc-models/{cvc_id}/build-status",
            params={
                "wait_seconds": int(wait_seconds),
                "include_logs": bool(include_logs),
                "log_since": int(log_since),
            },
        )
        if isinstance(data, dict) and include_logs:
            logs = data.get("build_logs") or []
            # 控制上下文体积：只回传最近 20 条，游标供下次增量拉取
            data["build_logs"] = logs[-20:]
        return data

    return _report_call(call, client)


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
    return _safe_call(sdk_cvc_stats, cvc_model_id, **_caller_key_kwargs())


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
    return _safe_call(sdk_cvc_cache_purge, cvc_model_id, **_caller_key_kwargs())


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
    return _safe_call(sdk_cvc_cleanup_status, cvc_model_id, **_caller_key_kwargs())


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
    return _safe_call(sdk_cvc_reindex, cvc_model_id, **_caller_key_kwargs())


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
        **_caller_key_kwargs(),
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
    return _safe_call(
        sdk_intelligence_purge_status, task_id or None,
        **_caller_key_kwargs(),
    )


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
    # REST 包装工具同样要过对外投影：CVC/情报数据里的信源与采集痕迹不可下发
    return sanitize_payload({"ok": True, "data": data, "meta": meta})


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
        "alternatives": list(_ALTERNATIVES.get("VALIDATION_ERROR", [])),
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
              error_info, billing: {charge_status, credits_charged,
              credits_refunded, tx_id, settled_at}, next_poll_after_ms}, meta}
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
        for key in ("url", "api_url", "pdf_url", "document_pdf_url"):
            if isinstance(data, dict) and data.get(key):
                data[key] = client.absolute_url(data[key])
        return data

    result = _report_call(call, client)
    if not result.get("ok") and result.get("error_code") == "TEMPLATE_TOO_LARGE":
        # The backend refuses to inline an oversized HTML snapshot (HTTP 413,
        # code 40015). Retrying can never succeed; point the agent at the
        # download endpoint instead of forcing a direct OpenAPI call.
        details = result.setdefault("details", {})
        details["fallback_tool"] = "casee_download_report"
        details["fallback_args"] = {"job_id": job_id, "format": "html"}
        details.setdefault("max_share_bytes_note", "Report HTML exceeds the share snapshot limit")
        result["hint"] = (
            "The report is too large to inline as a share snapshot (TEMPLATE_TOO_LARGE/413). "
            "Do not retry share_report. Call casee_download_report(job_id=..., format='html'), "
            "save the file locally and distribute it; or generate with fewer sections/charts. "
            "For 'md' jobs use format='md'."
        )
    return result


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


@mcp.tool(
    title="重建价值链（付费、异步；lite 约 3-5 分钟 / full 约 10 分钟）",
    annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    meta={"tier": "T3", "cost_credits": 1.0, "eta_seconds": 300,
          "requires_user_confirm": True},
)
def rebuild_cvc_model(
    cvc_id: str,
    confirm: bool = False,
    backup: bool = True,
    dry_run: bool = True,
    mode: str = "full",
    build_profile: str = "",
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
        build_profile: optional override lite (1.0 credit, ~3 min) |
            full (2.5 credits, ~10 min); empty keeps the model's stored profile
        idempotency_key: optional client-generated key (8-128 chars)

    Returns:
        dict: {ok, data: {task_id?, status?, will_delete?, safety?, poll_url?},
              meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if mode not in ("full", "non_destructive"):
        return _report_invalid("mode must be full or non_destructive", {"mode": mode})
    requested_profile = str(build_profile or "").strip().lower()
    if requested_profile and requested_profile not in ("full", "lite"):
        return _report_invalid("build_profile must be full or lite", {"build_profile": build_profile})
    if idempotency_key and not 8 <= len(idempotency_key) <= 128:
        return _report_invalid(
            "idempotency_key must be 8-128 characters",
            {"length": len(idempotency_key)},
        )
    client = get_report_client()
    execute = bool(confirm and not dry_run)

    def call():
        body = {
            "confirm": bool(confirm),
            "dry_run": not execute,
            "backup": bool(backup),
            "mode": mode,
        }
        if requested_profile:
            body["build_profile"] = requested_profile
        data = client.request(
            "POST", f"/cvc-models/{cvc_id}/rebuild",
            json_body=body,
            extra_headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        if not execute and isinstance(data, dict):
            data["requires_confirm"] = True
            data["executed"] = False
        elif isinstance(data, dict):
            data["executed"] = True
        if requested_profile and isinstance(data, dict):
            data["build_profile"] = requested_profile
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
    name="cvc_creation",
    description="Create a CVC from a bare analysis goal: search companies → user confirms (all by default) → create → rebuild → ~10 min + completion email.",
)
def cvc_creation_prompt(query: str = "", cvc_name: str = "") -> str:
    """Reusable CVC-creation recipe exposed via prompts/list (v1.6)."""
    goal = query or "<analysis goal>"
    name = cvc_name or "<suggested_cvc_name>"
    return f"""Use the CaSee MCP tools to turn an analysis goal into a buildable CVC.
Goal: `{goal}`

Follow this order; never invent companies:

1. search_cvc_companies(query="{goal}", limit=8, wait_seconds=240)
   - If data.status is queued/running: call get_company_search_job(job_id=...)
     and poll every 5 seconds until succeeded/failed.
   - If items is empty: ask the user for a more specific industry/company name,
     or for 3-8 company names. Do not fabricate.
   - Show the list as: index. name | main_business | website
   - Say explicitly: "已默认全选；回复要去掉的序号即可，回复“全部保留/继续”也可以"
2. Wait for the user to confirm. "全部/都行/继续" means keep all; otherwise
   drop the indices the user excluded.
3. create_cvc_model(
     name="{name}",
     companies=<selected items, keep products/markets/competitors>,
     query_topic="{goal}",
     query_type="industry",
     build_profile="lite",
     wait_seconds=30,
   )
   - If data.status is queued/running: call get_cvc_creation_job(job_id=...)
     and poll every 5 seconds until succeeded/failed.
   - If data.build_readiness.ready is false or data.warnings is non-empty:
     tell the user what is missing before building (it may fail in phase 1).
4. rebuild_cvc_model(cvc_id=<cvc_id>, confirm=true, dry_run=false, backup=true,
                     build_profile="lite")
5. Immediately tell the user:
   "价值链已开始构建，lite 快速建链约 3-5 分钟（full 全量约 10 分钟）；完成后系统会向你的注册邮箱发送邮件通知
    （若长时间未收到，可直接回来让我查进度）。期间你可以继续问其他问题。"
   Do NOT block the conversation and do NOT poll every few seconds.
6. When the user asks for progress later: get_cvc_build_status(cvc_id, wait_seconds=15)
   - Report status_label + phases[] + phase_detail
     (e.g. "阶段1 竞争情报采集 80%：全文抓取中").
   - status=failed → read failure.hint / next_actions; explain it and ask before
     any destructive rebuild.

Building is asynchronous and consumes Credits; a failed build is refunded.
"""


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

0. casee_get_credits — confirm balance covers the generation (1 Credit) and,
   when reconciling, keep {{balance, ledger_cursor}} for casee_get_credit_ledger.
1. list_report_templates — pick a template ({template} if provided).
2. plan_report(cvc_id="{target}", template_id="{template}", time_range="{time_range}").
   - If compatibility.compatible is false: STOP, report the reasons
     (MODEL_NOT_COMPLETED / NO_AVAILABLE_CHARTS) and ask whether to rebuild.
   - If charts.available is 0: STOP and report data_gaps.
   - If recommended_time_range differs: rerun plan with it before generating.
3. generate_report(..., output_formats=["html"]) with a unique idempotency_key.
4. Poll get_report_job(job_id, wait_ms=30000) until status is terminal;
   wait meta.next_poll_after_ms between calls (about 10 minutes total).
5. If status is partial: the job is already charged (billing.charge_status=charged).
   List the missing sections first, then decide with the user whether a retry is
   worth another 1 Credit. If failed: report error_code, hint, retryable and
   request_id; the reservation is refunded automatically
   (billing.charge_status=refunded), so retry only when retryable is true.
6. share_report(job_id) and give the user the absolute url (and pdf_url).
   If share_report returns error_code=TEMPLATE_TOO_LARGE (HTTP 413), do NOT
   retry: call casee_download_report(job_id, format="html") instead, tell the
   user the local file path (data.saved_path) and share that file.
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
   query scope (category / region / language); never fill the gap with
   invented facts.
6. Citation discipline: quote only from title / description / abstract plus
   the item's citation.url; never quote summary — it may be AI-expanded text
   (summary_origin=llm_expanded, or unknown on legacy items) for reading
   comprehension only. Items flagged content_flags=["anti_bot_page"] returned
   an anti-bot detection page instead of the real article; never quote them.
"""


from . import openapi_tools as _openapi_tools  # noqa: E402,F401  (registers v1.7 tools)
from . import guidance as _guidance  # noqa: E402,F401  (registers casee_guide / casee_plan_task)


def main():
    """MCP server entry point."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8100"))
    path = os.environ.get("MCP_PATH", "/mcp")

    from .startup_check import is_strict, run_startup_self_check

    if not run_startup_self_check(transport) and is_strict():
        raise SystemExit(1)

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