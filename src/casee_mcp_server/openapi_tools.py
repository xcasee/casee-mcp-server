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
    _report_call,
    _report_invalid,
    get_report_client,
    mcp,
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

@mcp.tool()
def casee_get_credits() -> dict:
    """Read the tenant Credits balance and per-operation unit prices.

    Call this before any charged operation (generate_report, rebuild, insights)
    to avoid a 402 mid-flight. Does not consume Credits.

    Returns:
        dict: {ok, data: {balance, pending_credits, buckets: {free, monthly,
              paid}, ledger_cursor, unit, operations: {op: {credits,
              available}}, as_of}, meta}
    """
    client = get_report_client()
    return _report_call(lambda: client.request("GET", "/credits"), client)


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

@mcp.tool()
def casee_home_overview(limit: int = 8) -> dict:
    """Tenant overview: CVC counts/status, building jobs, recent reports, credits.

    A good first call for a new session: shows what exists and what is running.

    Args:
        limit: recent model/report count (1-50)

    Returns:
        dict: {ok, data: {tenant, cvc: {total, by_status, building, recent},
              reports: {recent}, credits, capabilities, next}, meta}
    """
    if not 1 <= limit <= 50:
        return _report_invalid("limit must be between 1 and 50", {"limit": limit})
    client = get_report_client()
    return _report_call(
        lambda: client.request("GET", "/home/overview", params={"limit": limit}), client,
    )


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


@mcp.tool()
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


@mcp.tool()
def casee_get_enterprise_insight(
    cvc_id: str,
    company_name: str,
    force_refresh: bool = False,
    dry_run: bool = True,
    max_age_days: int = 7,
) -> dict:
    """Generate/read an enterprise insight (profile, gaps, opportunities, risks).

    Costs 0.5 Credits when generated. ``dry_run=true`` (default) only probes the
    cache and balance without charging; set dry_run=false only after the user
    agrees to spend credits.

    Args:
        cvc_id: CVC model id
        company_name: company from casee_list_cvc_companies
        force_refresh: bypass cache (charges again)
        dry_run: probe only, no generation and no charge
        max_age_days: cache freshness window (1-90)

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
    client = get_report_client()
    params = {
        "company_name": company_name,
        "force_refresh": force_refresh,
        "dry_run": dry_run,
        "max_age_days": max_age_days,
    }
    return _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/enterprise-insight",
            params=params,
        ),
        client,
    )


@mcp.tool()
def casee_get_market_insight(
    cvc_id: str,
    market_name: str,
    force_refresh: bool = False,
    dry_run: bool = True,
    max_age_days: int = 7,
) -> dict:
    """Generate/read a market insight (size, growth, landscape, drivers, risks).

    Costs 0.5 Credits when generated; dry_run=true by default (probe only).

    Args:
        cvc_id: CVC model id
        market_name: market / segment name
        force_refresh: bypass cache
        dry_run: probe only, no charge
        max_age_days: cache freshness window (1-90)

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
    client = get_report_client()
    params = {
        "market_name": market_name,
        "force_refresh": force_refresh,
        "dry_run": dry_run,
        "max_age_days": max_age_days,
    }
    return _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/market-insight",
            params=params,
        ),
        client,
    )


@mcp.tool()
def casee_get_customer_insight(
    cvc_id: str,
    customer_name: str,
    force_refresh: bool = False,
    dry_run: bool = True,
    max_age_days: int = 7,
) -> dict:
    """Generate/read a customer insight (preferences, demand, decision chain).

    Costs 0.5 Credits when generated; dry_run=true by default (probe only).

    Args:
        cvc_id: CVC model id
        customer_name: customer name
        force_refresh: bypass cache
        dry_run: probe only, no charge
        max_age_days: cache freshness window (1-90)

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
    client = get_report_client()
    params = {
        "customer_name": customer_name,
        "force_refresh": force_refresh,
        "dry_run": dry_run,
        "max_age_days": max_age_days,
    }
    return _report_call(
        lambda: client.request(
            "GET", f"/cvc-models/{quote(cvc_id, safe='')}/customer-insight",
            params=params,
        ),
        client,
    )
