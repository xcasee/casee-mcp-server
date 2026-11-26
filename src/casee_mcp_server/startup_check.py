"""Boot-time self-check of the two CaSee API planes.

Retrieval tools call is-server (``CASEE_API_BASE_URL``); report and
CVC-management tools call the EIRS OpenAPI plane (``CASEE_OPENAPI_BASE_URL``).
The two planes keep independent key stores, so a key valid on only one side
makes half of the tools fail with ``40102`` while the rest keep working. The
gateway therefore probes both at boot and prints one actionable summary
instead of letting agents discover the split at runtime.

Strictness is controlled by ``MCP_STARTUP_CHECK_STRICT`` (default off): the
check always logs a summary, and optionally aborts startup when either plane is
unusable.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

import httpx

from casee.constants import DEFAULT_API_BASE_URL

from .reports import resolve_is_api_key, resolve_openapi_base

logger = logging.getLogger("casee_mcp_server.startup")

_PROBE_TIMEOUT = 5.0


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _mask(key: str) -> str:
    if len(key) <= 10:
        return key[:4] + "…" if key else "<empty>"
    return key[:10] + "…"


def _is_base() -> str:
    return (
        os.environ.get("CASEE_API_BASE_URL", "").strip()
        or DEFAULT_API_BASE_URL
    ).rstrip("/")


def _probe(url: str, api_key: str) -> Dict[str, Any]:
    """GET a probe URL and normalise the outcome (never raises)."""
    try:
        resp = httpx.get(
            url,
            headers={
                "X-API-Key": api_key,
                "X-Casee-Call-Purpose": "auth_probe",
                "X-Casee-Client": "casee-mcp-server",
            },
            timeout=_PROBE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "status": None, "detail": f"unreachable: {exc}"}

    detail = ""
    if resp.status_code == 200:
        ctype = (resp.headers.get("content-type") or "").lower()
        if "json" not in ctype:
            return {
                "ok": False,
                "status": 200,
                "detail": (
                    f"non-JSON response ({ctype or 'unknown content-type'}); "
                    "the base URL probably points at the portal/CDN, not the API"
                ),
            }
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if isinstance(body, dict):
            parts = [
                f"code={body.get('code')}" if body.get("code") is not None else "",
                f"reason={(body.get('details') or {}).get('reason')}"
                if isinstance(body.get("details"), dict)
                else "",
                str(body.get("message") or ""),
            ]
            detail = " ".join(p for p in parts if p)
        if not detail:
            detail = resp.text[:200]
    return {"ok": resp.status_code == 200, "status": resp.status_code, "detail": detail}


def run_startup_self_check(transport: str) -> bool:
    """Probe both planes and log one summary; returns True when both are usable."""
    api_key = resolve_is_api_key()
    is_base = _is_base()
    openapi_base = resolve_openapi_base()

    logger.info(
        "startup-check transport=%s key=%s is=%s eirs=%s",
        transport, _mask(api_key), is_base, openapi_base,
    )

    if not os.environ.get("CASEE_IS_API_KEY", "").strip() and api_key:
        logger.warning(
            "startup-check note: CASEE_API_KEY is deprecated; rename it to "
            "CASEE_IS_API_KEY to distinguish the deployment key from tenant keys."
        )

    if not api_key:
        logger.error(
            "startup-check FAIL: CASEE_IS_API_KEY is empty; outbound CaSee calls "
            "have no credential (stdio/anonymous requests cannot work)."
        )
        return False

    is_result = _probe(f"{is_base}/v1/stats", api_key)
    eirs_result = _probe(f"{openapi_base}/keys/whoami", api_key)

    for label, url, result in (
        ("is-server", f"{is_base}/v1/stats", is_result),
        ("eirs-openapi", f"{openapi_base}/keys/whoami", eirs_result),
    ):
        if result["ok"]:
            logger.info("startup-check %-13s %s -> 200 OK", label, url)
        else:
            logger.error(
                "startup-check %-13s %s -> %s %s",
                label, url, result["status"] or "ERR", result["detail"],
            )

    if api_key.startswith("sk_"):
        logger.warning(
            "startup-check note: CASEE_IS_API_KEY starts with 'sk_'; keys created "
            "directly on is-server are usually not registered in the EIRS key "
            "store and will fail report / CVC-management tools with 40102."
        )

    if transport == "streamable-http":
        logger.info(
            "startup-check note: each caller's X-API-Key is forwarded to both "
            "planes; CASEE_IS_API_KEY is only the stdio/anonymous fallback."
        )

    planes_ok = int(is_result["ok"]) + int(eirs_result["ok"])
    if planes_ok == 2:
        logger.info("startup-check result: OK (2/2 planes usable)")
        return True

    logger.error("startup-check result: FAIL (%s/2 planes usable)", planes_ok)
    if not eirs_result["ok"]:
        logger.error(
            "startup-check action: report/CVC tools need a tenant key from "
            "https://www.casee.me (系统配置 → API Keys); it is registered in "
            "both is-server and EIRS. IS-only keys cannot access OpenAPI."
        )
    if not is_result["ok"]:
        logger.error(
            "startup-check action: verify CASEE_API_BASE_URL points at the "
            "is-server FastAPI port (not the portal/CDN) and that the key is "
            "active."
        )
    return False


def is_strict() -> bool:
    """Whether a failed self-check should abort startup.

    Off by default so an already-serving gateway is never taken down by a
    transient probe failure; set ``MCP_STARTUP_CHECK_STRICT=true`` to fail fast.
    """
    return _env_bool("MCP_STARTUP_CHECK_STRICT", False)
