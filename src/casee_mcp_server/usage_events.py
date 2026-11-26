"""轻量使用事件上报（免费检索等不扣点调用）。

EIRS 的 ``POST /api/v1/openapi/usage-events`` 只做参数校验与回执，事件本身由
API Key 访问审计落库（``endpoint_id=meta.usage_event``，``mcp_tool`` 取请求头
``X-Casee-Tool``）。这样租户监控能看到「免费能力被真实使用」，又不会产生扣点。

上报是 **fire-and-forget**：后台守护线程 + 短超时，任何失败只记 debug，
绝不影响工具返回。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import httpx

from .reports import resolve_is_api_key, resolve_openapi_base
from .request_context import attribution_headers, get_caller_api_key

logger = logging.getLogger("casee_mcp_server.usage_events")

_TIMEOUT = 2.0


def report_usage_event(
    tool: str,
    *,
    event: str = "free_retrieval",
    status: str = "ok",
    result_count: Optional[int] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """异步上报一次免费调用使用事件；永不抛出异常。"""
    key = get_caller_api_key() or resolve_is_api_key()
    if not key:
        return
    base = resolve_openapi_base()
    if not base:
        return

    payload: dict = {"event": event, "tool": tool, "status": status}
    if result_count is not None:
        payload["result_count"] = int(result_count)
    if duration_ms is not None:
        payload["duration_ms"] = int(duration_ms)

    # 归因头必须在调用方线程内取好：ContextVar 不会传播到后台线程，
    # 若在线程里调用 attribution_headers()，X-Casee-Tool/Session 会全部丢失。
    headers = {"X-API-Key": key, "X-Casee-Call-Purpose": "usage_event"}
    headers.update(attribution_headers())

    def _send() -> None:
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                client.post(f"{base}/usage-events", json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.debug("usage event report failed (ignored): %s", exc)

    threading.Thread(target=_send, daemon=True, name="casee-usage-event").start()
