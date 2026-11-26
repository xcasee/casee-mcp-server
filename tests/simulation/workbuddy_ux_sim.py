#!/usr/bin/env python3
"""WorkBuddy ↔ MCP 接入体验仿真（4.2 回归，可离线运行）。

为什么需要它
-----------
4.2 的四项优化（默认鉴权、工具分层、首屏免费引导、一次确认令牌）都发生在
「客户端接入阶段」，只有按 WorkBuddy 的真实接入顺序（initialize → tools/list →
tools/call）跑一遍才能观察到。单元测试各自命中单个函数，无法发现「协议层能过、
但工具目录/引导/错误提示对 Agent 不可用」这类体验问题。

与 ``workbuddy_sim.py`` 的区别
------------------------------
``workbuddy_sim.py`` 连真实后端，用于上线前联调；本脚本用 ``mock_casee_backend``
在进程内起一个后端桩，**不依赖线上 Key 与数据**，因此可以随时回归：

1. 用真实 MCP server 子进程（streamable HTTP + 默认鉴权）承载协议；
2. 用官方 MCP client 扮演 WorkBuddy；
3. 断言 4.2 的接入体验契约：
   - 缺 Key 时 401 且 ``error.data`` 含 error_code / hint / action / retryable；
   - ``MCP_TOOL_PROFILE=core`` 仅列出 18 个主链路工具，``full`` 列出全部，
     隐藏工具仍可直接调用；
   - ``casee_guide`` 首屏给出免费第一步与工具分层摘要；
   - 三种建链方式（智能推理 / 精确定义 / 行业模版）的入参与下一步指引完整；
   - 洞察 ``dry_run`` 返回 ``confirm_token``，同参数带令牌一次执行成功；
   - 参数错误在调用前就被拦下，并给出可执行的 hint/action；
   - 调用方 Key 与 ``X-Casee-Client/Tool/Session`` 归因头确实透传到后端。

运行
----
    cd design/CaSee-v2.0/AIAgent/casee_mcp_server
    PYTHONPATH=src uv run --no-project --with mcp --with httpx --with starlette \\
        --with ./casee-2.0.0-py3-none-any.whl --python 3.12 \\
        python tests/simulation/workbuddy_ux_sim.py

    --keep-logs  保留 MCP server 日志并打印路径
    --json       打印机器可读结果

退出码 0 表示全部通过。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:  # SDK（部署环境已安装）
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
except ImportError as exc:  # pragma: no cover - 环境守卫
    print(f"仿真需要 MCP SDK（mcp + httpx2）：{exc}", file=sys.stderr)
    raise SystemExit(2)

from mock_casee_backend import MockBackend

PKG_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PKG_ROOT / "src"

CLIENT_NAME = "workbuddy"
CLIENT_VERSION = "2.0.0"
SIM_API_KEY = "casee_sim_workbuddy_key"
SIM_COMPANY = "仿真样本公司"

_VALID_TIERS = {"T0", "T1", "T2", "T3"}


# ---------------------------------------------------------------------------
# 结果收集
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def check(self, area: str, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append({"area": area, "name": name, "ok": bool(ok), "detail": detail})
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {area} :: {name}"
        if detail and not ok:
            line += f" — {detail}"
        print(line)
        return ok

    def note(self, text: str) -> None:
        print(f"[INFO] {text}")

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if not r["ok"]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": len(self.rows),
            "passed": len(self.rows) - len(self.failed),
            "failed": len(self.failed),
            "checks": self.rows,
        }


def payload_of(result: Any) -> dict[str, Any]:
    """从 CallToolResult 里取出工具返回的 dict。"""
    if isinstance(result, dict):
        return result
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"_raw_text": text[:400]}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    return {}


def recommendations_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    recs = (data or {}).get("recommendations")
    if recs is None:
        recs = payload.get("recommendations")
    return [r for r in (recs or []) if isinstance(r, dict)]


def option_shape_ok(opt: Any) -> bool:
    if not isinstance(opt, dict):
        return False
    for key in ("option_id", "title", "why", "steps", "cost_credits", "eta_seconds"):
        if key not in opt:
            return False
    return bool(opt.get("steps")) and isinstance(opt["steps"][0].get("tool"), str)


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

async def scenario_auth(report: Report, mock: MockBackend, mcp_url: str, key: str) -> None:
    """默认鉴权：缺 Key 必须 401，且错误体对 Agent 可执行。"""
    area = "4.2 默认鉴权"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    async with httpx2.AsyncClient(timeout=15) as client:
        resp = await client.post(mcp_url, json=body, headers=headers)
    payload: dict[str, Any] = {}
    try:
        payload = resp.json()
    except ValueError:
        payload = {"_raw": resp.text[:300]}

    report.check(area, "缺少 X-API-Key 返回 401", resp.status_code == 401,
                 f"status={resp.status_code} body={str(payload)[:200]}")
    err = (payload or {}).get("error") or {}
    data = err.get("data") or {}
    report.check(area, "错误码 AUTH_MISSING_KEY",
                 data.get("error_code") == "AUTH_MISSING_KEY", str(data)[:200])
    report.check(area, "带可执行 action=provide_api_key",
                 data.get("action") == "provide_api_key", str(data)[:200])
    report.check(area, "带 hint 指引与 retryable=false",
                 bool(data.get("hint")) and data.get("retryable") is False, str(data)[:200])

    init_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        },
    }
    handshake_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "X-API-Key": key,
        "X-Casee-Client": CLIENT_NAME,
        "X-Casee-Client-Version": CLIENT_VERSION,
    }
    async with httpx2.AsyncClient(timeout=15) as client:
        resp = await client.post(mcp_url, json=init_body, headers=handshake_headers)
    report.check(area, "携带有效 Key 可完成 initialize", resp.status_code == 200,
                 f"status={resp.status_code} body={resp.text[:200]}")

    whoami = mock.last("/api/v1/openapi/keys/whoami")
    stats = mock.last("/v1/stats")
    report.check(area, "双平面鉴权确实探测（is-server + EIRS）",
                 whoami is not None and stats is not None
                 and whoami["headers"].get("x-api-key") == key,
                 f"whoami={bool(whoami)} stats={bool(stats)}")


async def scenario_profile_core(session: ClientSession, report: Report,
                                core_names: frozenset[str],
                                all_names: list[str]) -> None:
    area = "4.2 工具分层 core"
    tools = (await session.list_tools()).tools
    names = {t.name for t in tools}
    report.check(area, f"tools/list 只暴露核心 {len(core_names)} 个",
                 names == set(core_names), f"count={len(names)} "
                 f"extra={sorted(names - core_names)[:5]} "
                 f"missing={sorted(core_names - names)[:5]}")
    report.check(area, "默认不列出长尾工具",
                 not (names & (set(all_names) - set(core_names))),
                 str(sorted(names & (set(all_names) - set(core_names)))[:5]))

    empty_desc = [t.name for t in tools if not (t.description or "").strip()]
    report.check(area, "核心工具都有描述", not empty_desc, str(empty_desc[:5]))
    bad_tier = [t.name for t in tools
                if str((t.meta or {}).get("tier", "")) not in _VALID_TIERS]
    report.check(area, "核心工具声明 _meta.tier", not bad_tier, str(bad_tier[:5]))

    hidden = [n for n in all_names if n not in core_names]
    res = payload_of(await session.call_tool("casee_guide", {}))
    profile = (res.get("data") or {}).get("tool_profile") or {}
    report.check(area, "casee_guide 暴露 tool_profile", profile.get("profile") == "core",
                 str(profile)[:240])
    report.check(area, "tool_profile 告知隐藏清单与开启方式",
                 profile.get("hidden_tools") == hidden
                 and "MCP_TOOL_PROFILE=full" in str(profile.get("enable_full", "")),
                 str(profile)[:240])

    hidden_tool = "casee_get_quota"
    if hidden_tool in hidden:
        called = payload_of(await session.call_tool(hidden_tool, {}))
        report.check(area, f"隐藏工具 {hidden_tool} 仍可调用", called.get("ok") is True,
                     str(called)[:240])
    else:  # pragma: no cover - 分层表调整时提示更新用例
        report.check(area, "选择一个隐藏工具做可调用性验证", False,
                     "casee_get_quota 已不在隐藏集合，请更换样本工具")


async def scenario_profile_full(session: ClientSession, report: Report,
                                all_names: list[str]) -> None:
    area = "4.2 工具分层 full"
    tools = (await session.list_tools()).tools
    names = {t.name for t in tools}
    report.check(area, f"MCP_TOOL_PROFILE=full 暴露全部 {len(all_names)} 个",
                 names == set(all_names), f"count={len(names)}")
    res = payload_of(await session.call_tool("casee_guide", {}))
    profile = (res.get("data") or {}).get("tool_profile") or {}
    report.check(area, "full 模式无隐藏工具",
                 profile.get("profile") == "full" and profile.get("hidden_tools") == [],
                 str(profile)[:200])


async def scenario_guide(session: ClientSession, report: Report) -> None:
    """首屏引导：先免费检索，再决定付费。"""
    area = "4.2 首屏引导"
    res = payload_of(await session.call_tool("casee_guide", {}))
    data = res.get("data") or {}
    report.check(area, "casee_guide ok", res.get("ok") is True, str(res)[:200])

    free_first = data.get("free_first") or {}
    try_this = free_first.get("try_this") or {}
    report.check(area, "首屏给出 free_first 免费第一步",
                 try_this.get("tool") == "search_intelligence"
                 and float(try_this.get("cost_credits") or 0) == 0,
                 str(free_first)[:240])
    report.check(area, "free_first 带可直接执行的参数",
                 isinstance(try_this.get("args"), dict) and bool(try_this["args"].get("q")),
                 str(try_this)[:200])

    checklist = data.get("first_time_checklist") or []
    report.check(area, "checklist 第一步是免费检索",
                 bool(checklist) and "search_intelligence" in str(checklist[0])
                 and "免费" in str(checklist[0]), str(checklist[:1]))

    tiers = {t.get("tier") for t in data.get("capability_tiers") or []}
    report.check(area, "能力分层 T0-T3 齐全", tiers == {"T0", "T1", "T2", "T3"}, str(tiers))

    themed = payload_of(await session.call_tool("casee_guide", {"topic": "insight"}))
    themed_data = themed.get("data") or {}
    report.check(area, "topic 过滤后仍带 tool_profile",
                 bool(themed_data.get("tool_profile")), str(themed_data)[:160])


async def scenario_plan(session: ClientSession, report: Report) -> None:
    area = "4.2 方案预检"
    res = payload_of(await session.call_tool(
        "casee_plan_task", {"goal": "帮我分析一下宁德时代的风险和差距"}))
    data = res.get("data") or {}
    options = data.get("options") or []
    report.check(area, "casee_plan_task ok", res.get("ok") is True, str(res)[:200])
    report.check(area, "给出 1-3 个方案", 1 <= len(options) <= 3, f"len={len(options)}")
    report.check(area, "方案字段契约完整", all(option_shape_ok(o) for o in options),
                 str([o.get("option_id") for o in options]))
    report.check(area, "至少一个 0 credit 起步方案",
                 any(float(o.get("cost_credits") or 0) == 0 for o in options),
                 str([o.get("cost_credits") for o in options]))
    report.check(area, "给出 ask_user 澄清项", bool(data.get("ask_user")),
                 str(data.get("ask_user"))[:120])


async def scenario_credits(session: ClientSession, report: Report) -> None:
    area = "4.2 余额与免费能力"
    res = payload_of(await session.call_tool("casee_get_credits", {}))
    data = res.get("data") or {}
    report.check(area, "casee_get_credits ok", res.get("ok") is True, str(res)[:200])
    report.check(area, "给出首条 CVC 免费提示", bool(data.get("first_cvc_free")),
                 str(data)[:160])
    report.check(area, "列出免费能力", len(data.get("free_capabilities") or []) >= 5)
    report.check(area, "推荐不超过 3 条", len(recommendations_of(res)) <= 3)


async def scenario_overview(session: ClientSession, report: Report) -> None:
    area = "4.2 会话首屏总览"
    res = payload_of(await session.call_tool("casee_home_overview", {"limit": 3}))
    data = res.get("data") or {}
    report.check(area, "casee_home_overview ok", res.get("ok") is True, str(res)[:200])
    report.check(area, "onboarding 引导存在", len(data.get("onboarding") or []) >= 1,
                 str(data.get("onboarding"))[:160])
    report.check(area, "pending_jobs 字段为列表",
                 isinstance(data.get("pending_jobs"), list),
                 str(type(data.get("pending_jobs"))))
    report.check(area, "推荐不超过 3 条", len(recommendations_of(res)) <= 3)


async def scenario_confirm_token(session: ClientSession, report: Report) -> None:
    """洞察：dry_run 一次预检 → 同参数带令牌直接执行。"""
    area = "4.2 一次确认令牌"
    args = {"cvc_id": "cvc_sim_001", "company_name": SIM_COMPANY}
    probe = payload_of(await session.call_tool(
        "casee_get_enterprise_insight", {**args, "dry_run": True}))
    data = probe.get("data") or {}
    cost_note = data.get("cost_note") or {}
    token = str(data.get("confirm_token") or "")
    report.check(area, "dry_run ok 且未扣费",
                 probe.get("ok") is True and cost_note.get("dry_run") is True
                 and float(cost_note.get("charged_credits") or 0) == 0,
                 str(data)[:240])
    report.check(area, "dry_run 返回 confirm_token", bool(token), str(data)[:200])
    report.check(area, "confirm_token 有有效期",
                 int(data.get("confirm_token_expires_in") or 0) > 0,
                 str(data.get("confirm_token_expires_in")))
    confirm_call = data.get("confirm_call") or {}
    call_args = confirm_call.get("args") or {}
    report.check(area, "confirm_call 可直接执行",
                 confirm_call.get("tool") == "casee_get_enterprise_insight"
                 and call_args.get("dry_run") is False
                 and call_args.get("confirm_token") == token
                 and call_args.get("company_name") == SIM_COMPANY,
                 str(confirm_call)[:240])
    rec_ids = [str(r.get("id")) for r in recommendations_of(probe)]
    report.check(area, "推荐 confirm_insight", "confirm_insight" in rec_ids, str(rec_ids))

    executed = payload_of(await session.call_tool(
        "casee_get_enterprise_insight", {**args, "dry_run": False, "confirm_token": token}))
    exec_data = executed.get("data") or {}
    exec_cost = exec_data.get("cost_note") or {}
    report.check(area, "带令牌执行成功并标记 confirmed",
                 executed.get("ok") is True and exec_data.get("confirmed") is True,
                 str(exec_data)[:240])
    report.check(area, "确认来源与计费可见",
                 exec_cost.get("confirmation") == "confirm_token"
                 and float(exec_cost.get("charged_credits") or 0) == 0.5,
                 str(exec_cost)[:200])

    tampered = payload_of(await session.call_tool(
        "casee_get_enterprise_insight",
        {**args, "company_name": "另一家公司", "dry_run": False, "confirm_token": token}))
    tampered_data = tampered.get("data") or {}
    report.check(area, "令牌与参数绑定（换参数不复用）",
                 tampered_data.get("confirmed") is not True, str(tampered_data)[:240])


async def scenario_errors(session: ClientSession, report: Report) -> None:
    area = "4.2 错误前置可执行"
    bad = payload_of(await session.call_tool(
        "semantic_search_tool", {"cvc_model_id": "bad id/../x", "q": "测试"}))
    report.check(area, "非法 cvc_id -> VALIDATION_ERROR",
                 bad.get("ok") is False and bad.get("error_code") == "VALIDATION_ERROR",
                 str(bad)[:240])
    report.check(area, "非法 cvc_id -> hint + action",
                 bool(bad.get("hint")) and bool(bad.get("action")), str(bad)[:240])

    missing = payload_of(await session.call_tool(
        "casee_get_enterprise_insight", {"cvc_id": "cvc_sim_001", "company_name": " "}))
    report.check(area, "缺少 company_name -> VALIDATION_ERROR + action",
                 missing.get("ok") is False
                 and missing.get("error_code") == "VALIDATION_ERROR"
                 and bool(missing.get("action")), str(missing)[:240])

    bad_range = payload_of(await session.call_tool(
        "casee_get_market_insight",
        {"cvc_id": "cvc_sim_001", "market_name": "储能", "max_age_days": 0}))
    report.check(area, "非法 max_age_days 被调用前拦下",
                 bad_range.get("ok") is False
                 and bad_range.get("error_code") == "VALIDATION_ERROR", str(bad_range)[:240])


async def scenario_creation_modes(session: ClientSession, report: Report,
                                  mock: MockBackend) -> None:
    """三种建链方式：智能推理 / 精确定义 / 行业模版。"""
    area = "4.3 建链方式"

    # ---------------------------------------------------------- 智能推理
    smart = payload_of(await session.call_tool("create_cvc_model", {
        "name": "长时储能智能建链",
        "companies": [{"name": "大连融科储能"}],
        "objective": "跟踪长时储能技术路线与主要厂商竞争格局",
    }))
    smart_data = smart.get("data") or {}
    smart_post = mock.last("/api/v1/openapi/cvc-models/creation-jobs")
    smart_body = json.loads(smart_post["body"]) if smart_post else {}
    report.check(area, "智能推理 objective 透传到后端",
                 smart_body.get("objective") == "跟踪长时储能技术路线与主要厂商竞争格局",
                 str(smart_body)[:240])
    report.check(area, "智能推理 creation_mode=smart",
                 smart.get("ok") is True and smart_data.get("creation_mode") == "smart",
                 str(smart_data)[:240])
    report.check(area, "创建后下一步是 rebuild",
                 (smart_data.get("next_step") or {}).get("tool") == "rebuild_cvc_model",
                 str(smart_data.get("next_step"))[:200])

    # ---------------------------------------------------------- 精确定义
    definition = {
        "mode": "precise",
        "target_regions": ["中国", "欧洲"],
        "target_customers": "储能系统集成商",
        "target_competitors": "宁德时代",
        "target_industry_standards": "",
    }
    precise = payload_of(await session.call_tool("create_cvc_model", {
        "name": "长时储能精确定义建链",
        "companies": ["大连融科储能"],
        "objective_definition": definition,
    }))
    precise_data = precise.get("data") or {}
    expected = "目标地区：中国、欧洲；目标客户：储能系统集成商；目标竞品：宁德时代"
    report.check(area, "精确定义按 地区→客户→竞品 组合",
                 precise_data.get("objective") == expected, str(precise_data)[:240])
    report.check(area, "精确定义 creation_mode=precise",
                 precise.get("ok") is True and precise_data.get("creation_mode") == "precise",
                 str(precise_data)[:200])
    report.check(area, "精确定义 version=1 且结构回显",
                 (precise_data.get("objective_definition") or {}).get("version") == 1,
                 str(precise_data.get("objective_definition"))[:200])

    empty_precise = payload_of(await session.call_tool("create_cvc_model", {
        "name": "空精确定义",
        "companies": ["大连融科储能"],
        "objective_definition": {"mode": "precise"},
    }))
    report.check(area, "精确定义四维全空被拦下",
                 empty_precise.get("ok") is False
                 and empty_precise.get("error_code") == "VALIDATION_ERROR",
                 str(empty_precise)[:240])

    # 精确定义同样可用于建链前的企业检索
    searched = payload_of(await session.call_tool("search_cvc_companies", {
        "query": "",
        "objective_definition": {"mode": "precise", "target_regions": ["中国"],
                                 "target_customers": "储能系统集成商"},
    }))
    search_data = searched.get("data") or {}
    search_post = mock.last("/api/v1/openapi/companies/search-jobs")
    search_body = json.loads(search_post["body"]) if search_post else {}
    report.check(area, "企业检索任务接受精确定义",
                 searched.get("ok") is True
                 and search_body.get("objective_definition", {}).get("mode") == "precise",
                 str(search_body)[:240])
    report.check(area, "检索 query 由精确定义组合",
                 search_data.get("query") == "目标地区：中国；目标客户：储能系统集成商",
                 str(search_data.get("query"))[:200])

    # ---------------------------------------------------------- 行业模版
    listed = payload_of(await session.call_tool("list_cvc_templates", {"limit": 5}))
    list_data = listed.get("data") or {}
    items = list_data.get("items") or []
    report.check(area, "模版列表可列出（含 preview 与授权状态）",
                 listed.get("ok") is True and bool(items)
                 and "granted" in items[0] and bool(items[0].get("companies_preview")),
                 str(list_data)[:240])
    filtered = payload_of(await session.call_tool(
        "list_cvc_templates", {"industry": "半导体"}))
    report.check(area, "模版列表支持行业过滤",
                 [i.get("id") for i in (filtered.get("data") or {}).get("items") or []]
                 == ["tpl_sim_semi"], str(filtered.get("data"))[:200])

    template_id = str(items[0].get("id") or "tpl_sim_energy")
    detail = payload_of(await session.call_tool("get_cvc_template", {"template_id": template_id}))
    detail_data = detail.get("data") or {}
    report.check(area, "模版详情含创建与进度入口",
                 detail.get("ok") is True and detail_data.get("id") == template_id
                 and bool(detail_data.get("creation_endpoint")),
                 str(detail_data)[:240])

    preview = payload_of(await session.call_tool(
        "create_cvc_from_template", {"template_id": template_id}))
    preview_data = preview.get("data") or {}
    token = str(preview_data.get("confirm_token") or "")
    cost_note = preview_data.get("cost_note") or {}
    report.check(area, "模版 dry_run 默认不实例化",
                 preview.get("ok") is True and preview_data.get("dry_run") is True
                 and not mock.find(f"/cvc-templates/{template_id}/creation-jobs"),
                 str(preview_data)[:240])
    report.check(area, "模版 dry_run 返回成本与令牌",
                 float(cost_note.get("credits") or 0) == 2.5 and bool(token),
                 str(cost_note)[:200])
    confirm_call = preview_data.get("confirm_call") or {}
    report.check(area, "模版 confirm_call 可直接执行",
                 confirm_call.get("tool") == "create_cvc_from_template"
                 and (confirm_call.get("args") or {}).get("dry_run") is False,
                 str(confirm_call)[:200])
    report.check(area, "模版 dry_run 推荐 confirm_template_create",
                 "confirm_template_create" in [str(r.get("id"))
                                               for r in recommendations_of(preview)],
                 str([r.get("id") for r in recommendations_of(preview)]))

    created = payload_of(await session.call_tool("create_cvc_from_template", {
        "template_id": template_id, "dry_run": False, "confirm_token": token}))
    created_data = created.get("data") or {}
    report.check(area, "带令牌确认后返回 202 实例",
                 created.get("ok") is True
                 and created_data.get("creation_mode") == "template"
                 and created_data.get("instance_status") == "pending",
                 str(created_data)[:240])
    report.check(area, "模版建链下一步轮询准备状态",
                 (created_data.get("next_actions") or [{}])[0].get("tool")
                 == "get_cvc_template_provision_status",
                 str(created_data.get("next_actions"))[:200])

    provision = payload_of(await session.call_tool(
        "get_cvc_template_provision_status", {"template_id": template_id}))
    provision_data = provision.get("data") or {}
    report.check(area, "准备状态 ready 且给出构建入口",
                 provision.get("ok") is True
                 and provision_data.get("instance_status") == "ready"
                 and provision_data.get("status_label") == "实例已就绪"
                 and (provision_data.get("next_actions") or [{}])[0].get("tool")
                 == "get_cvc_build_status",
                 str(provision_data)[:240])

    bad_id = payload_of(await session.call_tool(
        "get_cvc_template", {"template_id": "bad id/../x"}))
    report.check(area, "非法 template_id 调用前拦下",
                 bad_id.get("ok") is False
                 and bad_id.get("error_code") == "VALIDATION_ERROR", str(bad_id)[:200])


def scenario_attribution(mock: MockBackend, report: Report, key: str) -> None:
    area = "L0 归因透传"
    credits = mock.last("/api/v1/openapi/credits")
    if not report.check(area, "后端收到 /credits 调用", credits is not None):
        return
    headers = credits["headers"]
    report.check(area, "使用调用方 Key 而非部署 Key",
                 headers.get("x-api-key") == key, headers.get("x-api-key", "")[:12])
    report.check(area, "X-Casee-Client=workbuddy",
                 headers.get("x-casee-client") == CLIENT_NAME, headers.get("x-casee-client", ""))
    report.check(area, "X-Casee-Tool=casee_get_credits",
                 headers.get("x-casee-tool") == "casee_get_credits",
                 headers.get("x-casee-tool", ""))
    report.check(area, "X-Casee-Session 已透传", bool(headers.get("x-casee-session")),
                 headers.get("x-casee-session", ""))
    report.check(area, "归因头不走 X-Casee-Tool 伪造",
                 all(c["headers"].get("x-casee-tool") != "spoofed" for c in mock.calls))


# ---------------------------------------------------------------------------
# 服务端生命周期
# ---------------------------------------------------------------------------

def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def boot_server(backend: str, port: int, log_path: Path, profile: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "MCP_TRANSPORT": "streamable-http",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_JSON_RESPONSE": "true",
        "MCP_STARTUP_CHECK_STRICT": "false",
        # 显式关闭静态白名单，让双平面鉴权探测真实打到后端桩
        "MCP_REQUIRE_API_KEY": "true",
        "MCP_ALLOWED_API_KEYS": "",
        "MCP_TOOL_PROFILE": profile,
        "CASEE_IS_API_KEY": SIM_API_KEY,
        "CASEE_API_BASE_URL": backend,
        "CASEE_OPENAPI_BASE_URL": f"{backend}/api/v1/openapi",
        "PYTHONPATH": str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        "PYTHONUNBUFFERED": "1",
    })
    log_file = log_path.open("wb")
    return subprocess.Popen(
        [sys.executable, "-m", "casee_mcp_server"],
        cwd=str(PKG_ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )


async def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if _port_open(port):
            return True
        await asyncio.sleep(0.25)
    return False


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _client_headers(key: str, session_id: str) -> dict[str, str]:
    return {
        "X-API-Key": key,
        "X-Casee-Client": CLIENT_NAME,
        "X-Casee-Client-Version": CLIENT_VERSION,
        "X-Casee-Session": session_id,
    }


async def with_session(mcp_url: str, key: str, session_id: str, run) -> None:
    http_client = httpx2.AsyncClient(
        headers=_client_headers(key, session_id), timeout=30)
    try:
        async with streamable_http_client(mcp_url, http_client=http_client) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                await run(session)
    finally:
        await http_client.aclose()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _load_registry() -> tuple[frozenset[str], list[str]]:
    """导入注册表，拿到核心集合与全量工具名（与服务端同源，避免硬编码漂移）。"""
    sys.path.insert(0, str(SRC_ROOT))
    from casee_mcp_server.server import CORE_TOOL_NAMES, all_tool_names

    return CORE_TOOL_NAMES, all_tool_names()


async def run(args: argparse.Namespace) -> int:
    report = Report()
    core_names, all_names = _load_registry()
    report.note(f"注册表：core={len(core_names)} full={len(all_names)}")

    mock = MockBackend()
    backend = args.backend or mock.start()
    own_backend = not args.backend
    if own_backend:
        report.note(f"mock 后端已启动：{backend}")

    log_dir = Path(tempfile.gettempdir())
    session_id = args.session or f"wbux-{int(time.time())}"
    core_proc: subprocess.Popen | None = None
    full_proc: subprocess.Popen | None = None
    core_log = log_dir / f"casee-wbux-core-{session_id}.log"
    full_log = log_dir / f"casee-wbux-full-{session_id}.log"
    try:
        core_port = args.port or _free_port()
        core_proc = boot_server(backend, core_port, core_log, "core")
        if not await wait_ready(core_port, core_proc):
            print(f"ERROR: core MCP server 启动失败，日志见 {core_log}", file=sys.stderr)
            return 2
        core_url = f"http://127.0.0.1:{core_port}/mcp"
        report.note(f"core MCP server：{core_url}")

        await scenario_auth(report, mock, core_url, SIM_API_KEY)

        async def core_run(session: ClientSession) -> None:
            await scenario_profile_core(session, report, core_names, all_names)
            await scenario_guide(session, report)
            await scenario_plan(session, report)
            await scenario_credits(session, report)
            await scenario_overview(session, report)
            await scenario_confirm_token(session, report)
            await scenario_errors(session, report)
            await scenario_creation_modes(session, report, mock)

        await with_session(core_url, SIM_API_KEY, session_id, core_run)
        scenario_attribution(mock, report, SIM_API_KEY)
        _stop(core_proc)
        core_proc = None

        full_port = args.port or _free_port()
        full_proc = boot_server(backend, full_port, full_log, "full")
        if not await wait_ready(full_port, full_proc):
            print(f"ERROR: full MCP server 启动失败，日志见 {full_log}", file=sys.stderr)
            return 2
        full_url = f"http://127.0.0.1:{full_port}/mcp"

        async def full_run(session: ClientSession) -> None:
            await scenario_profile_full(session, report, all_names)

        await with_session(full_url, SIM_API_KEY, session_id, full_run)
    finally:
        _stop(core_proc)
        _stop(full_proc)
        if own_backend:
            mock.stop()

    failed = report.failed
    print()
    print(f"RESULT: {len(report.rows) - len(failed)}/{len(report.rows)} checks passed")
    if failed:
        print("FAILED CHECKS:")
        for row in failed:
            print(f"  - {row['area']} :: {row['name']} — {row['detail']}")
    if args.keep_logs:
        print(f"core log: {core_log}")
        print(f"full log: {full_log}")
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if not failed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="",
                        help="复用已存在的 CaSee 后端（默认启动进程内 mock）")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--session", default="")
    parser.add_argument("--keep-logs", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
