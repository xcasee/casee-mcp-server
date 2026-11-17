"""CaSee OpenAPI client for report / report-job / CVC-control endpoints.

The ``casee`` SDK (1.9.x) covers retrieval and CVC collection administration
but not the OpenAPI report surface (templates / plan / generate / jobs /
share / export / rebuild). This module provides a small httpx-based client
that speaks the same ``{"code", "message", "data"}`` envelope as the REST
gateway and raises ``casee.exceptions`` so ``server._error_payload`` can reuse
the existing structured error contract.

Environment:
    CASEE_IS_API_KEY          Deployment is-server key, sent as ``X-API-Key``
                              (never in the URL). ``CASEE_API_KEY`` is still
                              accepted as a deprecated alias.
    CASEE_API_BASE_URL        Backend root, e.g. ``https://origin.casee.me``.
    CASEE_OPENAPI_BASE_URL    Full OpenAPI base; overrides the derived value.
    CASEE_PORTAL_BASE_URL     Portal origin for share/download links.
    CASEE_REPORTS_TIMEOUT     Read timeout seconds (default 120).
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from casee.constants import DEFAULT_API_BASE_URL
from casee.exceptions import (
    APIError,
    NetworkError,
    RateLimitError,
    ServerError,
    TimeoutError as CaseeTimeoutError,
)

from .request_context import get_caller_api_key

_OPENAPI_SUFFIX = "/api/v1/openapi"
_DEFAULT_TIMEOUT = 120.0
_MAX_RETRIES = 2  # 1 initial attempt + 2 retries
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _backend_base() -> str:
    return (
        os.environ.get("CASEE_API_BASE_URL", "").strip()
        or DEFAULT_API_BASE_URL
    ).rstrip("/")


def resolve_is_api_key() -> str:
    """Return the deployment-level is-server key.

    ``CASEE_IS_API_KEY`` is the canonical name; it is deliberately distinct
    from a caller's tenant key so the fallback credential cannot be mistaken
    for per-request auth. ``CASEE_API_KEY`` remains a deprecated alias for
    existing deployments and the stdio workflow.
    """
    return (
        os.environ.get("CASEE_IS_API_KEY", "").strip()
        or os.environ.get("CASEE_API_KEY", "").strip()
    )


def resolve_openapi_base(base_url: str = "") -> str:
    """Return the ``/api/v1/openapi`` base, tolerating any of the usual roots."""
    explicit = (
        base_url
        or os.environ.get("CASEE_OPENAPI_BASE_URL", "").strip()
    ).rstrip("/")
    base = explicit or _backend_base()
    if base.endswith(_OPENAPI_SUFFIX):
        return base
    if base.endswith("/api/v1"):
        return f"{base}/openapi"
    return f"{base}{_OPENAPI_SUFFIX}"


def resolve_public_base(base_url: str = "") -> str:
    """Return the ``/api/v1`` base used by public (no-key) OpenAPI endpoints.

    ``/api/v1/public/charts/{code}`` and ``/api/v1/public/reports/{code}`` live
    next to the ``/openapi`` prefix, so derive them from the same origin:
    ``https://origin/api/v1/openapi`` -> ``https://origin/api/v1``.
    """
    base = (base_url or resolve_openapi_base("")).rstrip("/")
    if base.endswith(_OPENAPI_SUFFIX):
        return base[: -len("/openapi")]
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"


def resolve_portal_base(base_url: str = "") -> str:
    """Return the portal origin used for human-facing share/report links."""
    explicit = (
        base_url
        or os.environ.get("CASEE_PORTAL_BASE_URL", "").strip()
    ).rstrip("/")
    if explicit:
        return explicit
    base = _backend_base()
    for suffix in (_OPENAPI_SUFFIX, "/api/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def absolute_url(path: str, portal_base: str = "") -> str:
    """Make a backend-relative URL absolute against the portal origin."""
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    base = (portal_base or resolve_portal_base()).rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _parse_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:2000]}


def _raise_for_status(resp: httpx.Response) -> None:
    """Raise an SDK exception that keeps the structured error body intact."""
    status = resp.status_code
    if status < 400:
        return
    body = _parse_body(resp)
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            # Legacy 402/403 bodies put the structured payload under `detail`.
            message = str(
                detail.get("message") or detail.get("msg")
                or detail.get("error") or ""
            )
        else:
            message = str(
                body.get("message") or detail or body.get("error") or ""
            )
        payload = body
    else:
        message = str(body)
        payload = {"raw": body}
    message = message or f"HTTP {status}"

    if status == 429:
        raw = resp.headers.get("retry-after", "")
        retry_after = float(raw) if raw.replace(".", "", 1).isdigit() else None
        error = RateLimitError(message, retry_after=retry_after)
        error.body = payload  # type: ignore[attr-defined]
        raise error
    if status >= 500:
        raise ServerError(message, status_code=status, body=payload)
    # 401/403/404/409/422/... keep the full body so error_code/details survive.
    raise APIError(message, status_code=status, body=payload)


class ReportClient:
    """Minimal synchronous OpenAPI client with retry and request-id capture."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        portal_base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: int = _MAX_RETRIES,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else resolve_is_api_key()
        self.base_url = resolve_openapi_base(base_url or "")
        self.public_base_url = resolve_public_base(self.base_url)
        self.portal_base_url = resolve_portal_base(portal_base_url or base_url or "")
        self.timeout = float(
            timeout
            if timeout is not None
            else os.environ.get("CASEE_REPORTS_TIMEOUT", _DEFAULT_TIMEOUT)
        )
        self.max_retries = max(0, int(max_retries))
        self.last_request_id = ""
        self._transport = transport

    # ------------------------------------------------------------- plumbing
    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "casee-mcp-server",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if extra:
            headers.update({k: v for k, v in extra.items() if v})
        return headers

    @staticmethod
    def _unwrap(body: Any) -> Any:
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        data, _meta = self.request_with_meta(
            method, path, params=params, json_body=json_body,
            extra_headers=extra_headers, timeout=timeout,
        )
        return data

    def request_with_meta(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[Any, Dict[str, Any]]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        clean_params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        started = time.monotonic()
        last_exc: Optional[Exception] = None
        self.last_request_id = ""
        # Only idempotent calls may be retried after a server error/timeout:
        # GET/HEAD, or writes carrying an Idempotency-Key. 429 is safe to
        # retry because the request was rejected before processing.
        idempotent = method.upper() in ("GET", "HEAD") or bool(
            (extra_headers or {}).get("Idempotency-Key")
        )

        with httpx.Client(timeout=timeout or self.timeout, transport=self._transport) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = client.request(
                        method, url, params=clean_params or None,
                        json=json_body, headers=self._headers(extra_headers),
                    )
                except httpx.TimeoutException as exc:
                    last_exc = CaseeTimeoutError(f"{method} {path} timed out: {exc}")
                    if not idempotent or attempt >= self.max_retries:
                        raise last_exc
                except httpx.HTTPError as exc:
                    last_exc = NetworkError(f"{method} {path} failed: {exc}")
                    if not idempotent or attempt >= self.max_retries:
                        raise last_exc
                else:
                    body = _parse_body(resp)
                    rid = ""
                    if isinstance(body, dict):
                        rid = str(body.get("request_id") or "")
                    rid = rid or resp.headers.get("x-request-id", "")
                    if rid:
                        self.last_request_id = rid
                    if resp.status_code < 400:
                        return self._unwrap(body), {
                            "status_code": resp.status_code,
                            "elapsed_ms": int((time.monotonic() - started) * 1000),
                            "request_id": self.last_request_id,
                        }
                    retryable = resp.status_code == 429 or (
                        resp.status_code in _RETRY_STATUS and idempotent
                    )
                    if retryable and attempt < self.max_retries:
                        retry_after = resp.headers.get("retry-after", "")
                        delay = (
                            float(retry_after)
                            if retry_after.replace(".", "", 1).isdigit()
                            else 0.5 * (2 ** attempt)
                        )
                        time.sleep(min(delay, 10.0))
                        continue
                    _raise_for_status(resp)

                if attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** attempt))

        assert last_exc is not None
        raise last_exc

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        public: bool = False,
        max_bytes: int = 100 * 1024 * 1024,
        accept: str = "*/*",
    ) -> Tuple[bytes, Dict[str, Any]]:
        """Fetch a raw artifact (report HTML/Markdown, OWL, JSONL export).

        Returns ``(content, meta)`` where meta carries status, request id and
        the response headers needed for filenames / media types. Errors reuse
        the same ``casee.exceptions`` mapping as :meth:`request`.
        """
        base = self.public_base_url if public else self.base_url
        url = f"{base}/{path.lstrip('/')}"
        clean_params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        started = time.monotonic()
        last_exc: Optional[Exception] = None
        self.last_request_id = ""
        idempotent = method.upper() in ("GET", "HEAD") or bool(
            (extra_headers or {}).get("Idempotency-Key")
        )
        headers = self._headers({"Accept": accept, **(extra_headers or {})})

        with httpx.Client(timeout=timeout or self.timeout, transport=self._transport) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = client.request(
                        method, url, params=clean_params or None,
                        json=json_body, headers=headers,
                    )
                except httpx.TimeoutException as exc:
                    last_exc = CaseeTimeoutError(f"{method} {path} timed out: {exc}")
                    if not idempotent or attempt >= self.max_retries:
                        raise last_exc
                except httpx.HTTPError as exc:
                    last_exc = NetworkError(f"{method} {path} failed: {exc}")
                    if not idempotent or attempt >= self.max_retries:
                        raise last_exc
                else:
                    rid = resp.headers.get("x-request-id", "")
                    if rid:
                        self.last_request_id = rid
                    if resp.status_code < 400:
                        content = resp.content
                        if max_bytes and len(content) > max_bytes:
                            raise APIError(
                                f"{method} {path} response exceeds the {max_bytes} byte limit",
                                status_code=413,
                                body={
                                    "code": "ARTIFACT_TOO_LARGE",
                                    "message": "Artifact exceeds the configured download limit",
                                    "details": {"bytes": len(content), "max_bytes": max_bytes},
                                },
                            )
                        content_type = resp.headers.get("content-type", "")
                        charset = "utf-8"
                        for part in content_type.split(";")[1:]:
                            key, _, value = part.strip().partition("=")
                            if key.lower() == "charset" and value:
                                charset = value.strip("\"'")
                        return content, {
                            "status_code": resp.status_code,
                            "elapsed_ms": int((time.monotonic() - started) * 1000),
                            "request_id": self.last_request_id,
                            "content_type": content_type,
                            "charset": charset,
                            "content_disposition": resp.headers.get(
                                "content-disposition", "",
                            ),
                            "etag": resp.headers.get("etag", "").strip('"'),
                            "total_count": resp.headers.get("x-total-count", ""),
                            "headers": {
                                k: v for k, v in resp.headers.items()
                                if k.lower() in (
                                    "content-type", "content-disposition", "etag",
                                    "x-total-count", "x-request-id", "x-ratelimit-remaining",
                                )
                            },
                        }
                    retryable = resp.status_code == 429 or (
                        resp.status_code in _RETRY_STATUS and idempotent
                    )
                    if retryable and attempt < self.max_retries:
                        retry_after = resp.headers.get("retry-after", "")
                        delay = (
                            float(retry_after)
                            if retry_after.replace(".", "", 1).isdigit()
                            else 0.5 * (2 ** attempt)
                        )
                        time.sleep(min(delay, 10.0))
                        continue
                    _raise_for_status(resp)

                if attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** attempt))

        assert last_exc is not None
        raise last_exc

    def request_text(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Tuple[str, Dict[str, Any]]:
        """Like :meth:`request_bytes` but decodes to text using the charset."""
        content, meta = self.request_bytes(method, path, **kwargs)
        return content.decode(meta.get("charset") or "utf-8", errors="replace"), meta

    # -------------------------------------------------------------- helpers
    def absolute_url(self, path: str) -> str:
        """Resolve a backend-relative path against this client's portal base."""
        return absolute_url(path, self.portal_base_url)

    def export_summary(self, cvc_id: str) -> Dict[str, Any]:
        """Stream the intelligence export and return metadata only."""
        url = f"{self.base_url}/cvc-models/{cvc_id}/intelligence/export"
        with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
            with client.stream(
                "GET", url, params={"format": "jsonl"},
                headers=self._headers(),
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    _raise_for_status(resp)
                rid = resp.headers.get("x-request-id", "")
                if rid:
                    self.last_request_id = rid
                return {
                    "cvc_id": cvc_id,
                    "format": "jsonl",
                    "count": int(resp.headers.get("x-total-count") or 0),
                    "etag": resp.headers.get("etag", "").strip('"'),
                    "bytes": int(resp.headers.get("content-length") or 0),
                    "export_path": f"/cvc-models/{cvc_id}/intelligence/export",
                }


_default_client: Optional[ReportClient] = None


def get_report_client() -> ReportClient:
    """Return a client bound to the verified caller key when there is one.

    Streamable-HTTP requests carry the caller's ``X-API-Key``; using it for
    outbound calls keeps tenant isolation, Credits billing and revocation with
    the caller. A new client per request keeps ``last_request_id`` scoped to
    that call and performs no I/O. stdio mode falls back to the cached
    ``CASEE_IS_API_KEY`` client.
    """
    caller_key = get_caller_api_key()
    if caller_key:
        return ReportClient(api_key=caller_key)
    global _default_client
    if _default_client is None:
        _default_client = ReportClient()
    return _default_client


def reset_report_client() -> None:
    """Drop the singleton so the next call re-reads environment variables."""
    global _default_client
    _default_client = None
