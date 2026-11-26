"""v1.7 OpenAPI coverage tools for the CaSee MCP server.

The v1.4-v1.6 tool set covered retrieval, CVC lifecycle and the report
generate/share flow. Several OpenAPI capabilities still required direct HTTP
calls, which breaks the "controlled toolset" contract:

- billing: ``GET /credits`` was only incidental inside ``casee_health`` and
  ``GET /credits/transactions`` (ledger reconciliation) was not exposed;
- quota: ``GET /quota`` (scopes / rate-limit watermarks) was not exposed;
- artifacts: ``GET /reports/jobs/{job_id}/download`` was not exposed, so an
  oversized report (``share_report`` -> 413 ``TEMPLATE_TOO_LARGE``) forced a
  direct download call;
- charts: catalog / detail / preview / access-code + public bundle;
- report templates: detail / download / create / update / delete / validate;
- CVC inspection: model meta / companies / entities / entity detail /
  ontology / intelligence search & detail / export;
- business insights: home overview / dashboard / market trends / enterprise /
  market / customer insight;
- API discovery: ``/meta/catalog``, ``/meta/endpoints/{id}``, ``/meta/mcp-tools``.

Every tool returns the same ``{ok, data, meta}`` envelope as the v1.4 report
tools and never raises. Binary / large text artifacts are written to disk and
returned as metadata; pass ``inline=True`` to also get (truncated) content.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .server import (
    _attach_recommendations,
    _CONFIRM_TOKEN_TTL_SECONDS,
    _recommendation,
    _report_call,
    _report_invalid,
    get_report_client,
    issue_confirm_token,
    mcp,
    verify_confirm_token,
)

_TIME_RANGES = ("week", "month", "quarter")
_TIME_RANGES_AUTO = ("week", "month", "quarter", "auto")
_LANGUAGES = ("zh", "en")
_ENTITY_TYPES = (
    "all", "company", "competitor", "product", "customer", "partner",
    "market", "belief", "attribute",
)
_CONCRETE_ENTITY_TYPES = _ENTITY_TYPES[1:]
_ONTOLOGY_FORMATS = ("turtle", "rdfxml", "jsonld", "nt")
_ONTOLOGY_EXT = {"turtle": "ttl", "rdfxml": "rdf", "jsonld": "jsonld", "nt": "nt"}
_REPORT_FORMATS = ("html", "md", "deck")
_TEMPLATE_STATUS = ("draft", "active")


# ---------------------------------------------------------------------------
# artifact helpers
# ---------------------------------------------------------------------------

def _safe_component(value: Any, fallback: str = "artifact") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return (cleaned or fallback)[:80]


def _download_root() -> Path:
    explicit = os.environ.get("CASEE_DOWNLOAD_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else Path(tempfile.gettempdir()) / "casee-downloads"


def _resolve_target(save_path: str, filename: str) -> Path:
    """Resolve save_path (file or directory) into an absolute target path."""
    if not save_path:
        target = _download_root() / filename
    else:
        raw = save_path.strip()
        candidate = Path(raw).expanduser()
        if raw.endswith(os.sep) or candidate.is_dir():
            target = candidate / filename
        else:
            target = candidate
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _write_atomic(target: Path, content: bytes) -> None:
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(content)
    tmp.replace(target)


def _artifact_payload(
    content: bytes,
    meta: dict,
    *,
    save_path: str,
    filename: str,
    inline: bool,
    max_inline_chars: int,
) -> dict:
    """Persist an artifact and return path / hash / size (+ optional content)."""
    target = _resolve_target(save_path, filename)
    _write_atomic(target, content)
    total = str(meta.get("total_count") or "")
    payload: dict = {
        "saved_path": str(target),
        "filename": target.name,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": str(meta.get("content_type") or "").split(";")[0],
        "etag": meta.get("etag") or "",
    }
    if total.isdigit():
        payload["total_count"] = int(total)
    if inline:
        text = content.decode(meta.get("charset") or "utf-8", errors="replace")
        truncated = max_inline_chars >= 0 and len(text) > max_inline_chars
        payload["content"] = text[:max_inline_chars] if max_inline_chars >= 0 else text
        payload["truncated"] = truncated
        if truncated:
            payload["content_chars"] = len(text)
    return payload


def _path_id(value: str, name: str) -> str:
    """Reject values that would break a single URL path segment."""
    text = str(value or "").strip()
    if not text or "/" in text or "?" in text or "#" in text or " " in text:
        return f"{name} must be a non-empty single path segment"
    return ""


def _condense_bundle(bundle: dict, *, include_embed: bool, save_path: str) -> dict:
    """Drop the full embed HTML from the tool result unless explicitly asked."""
    if not isinstance(bundle, dict):
        return bundle
    data = dict(bundle)
    embed = data.get("embed")
    if isinstance(embed, dict):
        html = embed.get("html") or embed.get("html_full") or ""
        if html and not include_embed:
            embed = {k: v for k, v in embed.items() if k not in ("html", "html_full")}
            embed["html_omitted"] = True
            embed["html_chars"] = len(html)
            if save_path:
                target = _resolve_target(
                    save_path, f"{_safe_component(data.get('ca_code'), 'chart')}.html",
                )
                _write_atomic(target, html.encode("utf-8"))
                embed["html_saved_path"] = str(target)
        data["embed"] = embed
    return data


# ---------------------------------------------------------------------------
# billing / quota
# ---------------------------------------------------------------------------

@mcp.tool(
    title="余额与单价（免费）",
    annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 2},
)
def casee_get_credits() -> dict:
    """读余额、免费额度与每个操作的单价（免费、无副作用）。

    USE WHEN: 任何收费操作之前；用户问“多少钱 / 我还剩多少 / 会不会扣费”。
    DO NOT USE WHEN: 只是想看历史流水（→ casee_get_credit_ledger）。
    PREREQUISITES: 无。COST: 0 credit。LATENCY: 约 2 秒。
    RETURNS: balance / buckets(free, monthly, paid) / operations 单价 +
    first_cvc_free 提示 + recommendations。
    NEXT: 余额不足时按 recommendations 使用免费能力，或联系管理员充值。

    Read the tenant Credits balance and per-operation unit prices.

    Call this before any charged operation (generate_report, rebuild, insights)
    to avoid a 402 mid-flight. Does not consume Credits.

    Returns:
        dict: {ok, data: {balance, pending_credits, buckets: {free, monthly,
              paid}, ledger_cursor, unit, operations: {op: {credits,
              available}}, as_of}, meta}
    """
    client = get_report_client()
    result = _report_call(
        lambda: client.request(
            "GET", "/credits",
            extra_headers={"X-Casee-Call-Purpose": "balance_precheck"},
        ),
        client,
    )
    if isinstance(result, dict) and result.get("ok"):
        data = result.get("data")
        if isinstance(data, dict):
            data["first_cvc_free"] = "每个租户首条 CVC 免费（lite 1.0 / full 2.5 credits 减免）"
            data["free_capabilities"] = [
                "search_intelligence", "analyze_trend", "aggregate_intelligence",
                "casee_get_cvc_dashboard", "casee_get_market_trends",
                "list_report_templates", "plan_report",
            ]
        _attach_recommendations(result, [
            _recommendation(
                "inspect_tenant_state",
                kind="next_step", priority="low",
                message="不确定从哪开始：先看租户总览（已有模型、构建中任务、余额）。",
                tool="casee_home_overview", args={}, eta_seconds=2,
            ),
        ], into_data=True)
    return result


@mcp.tool()
def casee_get_credit_ledger(
    operation: str = "",
    status: str = "all",
    direction: str = "all",
    cvc_id: str = "",
    job_id: str = "",
    since_id: int = 0,
    before_id: int = 0,
    cursor: str = "",
    older_cursor: str = "",
    page: int = 1,
    start_time: str = "",
    end_time: str = "",
    page_size: int = 20,
) -> dict:
    """Read the Credits ledger (reserve / confirm / refund) for reconciliation.

    Pair with ``casee_get_credits``: record ``balance`` + ``ledger_cursor``
    before an operation, then page through the ledger or pull ``since_id``
    deltas and re-read the balance to verify the charge/refund.

    Paging (choose one):
    - ``page`` + ``page_size`` — page numbers; ``data.pagination.total_pages``
      tells you how many pages cover the whole filtered history.
    - ``before_id`` / ``cursor`` / ``older_cursor`` — pass the previous
      ``data.older_cursor``.
    - ``since_id`` — ascending incremental reconciliation.

    Filter ``direction=refund`` (equivalent to ``status=rolled_back``) to see
    refunded rows. Do not infer "not charged" from a job's ``billing`` alone:
    always cross-check the ledger for a refund entry.

    Args:
        operation: billing operation filter, comma-separated (e.g. report_generate)
        status: all | pending | confirmed | rolled_back
        direction: all | refund (rolled_back) | charge (amount<0) | grant (amount>0)
        cvc_id: filter by CVC model
        job_id: filter by report job id
        since_id: incremental cursor (ascending); 0 = unset
        before_id: history pagination cursor (descending); 0 = unset
        cursor: alias of before_id (pass the previous ``older_cursor``)
        older_cursor: alias of before_id (pass the response field directly)
        page: 1-based page number (only without cursors)
        start_time: created-at lower bound (ISO8601)
        end_time: created-at upper bound (ISO8601)
        page_size: 1-500 (default 20)

    Returns:
        dict: {ok, data: {items, filters, summary: {count, refunded_count,
              charge_count, grant_count, pending_count, first_tx_id,
              last_tx_id, ...}, pagination, next_cursor, older_cursor,
              has_more, generated_at}, meta}
    """
    if status not in ("all", "pending", "confirmed", "rolled_back"):
        return _report_invalid(
            "status must be one of all|pending|confirmed|rolled_back",
            {"status": status},
        )
    if direction not in ("all", "refund", "charge", "grant"):
        return _report_invalid(
            "direction must be one of all|refund|charge|grant",
            {"direction": direction},
        )
    if direction == "refund" and status not in ("all", "rolled_back"):
        return _report_invalid(
            "direction=refund only works with status=all or rolled_back",
            {"direction": direction, "status": status},
        )
    if direction in ("charge", "grant") and status == "rolled_back":
        return _report_invalid(
            f"direction={direction} cannot be combined with status=rolled_back",
            {"direction": direction, "status": status},
        )
    if not 1 <= page_size <= 500:
        return _report_invalid("page_size must be between 1 and 500", {"page_size": page_size})
    if page < 1:
        return _report_invalid("page must be >= 1", {"page": page})
    if since_id and before_id:
        return _report_invalid(
            "since_id and before_id cannot be used together",
            {"since_id": since_id, "before_id": before_id},
        )
    if since_id < 0 or before_id < 0:
        return _report_invalid(
            "since_id / before_id must be >= 0",
            {"since_id": since_id, "before_id": before_id},
        )
    alias_values = [(name, value) for name, value in (
        ("cursor", cursor), ("older_cursor", older_cursor),
    ) if str(value or "").strip()]
    if len(alias_values) > 1:
        return _report_invalid(
            "cursor and older_cursor are aliases; pass only one",
            {"cursor": cursor, "older_cursor": older_cursor},
        )
    if alias_values:
        name, value = alias_values[0]
        try:
            parsed_cursor = int(str(value).strip())
        except (TypeError, ValueError):
            return _report_invalid(
                f"{name} must be an integer transaction id (older_cursor)",
                {name: value},
            )
        if parsed_cursor < 1:
            return _report_invalid(f"{name} must be >= 1", {name: parsed_cursor})
        if before_id and int(before_id) != parsed_cursor:
            return _report_invalid(
                "cursor and before_id point to different transactions",
                {name: parsed_cursor, "before_id": before_id},
            )
        before_id = parsed_cursor
    if page > 1 and (since_id or before_id):
        return _report_invalid(
            "page cannot be combined with since_id / before_id; choose one paging mode",
            {"page": page, "since_id": since_id, "before_id": before_id},
        )
    client = get_report_client()
    params = {
        "operation": operation or None,
        "status": None if status == "all" else status,
        "direction": None if direction == "all" else direction,
        "cvc_id": cvc_id or None,
        "job_id": job_id or None,
        "since_id": since_id or None,
        "before_id": before_id or None,
        "page": page if page > 1 else None,
        "start_time": start_time or None,
        "end_time": end_time or None,
        "page_size": page_size,
    }
    return _report_call(
        lambda: client.request("GET", "/credits/transactions", params=params), client,
    )


@mcp.tool()
def casee_get_quota() -> dict:
    """Read the API key quota self-check: scopes, rate-limit watermarks, products.

    Use this when diagnosing 429s or checking what the current key can call.
    Unlike ``casee_health`` (connectivity probe) this is the authoritative
    quota/scope snapshot.

    Returns:
        dict: {ok, data: {key_id, key_prefix, tenant_code, scopes, rate_limit,
              expires_at, cvc_models: {accessible}, credits, products,
              server_time}, meta}
    """
    client = get_report_client()
    return _report_call(lambda: client.request("GET", "/quota"), client)


# ---------------------------------------------------------------------------
# API discovery
# ---------------------------------------------------------------------------

@mcp.tool()
def casee_list_endpoints(group: str = "", q: str = "", detail: bool = False) -> dict:
    """List CaSee OpenAPI endpoints with scopes, quotas and recommended order.

    Use this when you are unsure which capability exists or need the endpoint
    id for ``casee_get_endpoint``.

    Args:
        group: discover | home | cvc | insights | charts | reports
        q: keyword filter
        detail: true returns full parameter tables

    Returns:
        dict: {ok, data: {groups, endpoints, next, ...}, meta}
    """
    client = get_report_client()
    params = {"group": group or None, "q": q or None, "detail": detail or None}
    return _report_call(lambda: client.request("GET", "/meta/catalog", params=params), client)


@mcp.tool()
def casee_get_endpoint(endpoint_id: str) -> dict:
    """Read one endpoint's parameters, outputs, error codes and changelog.

    Args:
        endpoint_id: e.g. reports.download, charts.catalog, cvc.rebuild

    Returns:
        dict: {ok, data: {endpoint_id, method, path, parameters, outputs,
              error_codes, ...}, meta}
    """
    error = _path_id(endpoint_id, "endpoint_id")
    if error:
        return _report_invalid(error, {"endpoint_id": endpoint_id})
    client = get_report_client()
    return _report_call(
        lambda: client.request("GET", f"/meta/endpoints/{quote(endpoint_id, safe='')}"),
        client,
    )


@mcp.tool()
def casee_list_mcp_tools() -> dict:
    """Return the backend's declarative MCP tool snapshot (tools/resources/prompts).

    Useful for verifying that this MCP server covers every tool the backend
    advertises.

    Returns:
        dict: {ok, data: {server, tools, resources, prompts}, meta}
    """
    client = get_report_client()
    return _report_call(lambda: client.request("GET", "/meta/mcp-tools"), client)


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------

@mcp.tool()
def casee_search_charts(
    domain: str = "",
    function_category: str = "",
    page_type: str = "",
    report_role: str = "",
    q: str = "",
    cvc_id: str = "",
    time_range: str = "month",
    detail: bool = False,
    with_screenshot: bool = False,
) -> dict:
    """Search the chart capability dictionary (which charts can feed data).

    Pass ``cvc_id`` to also get per-chart availability for that CVC. Analysis
    models (CPM/SWOT/BCG/PESTEL...) use ``domain=analysis``.

    Args:
        domain: competition | market | customer | ontology | valuechain | analysis
        function_category: functional category filter
        page_type: competition | market | customer | battlecard
        report_role: e.g. competition, cpm, pestel
        q: keyword over code / names / description / tags
        cvc_id: optional availability check
        time_range: week | month | quarter
        detail: true returns full chart metadata
        with_screenshot: include screenshot URLs

    Returns:
        dict: {ok, data: {catalog_version, total, filters, usage_guide,
              charts[]}, meta}
    """
    if time_range not in _TIME_RANGES:
        return _report_invalid(
            f"time_range must be one of {_TIME_RANGES}", {"time_range": time_range},
        )
    client = get_report_client()
    params = {
        "domain": domain or None,
        "function_category": function_category or None,
        "page_type": page_type or None,
        "report_role": report_role or None,
        "q": q or None,
        "cvc_id": cvc_id or None,
        "time_range": time_range,
        "detail": detail or None,
        "with_screenshot": with_screenshot or None,
    }
    return _report_call(lambda: client.request("GET", "/charts/catalog", params=params), client)


@mcp.tool()
def casee_get_chart(ca_code: str, cvc_id: str = "", time_range: str = "month") -> dict:
    """Read one chart's metadata, field contract and optional availability.

    Args:
        ca_code: chart code from casee_search_charts (e.g. ca.competition.threat_matrix)
        cvc_id: optional; check availability under this CVC
        time_range: week | month | quarter

    Returns:
        dict: {ok, data: {ca_code, name_zh, fields, access_guide,
              availability?}, meta}
    """
    error = _path_id(ca_code, "ca_code")
    if error:
        return _report_invalid(error, {"ca_code": ca_code})
    if time_range not in _TIME_RANGES:
        return _report_invalid(
            f"time_range must be one of {_TIME_RANGES}", {"time_range": time_range},
        )
    client = get_report_client()
    params = {"cvc_id": cvc_id or None, "time_range": time_range}
    return _report_call(
        lambda: client.request("GET", f"/charts/catalog/{quote(ca_code, safe='')}", params=params),
        client,
    )


@mcp.tool()
def casee_preview_chart(
    ca_code: str,
    cvc_id: str = "",
    time_range: str = "month",
    language: str = "zh",
    include_embed: bool = False,
    save_path: str = "",
) -> dict:
    """Render a chart preview (sample data or a CVC's real data; no quota cost).

    Args:
        ca_code: chart code from casee_search_charts
        cvc_id: optional; use this CVC's real data (some charts require it)
        time_range: week | month | quarter
        language: zh | en
        include_embed: include the full embed HTML in the response
        save_path: file or directory to save embed HTML when include_embed=false

    Returns:
        dict: {ok, data: {data, render_params, theme, embed, access_guide,
              preview, source}, meta}
    """
    error = _path_id(ca_code, "ca_code")
    if error:
        return _report_invalid(error, {"ca_code": ca_code})
    if time_range not in _TIME_RANGES:
        return _report_invalid(
            f"time_range must be one of {_TIME_RANGES}", {"time_range": time_range},
        )
    if language not in _LANGUAGES:
        return _report_invalid("language must be zh or en", {"language": language})
    client = get_report_client()
    params = {
        "cvc_id": cvc_id or None,
        "time_range": time_range,
        "language": language,
    }

    def call():
        data = client.request(
            "GET", f"/charts/catalog/{quote(ca_code, safe='')}/preview", params=params,
        )
        return _condense_bundle(
            data or {}, include_embed=include_embed, save_path=save_path,
        )

    return _report_call(call, client)


@mcp.tool()
def casee_get_chart_bundle(
    code: str = "",
    cvc_id: str = "",
    chart: str = "",
    time_range: str = "month",
    language: str = "zh",
    ttl_seconds: int = 3600,
    include_embed: bool = False,
    save_path: str = "",
) -> dict:
    """Fetch a chart render bundle, issuing a password-free ``cc-`` code if needed.

    Pass ``code`` when you already have one (from casee_plan_report or a
    previous call); otherwise pass ``cvc_id`` + ``chart`` to issue a new code
    and fetch the bundle in one step.

    Args:
        code: existing cc- access code
        cvc_id: CVC model id (required to issue a new code)
        chart: ca_code (default threat matrix on the backend side)
        time_range: week | month | quarter
        language: zh | en
        ttl_seconds: 60-604800 for a newly issued code
        include_embed: include the full embed HTML
        save_path: file or directory for the embed HTML

    Returns:
        dict: {ok, data: {code, ca_code, data, render_params, theme, embed,
              manifest, narrative, expires_at, ...}, meta}
    """
    if code:
        error = _path_id(code, "code")
        if error:
            return _report_invalid(error, {"code": code})
    else:
        if not cvc_id:
            return _report_invalid(
                "cvc_id is required when code is not provided", {"cvc_id": cvc_id},
            )
        if not 60 <= ttl_seconds <= 604800:
            return _report_invalid(
                "ttl_seconds must be between 60 and 604800",
                {"ttl_seconds": ttl_seconds},
            )
    if time_range not in _TIME_RANGES:
        return _report_invalid(
            f"time_range must be one of {_TIME_RANGES}", {"time_range": time_range},
        )
    if language not in _LANGUAGES:
        return _report_invalid("language must be zh or en", {"language": language})
    client = get_report_client()

    def call():
        issued = None
        access = code
        if not access:
            issued = client.request("POST", "/charts/access-code", json_body={
                "cvc_id": cvc_id,
                "chart": chart or "ca.competition.threat_matrix",
                "time_range": time_range,
                "language": language,
                "ttl_seconds": int(ttl_seconds),
            })
            access = str((issued or {}).get("code") or "")
            if not access:
                raise ValueError("access-code response did not include a code")
        content, _meta = client.request_bytes(
            "GET", f"/public/charts/{quote(access, safe='')}",
            public=True, accept="application/json",
        )
        bundle = json.loads(content.decode("utf-8"))
        bundle = _condense_bundle(
            bundle or {}, include_embed=include_embed, save_path=save_path,
        )
        if issued:
            bundle["access"] = {
                "expires_at": issued.get("expires_at"),
                "bundle": (issued.get("access") or {}).get("bundle"),
                "data_json": (issued.get("access") or {}).get("data_json"),
                "data_csv": (issued.get("access") or {}).get("data_csv"),
                "report_markdown": (issued.get("access") or {}).get("report_markdown"),
            }
        return bundle

    return _report_call(call, client)


# ---------------------------------------------------------------------------
# report templates
# ---------------------------------------------------------------------------

@mcp.tool()
def casee_get_report_template(template_id: str) -> dict:
    """Read a template's sections, chart codes and writing constraints.

    Args:
        template_id: template id from list_report_templates

    Returns:
        dict: {ok, data: {template_id, name_zh, sections: [{id, ca_codes,
              charts, writing_guide...}], source, ...}, meta}
    """
    error = _path_id(template_id, "template_id")
    if error:
        return _report_invalid(error, {"template_id": template_id})
    client = get_report_client()
    return _report_call(
        lambda: client.request("GET", f"/reports/templates/{quote(template_id, safe='')}"),
        client,
    )


@mcp.tool()
def casee_download_report_template(
    template_id: str,
    save_path: str = "",
    inline: bool = True,
    max_inline_chars: int = 200000,
) -> dict:
    """Download a template as Markdown (system templates are serialized).

    Use this to fork a system template into a tenant template:
    download -> edit -> casee_create_report_template.

    Args:
        template_id: template id
        save_path: file or directory (default: temp dir / casee-downloads)
        inline: include the Markdown body in the response
        max_inline_chars: truncate inline content beyond this many characters

    Returns:
        dict: {ok, data: {saved_path, filename, bytes, sha256, media_type,
              etag, content?, truncated?}, meta}
    """
    error = _path_id(template_id, "template_id")
    if error:
        return _report_invalid(error, {"template_id": template_id})
    client = get_report_client()

    def call():
        content, meta = client.request_bytes(
            "GET", f"/reports/templates/{quote(template_id, safe='')}/download",
            accept="text/markdown",
        )
        return _artifact_payload(
            content, meta, save_path=save_path,
            filename=f"{_safe_component(template_id.replace('.', '_'), 'template')}.md",
            inline=inline, max_inline_chars=max_inline_chars,
        )

    return _report_call(call, client)


@mcp.tool()
def casee_create_report_template(
    template_id: str,
    name: str,
    content_md: str,
    summary: str = "",
    status: str = "active",
) -> dict:
    """Upload a tenant template (Markdown + YAML front-matter) after validation.

    Args:
        template_id: tenant-namespaced id (e.g. tenant.acme.cpm_deep)
        name: display name
        content_md: full Markdown template
        summary: optional summary
        status: draft | active

    Returns:
        dict: {ok, data: {template, validation}, meta}
    """
    error = _path_id(template_id, "template_id")
    if error:
        return _report_invalid(error, {"template_id": template_id})
    if not name.strip():
        return _report_invalid("name is required", {"name": name})
    if not content_md.strip():
        return _report_invalid("content_md is required", {"content_md": ""})
    if status not in _TEMPLATE_STATUS:
        return _report_invalid("status must be draft or active", {"status": status})
    client = get_report_client()
    body = {
        "template_id": template_id,
        "name": name,
        "content_md": content_md,
        "status": status,
    }
    if summary:
        body["summary"] = summary
    return _report_call(
        lambda: client.request("POST", "/reports/templates", json_body=body), client,
    )


@mcp.tool()
def casee_update_report_template(
    template_id: str,
    name: str | None = None,
    content_md: str | None = None,
    summary: str | None = None,
    status: str | None = None,
) -> dict:
    """Update a tenant template (system templates are read-only).

    Args:
        template_id: tenant template id
        name: new display name
        content_md: new Markdown body (re-validated)
        summary: new summary
        status: draft | active

    Returns:
        dict: {ok, data: {template}, meta}
    """
    error = _path_id(template_id, "template_id")
    if error:
        return _report_invalid(error, {"template_id": template_id})
    if name is None and content_md is None and summary is None and status is None:
        return _report_invalid(
            "provide at least one of name / content_md / summary / status", {},
        )
    if status is not None and status not in _TEMPLATE_STATUS:
        return _report_invalid("status must be draft or active", {"status": status})
    body: dict = {}
    if name is not None:
        body["name"] = name
    if content_md is not None:
        body["content_md"] = content_md
    if summary is not None:
        body["summary"] = summary
    if status is not None:
        body["status"] = status
    client = get_report_client()
    return _report_call(
        lambda: client.request(
            "PUT", f"/reports/templates/{quote(template_id, safe='')}", json_body=body,
        ),
        client,
    )


@mcp.tool()
def casee_delete_report_template(template_id: str, confirm: bool = False) -> dict:
    """Soft-delete a tenant template (system templates cannot be deleted).

    Args:
        template_id: tenant template id
        confirm: must be true to delete

    Returns:
        dict: {ok, data: {template_id, deleted}, meta}
    """
    error = _path_id(template_id, "template_id")
    if error:
        return _report_invalid(error, {"template_id": template_id})
    if not confirm:
        return _report_invalid(
            "confirm=true is required to delete a template", {"template_id": template_id},
        )
    client = get_report_client()
    return _report_call(
        lambda: client.request(
            "DELETE", f"/reports/templates/{quote(template_id, safe='')}",
        ),
        client,
    )


@mcp.tool()
def casee_validate_report_template(template_id: str, content_md: str) -> dict:
    """Validate a template without saving it; returns per-error codes.

    Use before create/update to catch unknown ca_code, duplicate section ids,
    bad placeholders or size limits.

    Args:
        template_id: target template id (may be temporary)
        content_md: full Markdown template

    Returns:
        dict: {ok, data: {valid, errors: [{code, message, ...}]}, meta}
    """
    error = _path_id(template_id, "template_id")
    if error:
        return _report_invalid(error, {"template_id": template_id})
    if not content_md.strip():
        return _report_invalid("content_md is required", {"content_md": ""})
    client = get_report_client()
    return _report_call(
        lambda: client.request(
            "POST", f"/reports/templates/{quote(template_id, safe='')}/validate",
            json_body={"content_md": content_md},
        ),
        client,
    )


# ---------------------------------------------------------------------------
# report artifacts / access codes
# ---------------------------------------------------------------------------

@mcp.tool()
def casee_download_report(
    job_id: str,
    format: str = "html",
    save_path: str = "",
    inline: bool = False,
    max_inline_chars: int = 200000,
) -> dict:
    """Download a finished report artifact (html / md / deck) to a local file.

    This is the supported fallback when ``share_report`` returns
    ``TEMPLATE_TOO_LARGE`` (HTTP 413): the share snapshot has a size limit,
    but the artifact itself can always be downloaded once the job succeeded.

    Args:
        job_id: finished report job id
        format: html | md | deck (deck is the PPT single-file HTML)
        save_path: file or directory (default: temp dir / casee-downloads)
        inline: include the (possibly truncated) content in the response
        max_inline_chars: truncate inline content beyond this many characters

    Returns:
        dict: {ok, data: {saved_path, filename, bytes, sha256, media_type,
              api_url, content?, truncated?}, meta}
    """
    error = _path_id(job_id, "job_id")
    if error:
        return _report_invalid(error, {"job_id": job_id})
    if format not in _REPORT_FORMATS:
        return _report_invalid(
            f"format must be one of {_REPORT_FORMATS}", {"format": format},
        )
    client = get_report_client()
    suffix = {"html": ".html", "md": ".md", "deck": "-ppt.html"}[format]

    def call():
        content, meta = client.request_bytes(
            "GET", f"/reports/jobs/{quote(job_id, safe='')}/download",
            params={"format": format},
            accept="text/markdown" if format == "md" else "text/html",
        )
        payload = _artifact_payload(
            content, meta, save_path=save_path,
            filename=f"{_safe_component(job_id, 'report')}{suffix}",
            inline=inline, max_inline_chars=max_inline_chars,
        )
        payload["job_id"] = job_id
        payload["format"] = format
        payload["api_url"] = (
            f"{client.base_url}/reports/jobs/{quote(job_id, safe='')}"
            f"/download?format={format}"
        )
        return payload

    return _report_call(call, client)


@mcp.tool()
def casee_issue_report_access_code(
    cvc_id: str,
    template_id: str,
    time_range: str = "auto",
    output_formats: list[str] | None = None,
    language: str = "zh",
    ttl_seconds: int = 600,
) -> dict:
    """Issue a one-time report generation code (no API key, browser page).

    Args:
        cvc_id: CVC model id
        template_id: template id
        time_range: week | month | quarter | auto
        output_formats: subset of ["html", "md", "deck"]
        language: zh | en
        ttl_seconds: 60-1800 (default 600)

    Returns:
        dict: {ok, data: {code, expires_at, report_url}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if not template_id:
        return _report_invalid("template_id is required", {"template_id": template_id})
    if time_range not in _TIME_RANGES_AUTO:
        return _report_invalid(
            f"time_range must be one of {_TIME_RANGES_AUTO}", {"time_range": time_range},
        )
    formats = list(output_formats or ["html"])
    invalid = [f for f in formats if f not in _REPORT_FORMATS]
    if invalid:
        return _report_invalid(
            f"output_formats must be a subset of {_REPORT_FORMATS}", {"invalid": invalid},
        )
    if language not in _LANGUAGES:
        return _report_invalid("language must be zh or en", {"language": language})
    if not 60 <= ttl_seconds <= 1800:
        return _report_invalid(
            "ttl_seconds must be between 60 and 1800", {"ttl_seconds": ttl_seconds},
        )
    client = get_report_client()

    def call():
        data = client.request("POST", "/reports/access-code", json_body={
            "cvc_id": cvc_id,
            "template_id": template_id,
            "time_range": time_range,
            "output_formats": formats,
            "language": language,
            "ttl_seconds": int(ttl_seconds),
        })
        if isinstance(data, dict) and data.get("report_url"):
            data["report_url"] = client.absolute_url(data["report_url"])
        return data

    return _report_call(call, client)


# ---------------------------------------------------------------------------
# CVC inspection / insights
# ---------------------------------------------------------------------------

@mcp.tool(
    title="租户总览与续跑入口（免费）",
    annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 2},
)
def casee_home_overview(limit: int = 8) -> dict:
    """会话第一调用：看已有价值链、构建中任务、最近报告与余额（免费）。

    USE WHEN: 新会话开始、用户问“我之前建到哪了 / 我有什么 / 从哪继续”；
    不确定用户是否已有 cvc_id 时先调用它，而不是猜。

    DO NOT USE WHEN: 已经知道 cvc_id 和任务目标，直接调用对应工具。

    PREREQUISITES: 无。COST: 0 credit。LATENCY: 约 2 秒。
    RETURNS: {tenant, cvc:{total,by_status,building,recent}, reports, credits,
    capabilities, pending_jobs[], onboarding[], recommendations[]}。
    NEXT: pending_jobs 非空 → get_cvc_build_status 续跑；total=0 → 引导建链；
    有 completed → semantic_search_tool / 洞察。

    Tenant overview: CVC counts/status, building jobs, recent reports, credits.

    Args:
        limit: recent model/report count (1-50)

    Returns:
        dict: {ok, data: {tenant, cvc: {total, by_status, building, recent},
              reports: {recent}, credits, capabilities, next, pending_jobs,
              onboarding, recommendations}, meta}
    """
    if not 1 <= limit <= 50:
        return _report_invalid("limit must be between 1 and 50", {"limit": limit})
    client = get_report_client()
    result = _report_call(
        lambda: client.request("GET", "/home/overview", params={"limit": limit}), client,
    )
    if isinstance(result, dict) and result.get("ok"):
        data = result.get("data")
        if isinstance(data, dict):
            _enrich_overview(data)
    return result


def _job_view(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    cvc_id = item.get("cvc_id") or item.get("id") or item.get("model_id")
    if not cvc_id:
        return None
    return {
        "cvc_id": cvc_id,
        "name": item.get("name") or item.get("cvc_name") or item.get("title") or "",
        "status": item.get("status") or item.get("build_status") or "building",
        "resume_tool": "get_cvc_build_status",
        "resume_args": {"cvc_id": cvc_id},
    }


def _enrich_overview(data: dict) -> None:
    """Add pending_jobs / onboarding / recommendations to /home/overview."""
    cvc = data.get("cvc") if isinstance(data.get("cvc"), dict) else {}
    building = cvc.get("building") or []
    if isinstance(building, dict):
        building = list(building.values())
    pending = [v for v in (_job_view(x) for x in building or []) if v]
    data["pending_jobs"] = pending

    total = int(cvc.get("total") or 0)
    completed = int((cvc.get("by_status") or {}).get("completed") or 0)
    onboarding = []
    if total == 0:
        onboarding.append({
            "step": "start_first_cvc",
            "title": "从零开始：先免费检索，再决定是否建链",
            "tools": ["search_intelligence", "casee_plan_task"],
            "note": "第一只价值链免费；lite 约 3-5 分钟 / full 约 10 分钟，完成后邮件通知。",
        })
    else:
        onboarding.append({
            "step": "resume_existing",
            "title": "复用已有价值链：先看总览与构建状态",
            "tools": ["casee_get_cvc_dashboard", "get_cvc_build_status"],
        })
    onboarding.append({
        "step": "pick_capability",
        "title": "不确定下一步用哪个能力时，让 casee_plan_task 给出 2-3 个方案",
        "tools": ["casee_plan_task", "casee_guide"],
    })
    data["onboarding"] = onboarding

    recs = []
    if pending:
        recs.append(_recommendation(
            "resume_build",
            kind="next_step", priority="high",
            message=f"有 {len(pending)} 个构建中任务：先查状态，不要重复建链。",
            tool="get_cvc_build_status",
            args={"cvc_id": pending[0]["cvc_id"]},
            eta_seconds=2,
        ))
    if total == 0:
        recs.append(_recommendation(
            "first_cvc",
            kind="capability", priority="medium",
            message="还没有价值链：先免费检索确认主题，再建链以沉淀历史（首条免费）。",
            tool="casee_plan_task",
            args={"goal": "我想持续跟踪一个行业或公司"},
            eta_seconds=1,
        ))
    elif completed:
        recs.append(_recommendation(
            "use_completed_cvc",
            kind="next_step", priority="medium",
            message="已有可用价值链：可直接做语义检索或洞察，不必重新全站检索。",
            tool="semantic_search_tool",
            args={"q": "<topic>", "cvc_model_id": "<cvc_id>"},
            eta_seconds=5,
        ))
    _attach_recommendations(data, recs)


@mcp.tool()
def casee_get_cvc_model(
    cvc_id: str,
    include: str = "",
    detail: str = "summary",
    expand: str = "",
    min_confidence: float = 0.6,
    limit_per_type: int = 500,
) -> dict:
    """Read one CVC model's metadata, graph stats and entity collections.

    Lighter than build-status when you only need model state / counts.

    Args:
        cvc_id: CVC model id
        include: comma-separated entity collections (all = every collection)
        detail: summary | full
        expand: relations | ontology
        min_confidence: entity confidence floor (0-1)
        limit_per_type: max entities per collection (1-2000)

    Returns:
        dict: {ok, data: {model, counts, graph, data_freshness, companies...},
              meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if detail not in ("summary", "full"):
        return _report_invalid("detail must be summary or full", {"detail": detail})
    if not 0 <= min_confidence <= 1:
        return _report_invalid(
            "min_confidence must be between 0 and 1", {"min_confidence": min_confidence},
        )
    if not 1 <= limit_per_type <= 2000:
        return _report_invalid(
            "limit_per_type must be between 1 and 2000",
            {"limit_per_type": limit_per_type},
        )
    client = get_report_client()
    params = {
        "include": include or None,
        "detail": detail,
        "expand": expand or None,
        "min_confidence": min_confidence,
        "limit_per_type": limit_per_type,
    }
    return _report_call(
        lambda: client.request("GET", f"/cvc-models/{quote(cvc_id, safe='')}", params=params),
        client,
    )


@mcp.tool()
def casee_list_cvc_companies(cvc_id: str) -> dict:
    """List the companies in a CVC with roles, websites and selected entities.

    Use the returned names for enterprise-insight calls or to explain who the
    focus company / competitors / customers are.

    Args:
        cvc_id: CVC model id

    Returns:
        dict: {ok, data: {cvc_id, cvc_name, total, companies[]}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    client = get_report_client()
    return _report_call(
        lambda: client.request("GET", f"/cvc-models/{quote(cvc_id, safe='')}/companies"),
        client,
    )


@mcp.tool()
def casee_list_cvc_entities(
    cvc_id: str,
    entity_type: str = "all",
    keyword: str = "",
    company: str = "",
    min_confidence: float = 0.6,
    page: int = 1,
    page_size: int = 20,
    sort: str = "",
) -> dict:
    """List CVC ontology entities (company/product/market/belief/attribute...) with paging.

    Args:
        cvc_id: CVC model id
        entity_type: all | company | competitor | product | customer | partner |
            market | belief | attribute
        keyword: name keyword
        company: restrict to an owning company
        min_confidence: confidence floor (0-1)
        page: 1-based page
        page_size: 1-200
        sort: name/-name/confidence/-confidence/weight/-weight

    Returns:
        dict: {ok, data: {items, pagination}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if entity_type not in _ENTITY_TYPES:
        return _report_invalid(
            f"entity_type must be one of {_ENTITY_TYPES}", {"entity_type": entity_type},
        )
    if page < 1 or not 1 <= page_size <= 200:
        return _report_invalid(
            "page must be >= 1 and page_size between 1 and 200",
            {"page": page, "page_size": page_size},
        )
    if not 0 <= min_confidence <= 1:
        return _report_invalid(
            "min_confidence must be between 0 and 1", {"min_confidence": min_confidence},
        )
    client = get_report_client()
    params = {
        "entity_type": entity_type,
        "keyword": keyword or None,
        "company": company or None,
        "min_confidence": min_confidence,
        "page": page,
        "page_size": page_size,
        "sort": sort or None,
    }
    return _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/entities", params=params,
        ),
        client,
    )


@mcp.tool()
def casee_get_cvc_entity(
    cvc_id: str,
    entity_type: str,
    entity_id: str,
    by: str = "id",
    relations_depth: int = 1,
    include_intel: bool = False,
    include_ontology: bool = True,
) -> dict:
    """Read one entity's attributes, relations, ontology IRI and related intel.

    Args:
        cvc_id: CVC model id
        entity_type: company | competitor | product | customer | partner |
            market | belief | attribute
        entity_id: entity id or name (with by=name)
        by: id | name
        relations_depth: 1-2
        include_intel: attach related intelligence
        include_ontology: include ontology IRI (default true)

    Returns:
        dict: {ok, data: {entity, relations, ontology_iri, related_intel},
              meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if entity_type not in _CONCRETE_ENTITY_TYPES:
        return _report_invalid(
            f"entity_type must be one of {_CONCRETE_ENTITY_TYPES}",
            {"entity_type": entity_type},
        )
    if not entity_id:
        return _report_invalid("entity_id is required", {"entity_id": entity_id})
    if by not in ("id", "name"):
        return _report_invalid("by must be id or name", {"by": by})
    if not 1 <= relations_depth <= 2:
        return _report_invalid(
            "relations_depth must be 1 or 2", {"relations_depth": relations_depth},
        )
    client = get_report_client()
    params = {
        "by": by,
        "relations_depth": relations_depth,
        "include_intel": include_intel,
        "include_ontology": include_ontology,
    }
    path = (
        f"/cvc-models/{quote(cvc_id, safe='')}/entities/"
        f"{quote(entity_type, safe='')}/{quote(entity_id, safe='')}"
    )
    return _report_call(lambda: client.request("GET", path, params=params), client)


@mcp.tool()
def casee_get_ontology_schema(cvc_id: str) -> dict:
    """Read the CVC ontology TBox (classes, object/data/annotation properties).

    Args:
        cvc_id: CVC model id

    Returns:
        dict: {ok, data: {classes, object_properties, data_properties,
              annotation_properties}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    client = get_report_client()
    return _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/ontology/schema",
        ),
        client,
    )


@mcp.tool()
def casee_export_ontology(
    cvc_id: str,
    entity_type: str = "",
    entity_id: str = "",
    keyword: str = "",
    depth: int = 1,
    format: str = "turtle",
    include: str = "abox",
    layers: str = "fact,belief,causal",
    min_confidence: float = 0.6,
    include_evidence: bool = False,
    save_path: str = "",
    inline: bool = False,
    max_inline_chars: int = 200000,
) -> dict:
    """Export a CVC entity/keyword subgraph as OWL 2 DL (bounded to 200 triples).

    Scope is mandatory unless include=tbox: provide entity_type+entity_id or
    keyword. The artifact is saved to disk; set inline=true for the text.

    Args:
        cvc_id: CVC model id
        entity_type: optional entity type
        entity_id: optional entity id (requires entity_type)
        keyword: keyword subgraph (alternative scope)
        depth: 1-2
        format: turtle | rdfxml | jsonld | nt
        include: abox | tbox | both
        layers: fact,belief,causal
        min_confidence: 0-1
        include_evidence: include evidence triples
        save_path: file or directory
        inline: include the OWL text in the response
        max_inline_chars: inline truncation limit

    Returns:
        dict: {ok, data: {saved_path, filename, bytes, sha256, content?,
              truncated?}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if format not in _ONTOLOGY_FORMATS:
        return _report_invalid(
            f"format must be one of {_ONTOLOGY_FORMATS}", {"format": format},
        )
    if include not in ("abox", "tbox", "both"):
        return _report_invalid(
            "include must be abox, tbox or both", {"include": include},
        )
    if include != "tbox" and not entity_id and not keyword:
        return _report_invalid(
            "ontology export requires entity_type+entity_id or keyword "
            "(or include='tbox')",
            {"entity_id": entity_id, "keyword": keyword},
        )
    if entity_id and not entity_type:
        return _report_invalid(
            "entity_type is required when entity_id is provided",
            {"entity_id": entity_id},
        )
    if not 1 <= depth <= 2:
        return _report_invalid("depth must be 1 or 2", {"depth": depth})
    client = get_report_client()
    params = {
        "entity_type": entity_type or None,
        "entity_id": entity_id or None,
        "keyword": keyword or None,
        "depth": depth,
        "format": format,
        "include": include,
        "layers": layers,
        "min_confidence": min_confidence,
        "include_evidence": include_evidence,
    }

    def call():
        content, meta = client.request_bytes(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/ontology", params=params,
            accept="text/plain",
        )
        scope = _safe_component(entity_id or keyword or "tbox", "graph")
        return _artifact_payload(
            content, meta, save_path=save_path,
            filename=f"{_safe_component(cvc_id, 'cvc')}-{scope}.{_ONTOLOGY_EXT[format]}",
            inline=inline, max_inline_chars=max_inline_chars,
        )

    return _report_call(call, client)


@mcp.tool()
def casee_search_cvc_intelligence(
    cvc_id: str,
    keywords: str = "",
    match: str = "any",
    exclude_keywords: str = "",
    start_time: str = "",
    end_time: str = "",
    days: int = 0,
    company: str = "",
    category: str = "",
    sentiment: str = "",
    language: str = "",
    min_score: float = 0.0,
    live: bool = False,
    page: int = 1,
    page_size: int = 20,
    sort: str = "-published_at",
) -> dict:
    """Search the intelligence collected inside one CVC model.

    Args:
        cvc_id: CVC model id
        keywords: comma-separated keywords
        match: any | all
        exclude_keywords: comma-separated exclusions
        start_time / end_time: ISO8601 bounds
        days: relative window (1-3650; 0 = unset)
        company: filter by company
        category: filter by category
        sentiment: positive | negative | neutral
        language: zh | en
        min_score: source trust floor (0-1)
        live: trigger upstream live retrieval (needs intel:live scope)
        page: 1-based page
        page_size: 1-200
        sort: -published_at / published_at / -tscore ...

    Returns:
        dict: {ok, data: {items, pagination, query, generated_at}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if match not in ("any", "all"):
        return _report_invalid("match must be any or all", {"match": match})
    if sentiment and sentiment not in ("positive", "negative", "neutral"):
        return _report_invalid(
            "sentiment must be positive, negative or neutral", {"sentiment": sentiment},
        )
    if language and language not in _LANGUAGES:
        return _report_invalid("language must be zh or en", {"language": language})
    if days and not 1 <= days <= 3650:
        return _report_invalid("days must be between 1 and 3650", {"days": days})
    if not 0 <= min_score <= 1:
        return _report_invalid("min_score must be between 0 and 1", {"min_score": min_score})
    if page < 1 or not 1 <= page_size <= 200:
        return _report_invalid(
            "page must be >= 1 and page_size between 1 and 200",
            {"page": page, "page_size": page_size},
        )
    client = get_report_client()
    params = {
        "keywords": keywords or None,
        "match": match,
        "exclude_keywords": exclude_keywords or None,
        "start_time": start_time or None,
        "end_time": end_time or None,
        "days": days or None,
        "company": company or None,
        "category": category or None,
        "sentiment": sentiment or None,
        "language": language or None,
        "min_score": min_score if min_score > 0 else None,
        "live": live or None,
        "page": page,
        "page_size": page_size,
        "sort": sort,
    }
    return _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/intelligence", params=params,
        ),
        client,
    )


@mcp.tool()
def casee_get_intelligence_detail(cvc_id: str, intel_id: str) -> dict:
    """Read one intelligence item (title, content, source, tscore) plus entities.

    Args:
        cvc_id: CVC model id
        intel_id: intelligence id from a search result

    Returns:
        dict: {ok, data: {item, related_entities}, meta}
    """
    if not cvc_id or not intel_id:
        return _report_invalid(
            "cvc_id and intel_id are required",
            {"cvc_id": cvc_id, "intel_id": intel_id},
        )
    client = get_report_client()
    path = (
        f"/cvc-models/{quote(cvc_id, safe='')}/intelligence/"
        f"{quote(intel_id, safe='')}"
    )
    return _report_call(lambda: client.request("GET", path), client)


@mcp.tool()
def casee_export_intelligence(
    cvc_id: str,
    format: str = "jsonl",
    save_path: str = "",
    inline: bool = False,
    max_inline_chars: int = 200000,
) -> dict:
    """Export every intelligence item in a CVC (JSONL/JSON) to a local file.

    Use before a destructive rebuild for an offline backup. This tool writes
    the full export; ``backup_cvc_intelligence`` only returns metadata.

    Args:
        cvc_id: CVC model id
        format: jsonl | json
        save_path: file or directory
        inline: include (truncated) content in the response
        max_inline_chars: inline truncation limit

    Returns:
        dict: {ok, data: {saved_path, filename, bytes, sha256, etag,
              total_count, content?, truncated?}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if format not in ("jsonl", "json"):
        return _report_invalid("format must be jsonl or json", {"format": format})
    client = get_report_client()

    def call():
        content, meta = client.request_bytes(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/intelligence/export",
            params={"format": format},
            accept="application/x-ndjson" if format == "jsonl" else "application/json",
        )
        return _artifact_payload(
            content, meta, save_path=save_path,
            filename=f"{_safe_component(cvc_id, 'cvc')}-intelligence.{format}",
            inline=inline, max_inline_chars=max_inline_chars,
        )

    return _report_call(call, client)


@mcp.tool(
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 3,
          "requires": "completed CVC", "next": "semantic_search_tool"},
)
def casee_get_cvc_dashboard(cvc_id: str, time_range: str = "month") -> dict:
    """Read the CVC business dashboard (volume / sentiment / share of voice).

    No LLM and no Credits.

    Args:
        cvc_id: CVC model id
        time_range: week | month | quarter

    Returns:
        dict: {ok, data: {summary, companies, sentiment, events}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if time_range not in _TIME_RANGES:
        return _report_invalid(
            f"time_range must be one of {_TIME_RANGES}", {"time_range": time_range},
        )
    client = get_report_client()
    return _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/dashboard",
            params={"time_range": time_range},
        ),
        client,
    )


@mcp.tool()
def casee_get_market_trends(cvc_id: str, time_range: str = "month") -> dict:
    """Read time-aggregated market trends (heat / sources / topics).

    Requires the "市场情报平台" (Market Intelligence Platform) product module
    subscription; tenants without it get 403 PRODUCT_ACCESS_DENIED — fall back
    to aggregate_intelligence / analyze_trend to compute equivalent
    heat / sources / topics locally from retrieved intelligence items.

    No LLM and no Credits.

    Args:
        cvc_id: CVC model id
        time_range: week | month | quarter

    Returns:
        dict: {ok, data: {trend, summary}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if time_range not in _TIME_RANGES:
        return _report_invalid(
            f"time_range must be one of {_TIME_RANGES}", {"time_range": time_range},
        )
    client = get_report_client()
    return _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/market-trends",
            params={"time_range": time_range},
        ),
        client,
    )


def _decorate_insight(result: dict, *, cvc_id: str, dry_run: bool, tool: str,
                      confirmed: bool = False, **tool_args: Any) -> dict:
    """Make cost / confirmation / next steps explicit for the three insights.

    ``dry_run`` 结果会带一个签名 ``confirm_token``：用户只需确认一次，之后把
    ``confirm_token`` 原样传入 ``dry_run=False`` 的调用即可直接执行，不必再走
    一轮「预检 → 再确认」。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    data = result.get("data")
    confirm_args = {"cvc_id": cvc_id, **tool_args}
    if isinstance(data, dict):
        data["cost_note"] = {
            "dry_run": dry_run,
            "charged_credits": 0 if dry_run else 0.5,
            "eta_seconds": 0 if dry_run else 20,
        }
        if dry_run:
            token = issue_confirm_token(tool, confirm_args)
            data["confirm_token"] = token
            data["confirm_token_expires_in"] = _CONFIRM_TOKEN_TTL_SECONDS
            # 可直接执行的调用：客户端把用户的一次确认转成这一次带令牌的调用。
            data["confirm_call"] = {
                "tool": tool,
                "args": {**confirm_args, "dry_run": False, "confirm_token": token},
                "cost_credits": 0.5,
                "eta_seconds": 20,
            }
            data["confirm_prompt"] = (
                "预检完成，未扣费。是否花费 0.5 credit 生成洞察？"
                "（确认后请直接用 confirm_call 执行，confirm_token 在 "
                f"{_CONFIRM_TOKEN_TTL_SECONDS // 60} 分钟内有效，无需再次预检）"
            )
        elif confirmed:
            data["confirmed"] = True
            data["cost_note"]["confirmation"] = "confirm_token"
    recs = []
    if dry_run:
        recs.append(_recommendation(
            "confirm_insight",
            kind="next_step", priority="high",
            message=("预检未扣费。用户确认后直接执行 confirm_call（0.5 credit，"
                     "8-20 秒）；confirm_token 已绑定本次参数，无需重复预检/确认。"),
            tool=tool,
            args={**confirm_args, "dry_run": False,
                  "confirm_token": "<confirm_token>"},
            cost_credits=0.5, eta_seconds=20, requires_user_confirm=True,
            show_once_per_session=False,
        ))
    else:
        recs.append(_recommendation(
            "deliver_report",
            kind="next_step", priority="medium",
            message="洞察已生成：可选择报告模板并预检（免费），再生成报告（1 credit）。",
            tool="list_report_templates", args={}, eta_seconds=2,
        ))
    _attach_recommendations(data if isinstance(data, dict) else result, recs)
    return result


@mcp.tool(
    title="企业洞察（0.5 credit，先 dry_run）",
    annotations={"readOnlyHint": False, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T2", "cost_credits": 0.5, "eta_seconds": 20,
          "requires_user_confirm": True, "dry_run_default": True,
          "requires": "insight:generate + product module"},
)
def casee_get_enterprise_insight(
    cvc_id: str,
    company_name: str,
    force_refresh: bool = False,
    dry_run: bool = True,
    max_age_days: int = 7,
    confirm_token: str = "",
) -> dict:
    """企业洞察：判断一家公司值不值得合作、差距与风险（0.5 credit，先 dry_run）。

    USE WHEN: 用户已有 cvc_id 与 company_name（来自 casee_list_cvc_companies），
    要判断「这家公司靠不靠谱 / 风险在哪 / 和我们差距多大 / 值不值得合作」。
    DO NOT USE WHEN:
    - 还没有 cvc_id → 先 search_cvc_companies → create_cvc_model（约 10 分钟）。
    - 只想看最近动态 → search_intelligence（免费）。
    - 想看行业驱动因素 → casee_get_market_insight。
    PREREQUISITES: cvc_id 已 completed；租户已开通 insight 模块并有余量。
    COST: 0.5 credit/次（dry_run=true 不扣费）。LATENCY: 约 20 秒。
    RETURNS: profile / strengths / gaps / opportunities / risks / evidence +
    cost_note（dry_run, charged_credits）+ confirm_prompt + recommendations。
    NEXT: 先用 dry_run=true 预检并把 confirm_prompt 转述给用户；用户同意后再以
    dry_run=false 生成；需要交付时用 list_report_templates → generate_report。

    Generate/read an enterprise insight (profile, gaps, opportunities, risks).

    Costs 0.5 Credits when generated. ``dry_run=true`` (default) only probes the
    cache and balance without charging; set dry_run=false only after the user
    agrees to spend credits.

    Args:
        cvc_id: CVC model id
        company_name: company from casee_list_cvc_companies
        force_refresh: bypass cache (charges again)
        dry_run: probe only, no generation and no charge
        max_age_days: cache freshness window (1-90)
        confirm_token: ``dry_run`` 返回的一次性令牌；传入后视为用户已确认，
            直接执行（免重复预检/确认），需与预检时参数一致且在有效期内

    Returns:
        dict: {ok, data: {profile, strengths, gaps, opportunities, risks,
              evidence, ...}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if not company_name.strip():
        return _report_invalid("company_name is required", {"company_name": company_name})
    if not 1 <= max_age_days <= 90:
        return _report_invalid(
            "max_age_days must be between 1 and 90", {"max_age_days": max_age_days},
        )
    confirmed = (not dry_run) and verify_confirm_token(
        confirm_token, "casee_get_enterprise_insight",
        {"cvc_id": cvc_id, "company_name": company_name,
         "force_refresh": force_refresh, "max_age_days": max_age_days},
    )
    client = get_report_client()
    params = {
        "company_name": company_name,
        "force_refresh": force_refresh,
        "dry_run": dry_run,
        "max_age_days": max_age_days,
    }
    result = _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/enterprise-insight",
            params=params,
        ),
        client,
    )
    return _decorate_insight(
        result, cvc_id=cvc_id, dry_run=dry_run, confirmed=confirmed,
        tool="casee_get_enterprise_insight", company_name=company_name,
        force_refresh=force_refresh, max_age_days=max_age_days,
    )


@mcp.tool(
    title="市场洞察（0.5 credit，先 dry_run）",
    annotations={"readOnlyHint": False, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T2", "cost_credits": 0.5, "eta_seconds": 20,
          "requires_user_confirm": True, "dry_run_default": True,
          "requires": "insight:generate + product module"},
)
def casee_get_market_insight(
    cvc_id: str,
    market_name: str,
    force_refresh: bool = False,
    dry_run: bool = True,
    max_age_days: int = 7,
    confirm_token: str = "",
) -> dict:
    """Generate/read a market insight (size, growth, landscape, drivers, risks).

    Costs 0.5 Credits when generated; dry_run=true by default (probe only).

    Args:
        cvc_id: CVC model id
        market_name: market / segment name
        force_refresh: bypass cache
        dry_run: probe only, no charge
        max_age_days: cache freshness window (1-90)
        confirm_token: ``dry_run`` 返回的一次性令牌，传入后直接执行

    Returns:
        dict: {ok, data: {market, size, growth, landscape, drivers, risks},
              meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if not market_name.strip():
        return _report_invalid("market_name is required", {"market_name": market_name})
    if not 1 <= max_age_days <= 90:
        return _report_invalid(
            "max_age_days must be between 1 and 90", {"max_age_days": max_age_days},
        )
    confirmed = (not dry_run) and verify_confirm_token(
        confirm_token, "casee_get_market_insight",
        {"cvc_id": cvc_id, "market_name": market_name,
         "force_refresh": force_refresh, "max_age_days": max_age_days},
    )
    client = get_report_client()
    params = {
        "market_name": market_name,
        "force_refresh": force_refresh,
        "dry_run": dry_run,
        "max_age_days": max_age_days,
    }
    result = _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/market-insight",
            params=params,
        ),
        client,
    )
    return _decorate_insight(
        result, cvc_id=cvc_id, dry_run=dry_run, confirmed=confirmed,
        tool="casee_get_market_insight", market_name=market_name,
        force_refresh=force_refresh, max_age_days=max_age_days,
    )


@mcp.tool(
    title="客户洞察（0.5 credit，先 dry_run）",
    annotations={"readOnlyHint": False, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T2", "cost_credits": 0.5, "eta_seconds": 20,
          "requires_user_confirm": True, "dry_run_default": True,
          "requires": "insight:generate + product module"},
)
def casee_get_customer_insight(
    cvc_id: str,
    customer_name: str,
    force_refresh: bool = False,
    dry_run: bool = True,
    max_age_days: int = 7,
    confirm_token: str = "",
) -> dict:
    """Generate/read a customer insight (preferences, demand, decision chain).

    Costs 0.5 Credits when generated; dry_run=true by default (probe only).

    Args:
        cvc_id: CVC model id
        customer_name: customer name
        force_refresh: bypass cache
        dry_run: probe only, no charge
        max_age_days: cache freshness window (1-90)
        confirm_token: ``dry_run`` 返回的一次性令牌，传入后直接执行

    Returns:
        dict: {ok, data: {customer, preferences, demand_signals,
              decision_chain, opportunities}, meta}
    """
    if not cvc_id:
        return _report_invalid("cvc_id is required", {"cvc_id": cvc_id})
    if not customer_name.strip():
        return _report_invalid("customer_name is required", {"customer_name": customer_name})
    if not 1 <= max_age_days <= 90:
        return _report_invalid(
            "max_age_days must be between 1 and 90", {"max_age_days": max_age_days},
        )
    confirmed = (not dry_run) and verify_confirm_token(
        confirm_token, "casee_get_customer_insight",
        {"cvc_id": cvc_id, "customer_name": customer_name,
         "force_refresh": force_refresh, "max_age_days": max_age_days},
    )
    client = get_report_client()
    params = {
        "customer_name": customer_name,
        "force_refresh": force_refresh,
        "dry_run": dry_run,
        "max_age_days": max_age_days,
    }
    result = _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/customer-insight",
            params=params,
        ),
        client,
    )
    return _decorate_insight(
        result, cvc_id=cvc_id, dry_run=dry_run, confirmed=confirmed,
        tool="casee_get_customer_insight", customer_name=customer_name,
        force_refresh=force_refresh, max_age_days=max_age_days,
    )


# ---------------------------------------------------------------------------
# 行业模版建链（第三种创建方式：浏览模版 → 授权实例化 → 共享构建）
# ---------------------------------------------------------------------------
# 与门户「模版库 → 使用模版」同口径：模版由专家预置企业清单与检索口径，
# 实例全租户共享，无需重复构建。工具命名与 eirs/api_meta/endpoint_meta.py 的
# mcp.tool 声明保持一致，避免开放 API 与 MCP 两套叫法。
_TEMPLATE_BUILD_CREDITS = 2.5
_TEMPLATE_BUILD_ETA_SECONDS = 600
_TEMPLATE_PROVISION_ETA_SECONDS = 90


def _template_summary(data: dict) -> dict:
    """模版详情裁剪为预览结构：保留选型所需字段，去掉属主等内部字段。"""
    if not isinstance(data, dict):
        return {}
    keys = (
        "id", "name", "summary", "description", "industry", "scenario",
        "companies_preview", "provision_state", "granted", "grants_count",
        "status", "build_profile",
    )
    return {key: data.get(key) for key in keys if key in data}


@mcp.tool(
    title="浏览行业模版（免费，第三种建链方式）",
    annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T1", "cost_credits": 0, "eta_seconds": 2,
          "next": "get_cvc_template / create_cvc_from_template"},
)
def list_cvc_templates(
    industry: str = "",
    scenario: str = "",
    q: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """浏览行业模版库（免费）：专家预置企业清单 + 检索口径，一键实例化建链。

    USE WHEN:
    - 用户不确定该搜哪些公司/该用什么行业口径；
    - 用户说「有没有现成的行业分析 / 模版 / 案例」；
    - 想复用专家配置快速得到可用的价值链（比从零检索更快）。

    DO NOT USE WHEN: 用户已明确给出公司清单 → search_cvc_companies → create_cvc_model。

    COST: 0 credit。LATENCY: 约 1-2 秒。
    NEXT: 选定 id 后先 get_cvc_template 看企业清单，再 create_cvc_from_template
    （2.5 credits，租户首个 CVC 免费；老用户已实例化的模版不重复扣费）。

    Args:
        industry: industry filter, e.g. 新能源 / 半导体
        scenario: scenario filter, e.g. 竞争分析 / 市场调研 / 企业出海
        q: keyword over name / summary / topic
        limit: page size, 1-100 (default 20)
        offset: page offset (default 0)

    Returns:
        dict: {ok, data: {total, limit, offset, items: [{id, name, summary,
              industry, scenario, companies_preview[], provision_state,
              granted, grants_count}]}, meta}
    """
    if not 1 <= limit <= 100:
        return _report_invalid("limit must be between 1 and 100", {"limit": limit})
    if offset < 0:
        return _report_invalid("offset must be >= 0", {"offset": offset})
    params = {"limit": int(limit), "offset": int(offset)}
    for key, value in (("industry", industry), ("scenario", scenario), ("q", q)):
        if str(value or "").strip():
            params[key] = str(value).strip()
    client = get_report_client()
    result = _report_call(
        lambda: client.request("GET", "/cvc-templates", params=params), client,
    )
    if isinstance(result, dict) and result.get("ok"):
        data = result.get("data") or {}
        items = data.get("items") or []
        if not items:
            data["next_actions"] = [{
                "tool": "search_cvc_companies",
                "args": {"query": q or "<分析目标>", "limit": 8},
                "hint": "没有匹配模版时改用智能推理检索候选公司，再从检索结果建链",
            }]
            _attach_recommendations(result, [_recommendation(
                "fallback_smart_search",
                kind="fallback", priority="medium",
                message=("没有匹配的模版：改用智能推理检索候选公司，再从检索结果建链。"),
                tool="search_cvc_companies",
                args={"query": q or "<分析目标>", "limit": 8},
                cost_credits=0, eta_seconds=8,
            )], into_data=True)
        elif items:
            first = items[0] if isinstance(items[0], dict) else {}
            data["next_actions"] = [{
                "tool": "get_cvc_template",
                "args": {"template_id": first.get("id") or "<template_id>"},
                "hint": "先看模版详情里的企业清单与准备状态，再决定是否实例化",
            }]
            _attach_recommendations(result, [_recommendation(
                "inspect_template",
                kind="next_step", priority="high",
                message="先看模版详情里的企业清单与准备状态，再决定是否实例化。",
                tool="get_cvc_template",
                args={"template_id": first.get("id") or "<template_id>"},
                cost_credits=0, eta_seconds=2,
            )], into_data=True)
    return result


@mcp.tool(
    title="行业模版详情（免费）",
    annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T1", "cost_credits": 0, "eta_seconds": 2,
          "next": "create_cvc_from_template"},
)
def get_cvc_template(template_id: str) -> dict:
    """读取行业模版详情（免费）：企业清单预览、实例准备状态、本租户授权状态。

    Args:
        template_id: template id from list_cvc_templates

    Returns:
        dict: {ok, data: {id, name, summary, industry, scenario,
              companies_preview[], provision_state, granted, grants_count,
              creation_endpoint, provision_status_url}, meta}
    """
    error = _path_id(template_id, "template_id")
    if error:
        return _report_invalid(error, {"template_id": template_id})
    client = get_report_client()
    result = _report_call(
        lambda: client.request(
            "GET", f"/cvc-templates/{quote(template_id, safe='')}",
        ),
        client,
    )
    if isinstance(result, dict) and result.get("ok"):
        data = result.get("data")
        if isinstance(data, dict):
            data["next_actions"] = [{
                "tool": "create_cvc_from_template",
                "args": {"template_id": template_id, "dry_run": True},
                "hint": "先 dry_run 预检确认成本，用户同意后再 confirm=true 执行",
            }]
        _attach_recommendations(result, [_recommendation(
            "instantiate_template",
            kind="next_step", priority="high",
            message=("实例化模版（2.5 credits，租户首个 CVC 免费；已实例化不重复扣费）。"
                     "先 dry_run 预检，用户确认后再 confirm=true 执行。"),
            tool="create_cvc_from_template",
            args={"template_id": template_id, "dry_run": True},
            cost_credits=0, eta_seconds=2, requires_user_confirm=True,
        )], into_data=True)
    return result


@mcp.tool(
    title="用行业模版建链（2.5 credits，先 dry_run）",
    annotations={"readOnlyHint": False, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T2", "cost_credits": _TEMPLATE_BUILD_CREDITS,
          "eta_seconds": _TEMPLATE_BUILD_ETA_SECONDS, "first_free": True,
          "requires_user_confirm": True, "dry_run_default": True,
          "requires": "cvc:write + template published"},
)
def create_cvc_from_template(
    template_id: str,
    dry_run: bool = True,
    confirm: bool = False,
    confirm_token: str = "",
) -> dict:
    """用行业模版创建价值链：幂等授权 + 首次实例化并异步构建。

    USE WHEN: 用户想复用专家预置的行业口径，或已从 list_cvc_templates 选定模版。
    DO NOT USE WHEN: 用户有明确的企业清单 → create_cvc_model（智能推理/精确定义）。

    COST: 2.5 credits（full 构建）；租户首个 CVC 免费；模版实例全租户共享，
    已实例化且构建完成的模版再次使用不扣费。dry_run=true 只预检，不扣费。
    LATENCY: 实例化秒级返回 202；共享构建约 3-10 分钟，完成发邮件。
    RETURNS: dry_run → 模版预览 + cost_note + confirm_token + confirm_call；
    执行 → {cvc_id, instance_status, is_first_use, already_granted,
    provision_eta_seconds, next_actions}。
    NEXT: 轮询 get_cvc_template_provision_status，实例 ready 后
    get_cvc_build_status 看构建进度；不要阻塞对话等构建完成。

    Args:
        template_id: template id from list_cvc_templates
        dry_run: preview only (default true), no charge and no instantiation
        confirm: set true together with dry_run=false after the user agrees
        confirm_token: token returned by dry_run; passing it implies confirmation
            (bound to template_id, valid for MCP_CONFIRM_TOKEN_TTL)

    Returns:
        dict: {ok, data: {...}, meta}
    """
    error = _path_id(template_id, "template_id")
    if error:
        return _report_invalid(error, {"template_id": template_id})
    confirm_args = {"template_id": template_id, "dry_run": False}
    confirmed = verify_confirm_token(
        confirm_token, "create_cvc_from_template", confirm_args,
    ) or (bool(confirm) and not dry_run)
    client = get_report_client()

    if not confirmed:
        preview = _report_call(
            lambda: client.request(
                "GET", f"/cvc-templates/{quote(template_id, safe='')}",
            ),
            client,
        )
        if not isinstance(preview, dict) or not preview.get("ok"):
            return preview
        template = _template_summary(preview.get("data") or {})
        already_granted = bool(template.get("granted"))
        token = issue_confirm_token("create_cvc_from_template", confirm_args)
        data = {
            "dry_run": True,
            "template": template,
            "requires_confirm": True,
            "cost_note": {
                "credits": _TEMPLATE_BUILD_CREDITS,
                "first_cvc_free": True,
                "already_granted": already_granted,
                "charged_on_confirm": (not already_granted)
                and template.get("provision_state") != "ready",
                "note": "首个 CVC 免费；已实例化且就绪的模版不重复扣费；失败自动退点。",
            },
            "confirm_token": token,
            "confirm_call": {
                "tool": "create_cvc_from_template",
                "args": {"template_id": template_id, "dry_run": False, "confirm": True},
            },
            "user_message": (
                f"预检完成，未扣费。是否使用模版「{template.get('name') or template_id}」"
                f"创建价值链？（约 {_TEMPLATE_BUILD_CREDITS} credits，"
                "租户首个 CVC 免费；确认后直接执行 confirm_call，"
                f"令牌 {_CONFIRM_TOKEN_TTL_SECONDS // 60} 分钟内有效）"
            ),
        }
        result = {"ok": True, "data": data, "meta": preview.get("meta") or {}}
        _attach_recommendations(result, [_recommendation(
            "confirm_template_create",
            kind="next_step", priority="high",
            message=("把 user_message 转述给用户；确认后直接执行 confirm_call，"
                     "无需再次预检。"),
            tool="create_cvc_from_template",
            args={**confirm_args, "confirm": True, "confirm_token": "<confirm_token>"},
            cost_credits=_TEMPLATE_BUILD_CREDITS,
            eta_seconds=_TEMPLATE_BUILD_ETA_SECONDS,
            requires_user_confirm=True, show_once_per_session=False,
        )], into_data=True)
        return result

    result = _report_call(
        lambda: client.request(
            "POST", f"/cvc-templates/{quote(template_id, safe='')}/creation-jobs",
            json_body={},
        ),
        client,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    data = result.get("data")
    if isinstance(data, dict):
        data["creation_mode"] = "template"
        cvc_id = data.get("cvc_id") or ""
        data["user_message"] = (
            "已基于行业模版开始建链：共享实例约 3-10 分钟构建完成，"
            "完成后系统会向注册邮箱发送邮件通知；期间可先查看看板。"
        )
        data["next_actions"] = [
            {"tool": "get_cvc_template_provision_status",
             "args": {"template_id": template_id},
             "hint": "每 5 秒轮询一次，直到 instance_status=ready/failed"},
            {"tool": "get_cvc_build_status",
             "args": {"cvc_id": cvc_id or "<cvc_id>"},
             "hint": "实例 ready 后查看构建阶段与失败归因"},
        ]
        _attach_recommendations(result, [
            _recommendation(
                "poll_template_provision",
                kind="next_step", priority="high",
                message=("轮询模版实例准备状态；ready 后再看 build-status。"
                         "告诉用户约 3-10 分钟 + 完成邮件，不要阻塞等待。"),
                tool="get_cvc_template_provision_status",
                args={"template_id": template_id},
                cost_credits=0, eta_seconds=_TEMPLATE_PROVISION_ETA_SECONDS,
                show_once_per_session=False,
            ),
            _recommendation(
                "template_warmup",
                kind="capability", priority="medium",
                message="构建期间可先看免费看板与市场趋势，避免用户空等。",
                tool="casee_get_cvc_dashboard",
                args={"cvc_id": cvc_id or "<cvc_id>"},
                cost_credits=0, eta_seconds=5,
            ),
        ], into_data=True)
    return result


@mcp.tool(
    title="模版实例准备状态（免费）",
    annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T1", "cost_credits": 0, "eta_seconds": 2,
          "requires": "create_cvc_from_template"},
)
def get_cvc_template_provision_status(template_id: str) -> dict:
    """轮询模版实例准备进度（免费）：pending → building → ready / failed。

    Args:
        template_id: template id used in create_cvc_from_template

    Returns:
        dict: {ok, data: {template_id, cvc_id, instance_status, build_progress,
              current_phase, updated_at, user_message, next_actions}, meta}
    """
    error = _path_id(template_id, "template_id")
    if error:
        return _report_invalid(error, {"template_id": template_id})
    client = get_report_client()
    result = _report_call(
        lambda: client.request(
            "GET", f"/cvc-templates/{quote(template_id, safe='')}/provision-status",
        ),
        client,
    )
    if isinstance(result, dict) and result.get("ok"):
        data = result.get("data")
        if isinstance(data, dict):
            status = str(data.get("instance_status") or "")
            label = {
                "not_provisioned": "尚未实例化",
                "pending": "等待实例化",
                "provisioning": "实例准备中",
                "building": "构建中",
                "ready": "实例已就绪",
                "failed": "实例准备失败",
                "suspended": "实例已暂停",
            }.get(status, status or "未知状态")
            data["status_label"] = label
            cvc_id = data.get("cvc_id") or ""
            if status == "ready":
                data["user_message"] = "实例已就绪，可查看构建进度与看板。"
                data["next_actions"] = [{
                    "tool": "get_cvc_build_status",
                    "args": {"cvc_id": cvc_id or "<cvc_id>"},
                    "hint": "查看构建阶段；ready 后可直接使用语义检索与洞察",
                }]
            elif status == "failed":
                data["user_message"] = (
                    "实例准备失败：不要自动重试，先向用户说明并在模版库重新使用该模版。"
                )
                data["next_actions"] = [{
                    "tool": "create_cvc_from_template",
                    "args": {"template_id": template_id, "dry_run": True},
                    "hint": "重新预检并实例化（用户确认后执行）",
                }]
            else:
                data["user_message"] = "实例仍在准备中，请稍后再轮询（建议间隔 5 秒）。"
    return result
