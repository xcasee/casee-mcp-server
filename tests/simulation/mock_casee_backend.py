"""极简 CaSee 后端桩：让 WorkBuddy ↔ MCP 仿真可以离线跑。

真机仿真的前提是一个可用的 is-server 与 EIRS OpenAPI，但「接入体验」回归
不应该依赖线上数据。本模块用标准库 ``http.server`` 起一个进程内后端，只实现
接入体验需要的最小接口集合：

* 双平面鉴权探测：``GET /v1/stats``、``GET /api/v1/openapi/keys/whoami``
* 免费/计费信息：``GET /api/v1/openapi/credits``、``GET /api/v1/openapi/quota``
* 会话首屏：``GET /api/v1/openapi/home/overview``
* 洞察预检/生成：``GET /api/v1/openapi/cvc-models/{id}/enterprise-insight``
* 三种建链方式：企业检索任务、CVC 创建任务、行业模版四接口
* 检索链路（SDK 直连，返回裸 dict）：
  - ``GET /v1/searchx``：布尔检索（``search_intelligence`` / ``search_with_cvc``），
    带 ``cvc_model_id`` 时把命中条目写入进程内语料库；
  - ``POST /v1/semantic-search``：语义检索（``semantic_search_tool``），
    未采集过的模型返回 0 条，用于验证「先采集再检索」的反向引导；
* 建链后免费预热：``GET /api/v1/openapi/cvc-models/{id}/dashboard|market-trends|companies``
* 免费调用上报：``POST /api/v1/openapi/usage-events``

同时记录每一次请求的方法、路径与请求头，供仿真断言「调用方 Key + 归因头
是否被 MCP server 正确透传」。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

_OPENAPI_PREFIX = "/api/v1/openapi"

_CREDITS = {
    "balance": 12.5,
    "pending_credits": 0,
    "buckets": {"free": 5.0, "monthly": 7.5, "paid": 0.0},
    "ledger_cursor": "sim-cursor",
    "unit": "credit",
    "operations": {
        "insight": {"credits": 0.5, "available": True},
        "report": {"credits": 1.0, "available": True},
    },
    "as_of": "2026-11-26T00:00:00Z",
}

_OVERVIEW = {
    "tenant": {"code": "sim_tenant", "name": "仿真租户"},
    "cvc": {
        "total": 1,
        "by_status": {"completed": 1},
        "building": [],
        "recent": [
            {"cvc_id": "cvc_sim_001", "name": "长时储能竞争格局", "status": "completed"}
        ],
    },
    "reports": {"recent": []},
    "credits": {"balance": 12.5},
    "capabilities": ["insight:generate", "report:generate"],
}

_QUOTA = {
    "key_id": "sim_key",
    "key_prefix": "casee_sim",
    "tenant_code": "sim_tenant",
    "scopes": ["insight:generate", "report:generate"],
    "rate_limit": {"limit": 60, "remaining": 59, "reset_at": "2026-11-26T01:00:00Z"},
    "cvc_models": {"accessible": 1},
    "credits": {"balance": 12.5},
    "products": [{"id": "insight", "enabled": True}],
    "server_time": "2026-11-26T00:00:00Z",
}


def _intel_items(topic: str) -> list[dict[str, Any]]:
    """构造三条可被 sanitize 识别为「情报条目」的检索结果。

    ``sanitize`` 只认同时带 ``link|url`` 与 ``summary|description|abstract`` 的
    条目，并基于投影值重建 citation，所以字段必须按真实检索结果来给。
    """
    label = topic or "长时储能"
    return [
        {
            "title": f"{label}：全钒液流电池产能扩张",
            "link": "https://news.example.com/sim/flow-battery",
            "description": f"{label}领域头部厂商公布新一轮液流电池产能规划。",
            "tscore": 0.86,
            "published_at": "2026-11-20T08:00:00Z",
            "source": {"name": "仿真财经", "category": "finance",
                       "region": "cn", "language": "zh"},
        },
        {
            "title": f"{label}：压缩空气储能项目并网",
            "link": "https://news.example.com/sim/caes",
            "description": "某省压缩空气储能示范项目完成并网，装机规模创纪录。",
            "tscore": 0.74,
            "published_at": "2026-11-18T06:30:00Z",
            "source": {"name": "仿真能源观察", "category": "industry",
                       "region": "cn", "language": "zh"},
        },
        {
            "title": f"{label}：海外储能招标份额变化",
            "link": "https://news.example.com/sim/overseas",
            "description": "欧洲储能招标结果显示亚洲供应商份额继续提升。",
            "tscore": 0.58,
            "published_at": "2026-11-15T11:20:00Z",
            "source": {"name": "SimEnergyDaily", "category": "news",
                       "region": "eu", "language": "en"},
        },
    ]


def _searchx_response(query: dict[str, str]) -> dict[str, Any]:
    """``GET /v1/searchx`` 的裸响应：``{count, items}``（SDK 不剥 wrapper）。"""
    q = query.get("q") or ""
    if "空结果" in q:
        items: list[dict[str, Any]] = []
    else:
        items = _intel_items(q)
    return {
        "search_metadata": {"status": "Success", "query": q},
        "count": len(items),
        "items": items,
    }


def _semantic_response(body: dict[str, Any], collected: set[str]) -> dict[str, Any]:
    """``POST /v1/semantic-search`` 的 SerpAPI 四段式响应。"""
    q = str(body.get("q") or "")
    degraded = "降级" in q
    if degraded or collected:
        items = _intel_items(q)
        fusion = {
            "bm25_hits": 3,
            "vector_hits": 0 if degraded else 2,
            "degraded": degraded,
        }
    else:  # 还没通过 search_with_cvc 采集过语料
        items = []
        fusion = {"bm25_hits": 0, "vector_hits": 0, "degraded": False}
    return {
        "search_metadata": {"status": "Success", "query": q},
        "search_parameters": {
            "cvc_model_id": body.get("cvc_model_id"),
            "q": q,
            "mode": body.get("mode") or "hybrid",
            "top_k": body.get("top_k"),
        },
        "search_information": {"query": q, "fusion": fusion},
        "organic_results": items,
    }


class MockBackend:
    """进程内后端桩，记录收到的全部请求。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        # 已通过 search_with_cvc 采集过语料的 cvc_model_id 集合
        self._collected: dict[str, set[str]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> str:
        backend = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # 静默访问日志
                return

            def do_GET(self) -> None:
                backend._dispatch(self, "GET", b"")

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                backend._dispatch(self, "POST", self.rfile.read(length) if length else b"")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="mock-casee-backend")
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # ---------------------------------------------------------------- asserts
    def find(self, path_suffix: str) -> list[dict[str, Any]]:
        with self._lock:
            return [c for c in self.calls if c["path"].endswith(path_suffix)]

    def last(self, path_suffix: str) -> dict[str, Any] | None:
        found = self.find(path_suffix)
        return found[-1] if found else None

    def collected(self, cvc_id: str) -> set[str]:
        """返回某个 cvc_id 已采集进语料库的条目（link 集合）。"""
        with self._lock:
            return set(self._collected.get(cvc_id, set()))

    # ----------------------------------------------------------------- routes
    def _dispatch(self, handler: BaseHTTPRequestHandler, method: str, body: bytes) -> None:
        parsed = urlparse(handler.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        with self._lock:
            self.calls.append({
                "method": method,
                "path": path,
                "query": query,
                "headers": {k.lower(): v for k, v in handler.headers.items()},
                "body": body.decode("utf-8", "ignore") if body else "",
            })

        if path == "/v1/stats":
            self._send(handler, 200, {"version": "sim-is-2.0", "status": "ok"})
            return
        if path == "/v1/searchx":
            response = _searchx_response(query)
            cvc_id = query.get("cvc_model_id") or ""
            if cvc_id:  # 模拟「采集进价值链语料库」
                with self._lock:
                    bucket = self._collected.setdefault(cvc_id, set())
                    bucket.update(item["link"] for item in response["items"])
            self._send(handler, 200, response)
            return
        if path == "/v1/semantic-search" and method == "POST":
            try:
                body_json = json.loads(body or b"{}")
            except ValueError:
                body_json = {}
            cvc_id = str(body_json.get("cvc_model_id") or "")
            with self._lock:
                collected = set(self._collected.get(cvc_id, set()))
            self._send(handler, 200, _semantic_response(body_json, collected))
            return
        if path == f"{_OPENAPI_PREFIX}/keys/whoami":
            self._send(handler, 200, {"code": 200, "data": {
                "key_id": "sim_key", "tenant_code": "sim_tenant", "scopes": ["*"]}})
            return
        if path == f"{_OPENAPI_PREFIX}/credits":
            self._send(handler, 200, {"code": 200, "data": dict(_CREDITS)})
            return
        if path == f"{_OPENAPI_PREFIX}/quota":
            self._send(handler, 200, {"code": 200, "data": dict(_QUOTA)})
            return
        if path == f"{_OPENAPI_PREFIX}/home/overview":
            self._send(handler, 200, {"code": 200, "data": _OVERVIEW})
            return
        if path == f"{_OPENAPI_PREFIX}/usage-events":
            self._send(handler, 200, {"code": 200, "data": {"accepted": True}})
            return
        if path.startswith(f"{_OPENAPI_PREFIX}/cvc-models/") and path.endswith(
                "/enterprise-insight"):
            self._send(handler, 200, {"code": 200, "data": self._insight(path, query)})
            return
        for suffix in ("/dashboard", "/market-trends", "/companies"):
            if path.startswith(f"{_OPENAPI_PREFIX}/cvc-models/") and path.endswith(suffix):
                cvc_id = path.split("/cvc-models/", 1)[1].rsplit(suffix, 1)[0]
                self._send(handler, 200, {
                    "code": 200, "data": self._warmup(suffix, cvc_id)})
                return
        # ---------------------------------------------------- 三种建链方式
        if path == f"{_OPENAPI_PREFIX}/companies/search-jobs" and method == "POST":
            body = json.loads(body or b"{}")
            job_id = f"csjob_sim_{len(self._jobs) + 1}"
            self._jobs[job_id] = body
            self._send(handler, 200, {"code": 200, "data": {
                "job_id": job_id, "status": "queued", "kind": "company_search",
                "estimated_seconds": 20,
            }})
            return
        if path.startswith(f"{_OPENAPI_PREFIX}/companies/search-jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            request_body = self._jobs.get(job_id, {})
            self._send(handler, 200, {"code": 200, "data": {
                "job_id": job_id, "status": "succeeded", "kind": "company_search",
                "result": {
                    "query": request_body.get("query", ""),
                    "objective_definition": request_body.get("objective_definition"),
                    "items": [
                        {"name": "大连融科储能", "selected": True,
                         "products": ["全钒液流电池"], "markets": ["中国"]},
                        {"name": "宁德时代", "selected": True, "products": ["储能电池"]},
                    ],
                    "total": 2,
                    "default_selection": "all",
                    "suggested_cvc_name": "价值链-长时储能",
                },
            }})
            return
        if path == f"{_OPENAPI_PREFIX}/cvc-models/creation-jobs" and method == "POST":
            body = json.loads(body or b"{}")
            job_id = f"cvcjob_sim_{len(self._jobs) + 1}"
            self._jobs[job_id] = body
            self._send(handler, 200, {"code": 200, "data": {
                "job_id": job_id, "status": "queued", "kind": "cvc_create",
                "estimated_seconds": 30,
            }})
            return
        if path.startswith(f"{_OPENAPI_PREFIX}/cvc-models/creation-jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            request_body = self._jobs.get(job_id, {})
            mode = "precise" if request_body.get("objective_definition") else "smart"
            self._send(handler, 200, {"code": 200, "data": {
                "job_id": job_id, "status": "succeeded", "kind": "cvc_create",
                "result": {
                    "cvc_id": "cvc_sim000001",
                    "status": "draft",
                    "creation_mode": mode,
                    "objective": request_body.get("objective") or request_body.get("name"),
                    "objective_definition": request_body.get("objective_definition"),
                    "build_readiness": {"ready": True, "warnings": []},
                },
            }})
            return
        if path == f"{_OPENAPI_PREFIX}/cvc-templates":
            items = [
                {"id": "tpl_sim_energy", "name": "长时储能竞争格局",
                 "summary": "覆盖电化学/液流/压缩空气等长时储能路线",
                 "industry": "新能源", "scenario": "竞争分析",
                 "companies_preview": ["大连融科储能", "宁德时代", "Fluence"],
                 "provision_state": "not_provisioned", "granted": False,
                 "grants_count": 0},
                {"id": "tpl_sim_semi", "name": "半导体设备国产化",
                 "summary": "北方华创/中微/拓荆等设备厂竞争态势",
                 "industry": "半导体", "scenario": "国产替代",
                 "companies_preview": ["北方华创", "中微公司"],
                 "provision_state": "ready", "granted": True, "grants_count": 12},
            ]
            industry = query.get("industry", "")
            scenario = query.get("scenario", "")
            keyword = query.get("q", "")
            filtered = [
                item for item in items
                if (not industry or item["industry"] == industry)
                and (not scenario or item["scenario"] == scenario)
                and (not keyword or keyword in json.dumps(item, ensure_ascii=False))
            ]
            self._send(handler, 200, {"code": 200, "data": {
                "total": len(filtered), "limit": int(query.get("limit") or 20),
                "offset": int(query.get("offset") or 0), "items": filtered,
            }})
            return
        if path.startswith(f"{_OPENAPI_PREFIX}/cvc-templates/"):
            tail = path.rsplit("/cvc-templates/", 1)[1]
            if tail.endswith("/creation-jobs") and method == "POST":
                template_id = tail.rsplit("/creation-jobs", 1)[0]
                self._send(handler, 202, {"code": 200, "data": {
                    "cvc_id": "cvc_sim_tpl001",
                    "template_id": template_id,
                    "instance_status": "pending",
                    "provision_eta_seconds": 90,
                    "is_first_use": True,
                    "already_granted": False,
                    "creation_mode": "template",
                    "build_status_url":
                        "/api/v1/openapi/cvc-models/cvc_sim_tpl001/build-status",
                    "next": ["get_cvc_template_provision_status", "get_cvc_build_status"],
                }})
                return
            if tail.endswith("/provision-status"):
                template_id = tail.rsplit("/provision-status", 1)[0]
                self._send(handler, 200, {"code": 200, "data": {
                    "template_id": template_id,
                    "cvc_id": "cvc_sim_tpl001",
                    "instance_status": "ready",
                    "build_progress": 100,
                    "current_phase": "ready",
                }})
                return
            self._send(handler, 200, {"code": 200, "data": {
                "id": tail, "name": "长时储能竞争格局",
                "summary": "覆盖电化学/液流/压缩空气等长时储能路线",
                "industry": "新能源", "scenario": "竞争分析",
                "companies_preview": ["大连融科储能", "宁德时代", "Fluence"],
                "provision_state": "not_provisioned", "granted": False,
                "grants_count": 0,
                "creation_endpoint":
                    f"{_OPENAPI_PREFIX}/cvc-templates/{tail}/creation-jobs",
                "provision_status_url":
                    f"{_OPENAPI_PREFIX}/cvc-templates/{tail}/provision-status",
            }})
            return
        self._send(handler, 404, {"code": 40400, "message": f"mock: no route for {path}"})

    @staticmethod
    def _warmup(suffix: str, cvc_id: str) -> dict[str, Any]:
        """建链后免费预热看板：dashboard / market-trends / companies。"""
        if suffix == "/dashboard":
            return {
                "cvc_id": cvc_id,
                "summary": {"intelligence": 12, "entities": 5, "companies": 2},
                "companies": [{"name": "大连融科储能", "share_of_voice": 0.6},
                              {"name": "宁德时代", "share_of_voice": 0.4}],
                "sentiment": {"positive": 0.5, "neutral": 0.4, "negative": 0.1},
                "events": [{"date": "2026-11-20", "count": 3}],
            }
        if suffix == "/market-trends":
            return {
                "cvc_id": cvc_id,
                "trend": [{"date": "2026-11-20", "count": 3},
                          {"date": "2026-11-21", "count": 5}],
                "summary": {"topics": ["液流电池", "压缩空气储能"]},
            }
        return {
            "cvc_id": cvc_id,
            "total": 2,
            "companies": [
                {"name": "大连融科储能", "role": "core", "selected": True},
                {"name": "宁德时代", "role": "competitor", "selected": True},
            ],
        }

    @staticmethod
    def _insight(path: str, query: dict[str, str]) -> dict[str, Any]:
        cvc_id = path.split("/cvc-models/", 1)[1].rsplit("/enterprise-insight", 1)[0]
        dry_run = str(query.get("dry_run", "true")).lower() in ("1", "true", "yes")
        base = {
            "cvc_id": cvc_id,
            "company_name": query.get("company_name", ""),
            "cached": False,
        }
        if dry_run:
            base.update({"status": "probe", "charged_credits": 0})
            return base
        base.update({
            "status": "generated",
            "charged_credits": 0.5,
            "profile": {"summary": "仿真洞察：样本公司具备海外扩张潜力。"},
            "strengths": ["产能布局完整"],
            "gaps": ["海外渠道薄弱"],
            "opportunities": ["欧洲本地化合作"],
            "risks": ["贸易政策波动"],
            "evidence": [{"id": "intel_sim_001", "title": "仿真证据"}],
        })
        return base

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)
