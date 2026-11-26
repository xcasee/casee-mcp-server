#!/usr/bin/env python3
"""WorkBuddy-style MCP client simulation for the CaSee MCP server.

Why this exists
---------------
The UX optimizations (L0 attribution, L1 entry guidance, L2 decision hints,
L3 promise reduction) are only observable from the *client* side: an agent
platform such as WorkBuddy connects over streamable HTTP, reads the initialize
``instructions``, lists tools, then calls them one by one. Unit tests mock the
SDK and therefore cannot catch contract regressions in that conversation.

This harness reproduces the platform behaviour end to end:

1. boots the real MCP server as a subprocess (streamable HTTP, real auth);
2. connects with the official MCP client (same protocol WorkBuddy uses);
3. plays a scripted WorkBuddy session and asserts the *agent-facing* contract
   (instructions / tool metadata / plan options / recommendations / errors);
4. verifies the backend audit attribution through
   ``GET /api/v1/openapi/admin/usage-summary``;
5. checks the dynamic OpenAPI spec exposes the new admin endpoint.

Usage
-----
    export CASEE_IS_API_KEY=casee_xxx          # tenant key valid on both planes
    python tests/simulation/workbuddy_sim.py \
        --backend http://localhost:8009 \
        --upstream http://121.43.113.132:8000

Options:
    --mcp-url URL     reuse an already running MCP server instead of booting one
    --session ID      fixed X-Casee-Session value (default: sim-<uuid8>)
    --keep-logs       keep the MCP server log file and print its path
    --json            print the machine-readable result summary

Exit code is 0 only when every check passes.
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
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # SDK (installed in the deployment virtualenv)
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
except ImportError as exc:  # pragma: no cover - environment guard
    print(
        "This harness must run inside the MCP server virtualenv "
        "(mcp + httpx2 required): " + str(exc),
        file=sys.stderr,
    )
    raise SystemExit(2)

PKG_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PKG_ROOT / "src"

CLIENT_NAME = "workbuddy"
CLIENT_VERSION = "2.0.0"

VALID_TIERS = {"T0", "T1", "T2", "T3"}


def registry_snapshot(profile: str) -> Tuple[str, List[str], List[str]]:
    """读取服务端注册表，避免把版本号与工具数量写死在仿真里。

    ``MCP_TOOL_PROFILE`` 会影响 ``tools/list`` 的输出，因此期望值必须与
    当前 profile 同源计算，否则分层调整后仿真会误报。
    """
    sys.path.insert(0, str(SRC_ROOT))
    from casee_mcp_server import server as server_mod  # noqa: PLC0415 - 延迟导入
    from casee_mcp_server.version import __version__  # noqa: PLC0415 - 延迟导入

    return __version__, server_mod.visible_tool_names(profile), server_mod.all_tool_names()


# ---------------------------------------------------------------------------
# result collection
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

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
    def failed(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if not r["ok"]]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "total": len(self.rows),
            "passed": len(self.rows) - len(self.failed),
            "failed": len(self.failed),
            "checks": self.rows,
        }


def payload_of(result: Any) -> Dict[str, Any]:
    """Extract the tool's dict payload from an MCP CallToolResult."""
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


def option_shape_ok(opt: Any) -> Tuple[bool, str]:
    if not isinstance(opt, dict):
        return False, f"option is {type(opt).__name__}"
    required = {
        "option_id": str, "title": str, "why": str, "steps": list,
        "cost_credits": (int, float), "eta_seconds": int,
        "requires": list, "risk": str,
    }
    for key, kinds in required.items():
        if key not in opt:
            return False, f"missing {key}"
        if not isinstance(opt[key], kinds):
            return False, f"{key} has type {type(opt[key]).__name__}"
    if not opt["steps"] or not isinstance(opt["steps"][0].get("tool"), str):
        return False, "steps[0].tool missing"
    return True, ""


def rec_ids(payload: Dict[str, Any]) -> List[str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    recs = (data or {}).get("recommendations") or payload.get("recommendations") or []
    return [str(r.get("id")) for r in recs if isinstance(r, dict)]


def recommendations_of(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    recs = (data or {}).get("recommendations")
    if recs is None:
        recs = payload.get("recommendations")
    return [r for r in (recs or []) if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

async def scenario_protocol(session: ClientSession, report: Report,
                            expected_version: str, expected_tools: List[str],
                            all_tools: List[str], profile: str) -> None:
    area = "L1 protocol"

    instructions = (session.instructions or "").strip()
    report.check(area, "initialize carries instructions", len(instructions) > 200,
                 f"instructions len={len(instructions)}")
    for token in ("casee_guide", "casee_plan_task", "recommendations"):
        report.check(area, f"instructions mention {token}", token in instructions)

    server_info = session.server_info
    report.check(area, "server name", getattr(server_info, "name", "") == "casee",
                 str(server_info))
    report.check(area, f"server version {expected_version}",
                 getattr(server_info, "version", "") == expected_version,
                 str(getattr(server_info, "version", "")))

    tools = (await session.list_tools()).tools
    names = {t.name for t in tools}
    report.check(area, f"profile={profile} 暴露 {len(expected_tools)} 个工具",
                 names == set(expected_tools),
                 f"count={len(names)} missing={sorted(set(expected_tools) - names)[:5]} "
                 f"extra={sorted(names - set(expected_tools))[:5]}")
    report.check(area, "隐藏工具不在 tools/list",
                 not (names & (set(all_tools) - set(expected_tools))),
                 str(sorted(names & (set(all_tools) - set(expected_tools)))[:5]))
    empty_desc = [t.name for t in tools if not (t.description or "").strip()]
    report.check(area, "every tool has a description", not empty_desc, str(empty_desc[:5]))

    by_name = {t.name: t for t in tools}
    missing_tier: List[str] = []
    missing_annotations: List[str] = []
    missing_use_when: List[str] = []
    for name in expected_tools:
        tool = by_name.get(name)
        if tool is None:
            report.check(area, f"tool registered: {name}", False, "not listed")
            continue
        meta = tool.meta or {}
        if str(meta.get("tier", "")) not in VALID_TIERS:
            missing_tier.append(name)
        if not tool.annotations:
            missing_annotations.append(name)
        if "USE WHEN" not in (tool.description or ""):
            missing_use_when.append(name)
    report.check(area, "visible tools declare _meta.tier", not missing_tier, str(missing_tier))
    report.check(area, "visible tools declare annotations", not missing_annotations,
                 str(missing_annotations))
    report.check(area, "visible tool descriptions use USE WHEN", not missing_use_when,
                 str(missing_use_when))


async def scenario_guide(session: ClientSession, report: Report) -> None:
    area = "L1 casee_guide"
    res = payload_of(await session.call_tool("casee_guide", {}))
    data = res.get("data") or {}
    report.check(area, "ok", res.get("ok") is True, str(res)[:200])
    tiers = {t.get("tier") for t in data.get("capability_tiers") or []}
    report.check(area, "four capability tiers", tiers == {"T0", "T1", "T2", "T3"}, str(tiers))
    report.check(area, "decision tree present", len(data.get("decision_tree") or []) >= 5,
                 f"len={len(data.get('decision_tree') or [])}")
    report.check(area, "naming synonyms present", bool(data.get("naming")))
    report.check(area, "hard rules present", len(data.get("hard_rules") or []) >= 3)
    report.check(area, "first-time checklist present",
                 len(data.get("first_time_checklist") or []) >= 3)

    themed = payload_of(await session.call_tool("casee_guide", {"topic": "insight"}))
    themed_data = themed.get("data") or {}
    report.check(area, "topic=insight trims decision tree",
                 0 < len(themed_data.get("decision_tree") or []) < len(data.get("decision_tree") or []),
                 f"len={len(themed_data.get('decision_tree') or [])}")


PLAN_CASES: Tuple[Tuple[str, Dict[str, Any], str], ...] = (
    ("最近宁德时代出海有什么新闻", {}, "one_off_search"),
    ("帮我持续跟踪比亚迪在欧洲的动向，每周看一次", {}, "monitor"),
    ("帮我分析一下这家公司的风险和差距", {}, "company_deep_dive"),
    ("生成一份竞品分析报告", {}, "report"),
    ("我想了解新能源行业的市场趋势", {}, "market_analysis"),
    ("帮我盯住宁德时代的动态", {}, "monitor"),
    ("我们自己的客户最近在想什么", {}, "customer_analysis"),
)


async def scenario_plan(session: ClientSession, report: Report) -> None:
    area = "L2 casee_plan_task"
    for goal, extra, expected_intent in PLAN_CASES:
        res = payload_of(await session.call_tool("casee_plan_task", {"goal": goal, **extra}))
        ok = res.get("ok") is True
        if not report.check(area, f"ok: {goal}", ok, str(res)[:200]):
            continue
        data = res.get("data") or {}
        options = data.get("options") or []
        report.check(area, f"1-3 options: {goal}", 1 <= len(options) <= 3,
                     f"len={len(options)}")
        shape_errors = [msg for msg in (option_shape_ok(o)[1] for o in options) if msg]
        report.check(area, f"option contract: {goal}", not shape_errors, str(shape_errors[:2]))
        report.check(area, f"has a 0-credit option: {goal}",
                     any(float(o.get("cost_credits") or 0) == 0 for o in options),
                     str([o.get("cost_credits") for o in options]))
        report.check(area, f"ask_user present: {goal}", bool(data.get("ask_user")))
        report.check(area, f"intent={expected_intent}: {goal}",
                     data.get("intent") == expected_intent,
                     f"got {data.get('intent')}")

    budget = payload_of(await session.call_tool(
        "casee_plan_task", {"goal": "生成一份竞品分析报告", "budget_credits": 0.5}))
    budget_opts = ((budget.get("data") or {}).get("options")) or []
    report.check(area, "budget filter removes >0.5 options",
                 all(float(o.get("cost_credits") or 0) <= 0.5 for o in budget_opts),
                 str([o.get("cost_credits") for o in budget_opts]))

    impatient = payload_of(await session.call_tool(
        "casee_plan_task", {"goal": "持续跟踪固态电池进展，每周汇报", "max_wait_seconds": 30}))
    impatient_opts = ((impatient.get("data") or {}).get("options")) or []
    report.check(area, "patience filter hides the 10-min build",
                 all(int(o.get("eta_seconds") or 0) <= 30 for o in impatient_opts),
                 str([o.get("eta_seconds") for o in impatient_opts]))

    empty = payload_of(await session.call_tool("casee_plan_task", {"goal": "  "}))
    report.check(area, "empty goal is a structured error",
                 empty.get("ok") is False and empty.get("error_code") == "VALIDATION_ERROR",
                 str(empty)[:200])


async def scenario_overview(session: ClientSession, report: Report) -> None:
    area = "L2/L3 overview"
    res = payload_of(await session.call_tool("casee_home_overview", {"limit": 3}))
    data = res.get("data") or {}
    report.check(area, "ok", res.get("ok") is True, str(res)[:200])
    report.check(area, "onboarding hints present", len(data.get("onboarding") or []) >= 1,
                 str(data.get("onboarding"))[:200])
    report.check(area, "pending_jobs key present (may be empty)",
                 isinstance(data.get("pending_jobs"), list),
                 str(type(data.get("pending_jobs"))))
    recs = recommendations_of(res)
    report.check(area, "recommendations at most 3", len(recs) <= 3, f"len={len(recs)}")


async def scenario_credits(session: ClientSession, report: Report) -> None:
    area = "L1/L2 credits"
    res = payload_of(await session.call_tool("casee_get_credits", {}))
    data = res.get("data") or {}
    report.check(area, "ok", res.get("ok") is True, str(res)[:200])
    report.check(area, "first_cvc_free hint", bool(data.get("first_cvc_free")))
    report.check(area, "free_capabilities list", len(data.get("free_capabilities") or []) >= 5)
    report.check(area, "recommendations at most 3", len(recommendations_of(res)) <= 3)


async def scenario_search(session: ClientSession, report: Report) -> None:
    area = "L2 retrieval"
    query = "宁德时代 海外 建厂"
    recs_seen: List[Dict[str, Any]] = []
    for attempt in range(1, 4):
        res = payload_of(await session.call_tool(
            "search_intelligence", {"q": query, "days": 90, "limit": 5}))
        if res.get("error") or res.get("error_code"):
            report.note(f"search_intelligence upstream error on attempt {attempt}: "
                        f"{res.get('error_code') or res.get('error')}")
            report.check(area, "upstream failure is structured", bool(res.get("error_code")),
                         str(res)[:200])
            return
        ids = rec_ids(res)
        report.check(area, f"recommendations <=3 (attempt {attempt})", len(ids) <= 3, str(ids))
        report.check(area, f"recommendations unique (attempt {attempt})",
                     len(ids) == len(set(ids)), str(ids))
        recs_seen.extend(recommendations_of(res))

    ids_all = [str(r.get("id")) for r in recs_seen]
    report.check(area, "repeated query suggests building a CVC",
                 "repeat_query_build_cvc" in ids_all, str(ids_all))
    target = next((r for r in recs_seen if r.get("id") == "repeat_query_build_cvc"), None)
    if target:
        report.check(area, "repeat suggestion requires user confirmation",
                     target.get("requires_user_confirm") is True, str(target)[:200])
        report.check(area, "repeat suggestion carries tool+args",
                     bool(target.get("tool")) and isinstance(target.get("args"), dict))


async def scenario_semantic(session: ClientSession, report: Report,
                            cvc_id: str, company: str) -> None:
    area = "L1/L2 semantic"
    bad = payload_of(await session.call_tool(
        "semantic_search_tool", {"cvc_model_id": "bad id/../x", "q": "测试"}))
    report.check(area, "invalid cvc_id -> structured error",
                 bad.get("ok") is False and bad.get("error_code") == "VALIDATION_ERROR",
                 str(bad)[:240])
    report.check(area, "invalid cvc_id -> hint/action",
                 bool(bad.get("hint")) and bool(bad.get("action")), str(bad)[:240])

    if not cvc_id:
        report.note("no completed CVC available; semantic happy-path skipped")
        return
    res = payload_of(await session.call_tool(
        "semantic_search_tool", {"cvc_model_id": cvc_id, "q": company, "top_k": 5}))
    if res.get("error") or res.get("error_code"):
        report.check(area, "semantic upstream error is structured", bool(res.get("error_code")),
                     str(res)[:240])
        return
    report.check(area, "semantic search ok", res.get("ok") is True, str(res)[:200])
    ids = rec_ids(res)
    report.check(area, "semantic recommendations <=3", len(ids) <= 3, str(ids))
    report.check(area, "semantic recommends a paid deep dive",
                 "deep_dive_insight" in ids, str(ids))


async def scenario_insight(session: ClientSession, report: Report,
                           cvc_id: str, company: str) -> None:
    area = "L2/L3 insight dry_run"
    if not cvc_id:
        report.note("no completed CVC available; insight dry_run skipped")
        return
    res = payload_of(await session.call_tool(
        "casee_get_enterprise_insight",
        {"cvc_id": cvc_id, "company_name": company, "dry_run": True}))
    if res.get("error") or res.get("error_code"):
        report.check(area, "insight error is structured with alternatives",
                     bool(res.get("error_code")) and "alternatives" in res,
                     str(res)[:240])
        return
    data = res.get("data") or {}
    cost_note = data.get("cost_note") or {}
    report.check(area, "dry_run reports cost_note.dry_run", cost_note.get("dry_run") is True,
                 str(cost_note))
    report.check(area, "dry_run exposes confirm_prompt", bool(data.get("confirm_prompt")),
                 str(data)[:200])
    ids = rec_ids(res)
    report.check(area, "dry_run recommends confirm_insight", "confirm_insight" in ids, str(ids))


def scenario_error_contract(report: Report) -> None:
    """In-process contract check: known error codes always carry alternatives."""
    area = "L2 error contract"
    sys.path.insert(0, str(SRC_ROOT))
    from casee_mcp_server.server import _ALTERNATIVES, _report_invalid  # noqa: PLC0415

    for code in ("PRODUCT_ACCESS_DENIED", "CREDITS_INSUFFICIENT",
                 "VALIDATION_ERROR", "NOT_FOUND"):
        alts = _ALTERNATIVES.get(code)
        report.check(area, f"{code} has >=2 alternatives",
                     isinstance(alts, list) and len(alts) >= 2, str(alts))
        if isinstance(alts, list):
            bad = [a for a in alts if not a.get("tool")]
            report.check(area, f"{code} alternatives name a tool", not bad, str(bad))

    invalid = _report_invalid("sim", {"field": "x"})
    report.check(area, "validation error keeps retryable/action",
                 invalid.get("retryable") is False and bool(invalid.get("action")),
                 str(sorted(invalid.keys())))
    report.check(area, "validation error carries alternatives",
                 isinstance(invalid.get("alternatives"), list)
                 and len(invalid["alternatives"]) >= 2,
                 str(invalid.get("alternatives")))


def _pick_cvc(overview: Dict[str, Any]) -> Tuple[str, str]:
    data = overview.get("data") or {}
    cvc = data.get("cvc") or {}
    recent = cvc.get("recent") or []
    fallback: Tuple[str, str] = ("", "")
    for item in recent:
        if not isinstance(item, dict):
            continue
        cvc_id = str(item.get("cvc_id") or "")
        if not cvc_id:
            continue
        name = str(item.get("name") or item.get("cvc_name") or "")
        if str(item.get("status") or "") == "completed":
            return cvc_id, name
        fallback = fallback or (cvc_id, name)
    return fallback


async def _find_completed_cvc(session: ClientSession) -> Tuple[str, str]:
    res = payload_of(await session.call_tool(
        "list_cvc_models_tool", {"status": "completed", "page_size": 5}))
    data = res.get("data") or {}
    items = data.get("items") or data.get("models") or []
    for item in items:
        cvc_id = str(item.get("cvc_id") or "")
        if cvc_id:
            return cvc_id, str(item.get("name") or "")
    return "", ""


async def _pick_company(session: ClientSession, cvc_id: str) -> str:
    if not cvc_id:
        return ""
    res = payload_of(await session.call_tool(
        "casee_list_cvc_companies", {"cvc_id": cvc_id}))
    data = res.get("data") or {}
    items = data.get("items") or data.get("companies") or []
    for item in items:
        name = str(item.get("company_name") or item.get("name") or "")
        if name:
            return name
    return ""


# ---------------------------------------------------------------------------
# backend side checks
# ---------------------------------------------------------------------------

async def backend_usage_summary(backend: str, api_key: str, session_id: str,
                                report: Report) -> None:
    area = "L0 attribution"
    headers = {
        "X-API-Key": api_key,
        "X-Casee-Client": CLIENT_NAME,
        "X-Casee-Client-Version": CLIENT_VERSION,
        "X-Casee-Session": session_id,
    }
    async with httpx2.AsyncClient(timeout=ctx_timeout()) as client:
        resp = await client.get(
            f"{backend}/api/v1/openapi/admin/usage-summary",
            params={"range": "1d"}, headers=headers)
        report.check(area, "usage-summary reachable", resp.status_code == 200,
                     f"status={resp.status_code} body={resp.text[:200]}")
        if resp.status_code != 200:
            return
        data = (resp.json() or {}).get("data") or {}
        totals = data.get("totals") or {}
        report.check(area, "usage-summary reports MCP calls",
                     int(totals.get("mcp_calls") or 0) > 0, str(totals))
        clients = {c.get("client_name") for c in data.get("clients") or []}
        report.check(area, "client attribution recorded", CLIENT_NAME in clients, str(clients))
        tools = {t.get("tool") for t in data.get("tools") or []}
        report.check(area, "tool attribution recorded for casee_get_credits",
                     "casee_get_credits" in tools, str(sorted(x for x in tools if x)[:10]))
        report.check(area, "funnel buckets present",
                     bool(data.get("funnel")), str(data.get("funnel"))[:200])
        report.check(area, "sessions counted", int(totals.get("sessions") or 0) >= 1,
                     str(totals.get("sessions")))

        spec = await client.get(f"{backend}/api/v1/openapi/spec")
        report.check(area, "dynamic spec reachable", spec.status_code == 200,
                     f"status={spec.status_code}")
        if spec.status_code == 200:
            paths = (spec.json() or {}).get("paths") or {}
            target = [p for p in paths if p.endswith("/openapi/admin/usage-summary")]
            report.check(area, "dynamic spec lists usage-summary", bool(target), str(target))


def ctx_timeout() -> float:
    try:
        return float(os.environ.get("CASEE_TIMEOUT", "60"))
    except ValueError:
        return 60.0


# ---------------------------------------------------------------------------
# server lifecycle
# ---------------------------------------------------------------------------

def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def boot_server(api_key: str, backend: str, upstream: str, port: int,
                log_path: Path, profile: str = "core") -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "MCP_TRANSPORT": "streamable-http",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_JSON_RESPONSE": "true",
        "MCP_STARTUP_CHECK_STRICT": "false",
        "MCP_TOOL_PROFILE": profile,
        "CASEE_IS_API_KEY": api_key,
        "CASEE_API_BASE_URL": upstream,
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


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    report = Report()
    api_key = (args.api_key or os.environ.get("CASEE_IS_API_KEY", "")).strip()
    if not api_key:
        print("ERROR: provide --api-key or export CASEE_IS_API_KEY", file=sys.stderr)
        return 2

    session_id = args.session or f"sim-{uuid.uuid4().hex[:8]}"
    report.note(f"api_key={api_key[:14]}… session={session_id} backend={args.backend}")

    profile = (os.environ.get("MCP_TOOL_PROFILE") or "core").strip().lower()
    expected_version, expected_tools, all_tools = registry_snapshot(profile)
    report.note(f"profile={profile} visible={len(expected_tools)} total={len(all_tools)}")

    proc: Optional[subprocess.Popen] = None
    log_path = Path(tempfile.gettempdir()) / f"casee-mcp-sim-{session_id}.log"
    if args.mcp_url:
        mcp_url = args.mcp_url
    else:
        port = args.port or _free_port()
        proc = boot_server(api_key, args.backend, args.upstream, port, log_path, profile)
        if not await wait_ready(port, proc):
            print(f"ERROR: MCP server failed to start; see {log_path}", file=sys.stderr)
            proc.terminate()
            return 2
        mcp_url = f"http://127.0.0.1:{port}/mcp"
        report.note(f"mcp server booted at {mcp_url}")

    headers = {
        "X-API-Key": api_key,
        "X-Casee-Client": CLIENT_NAME,
        "X-Casee-Client-Version": CLIENT_VERSION,
        "X-Casee-Session": session_id,
    }
    http_client = httpx2.AsyncClient(headers=headers, timeout=ctx_timeout())
    try:
        async with streamable_http_client(mcp_url, http_client=http_client) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                report.note("session initialized")

                await scenario_protocol(session, report, expected_version,
                                        expected_tools, all_tools, profile)
                await scenario_guide(session, report)
                await scenario_plan(session, report)
                await scenario_credits(session, report)
                await scenario_overview(session, report)

                overview = payload_of(await session.call_tool("casee_home_overview", {"limit": 5}))
                cvc_id, cvc_name = _pick_cvc(overview)
                if not cvc_id:
                    cvc_id, cvc_name = await _find_completed_cvc(session)
                company = await _pick_company(session, cvc_id)
                report.note(f"using cvc_id={cvc_id or '-'} name={cvc_name or '-'} "
                            f"company={company or '-'}")

                await scenario_search(session, report)
                await scenario_semantic(session, report, cvc_id, company or cvc_name or "测试")
                await scenario_insight(session, report, cvc_id, company or cvc_name or "测试")
    finally:
        await http_client.aclose()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    scenario_error_contract(report)
    await backend_usage_summary(args.backend, api_key, session_id, report)

    failed = report.failed
    print()
    print(f"RESULT: {len(report.rows) - len(failed)}/{len(report.rows)} checks passed")
    if failed:
        print("FAILED CHECKS:")
        for row in failed:
            print(f"  - {row['area']} :: {row['name']} — {row['detail']}")
    print(f"session_id={session_id}")
    if args.keep_logs and proc is not None:
        print(f"mcp log: {log_path}")
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if not failed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--backend", default=os.environ.get("SIM_BACKEND", "http://localhost:8009"))
    parser.add_argument("--upstream", default=os.environ.get("SIM_UPSTREAM", "http://121.43.113.132:8000"))
    parser.add_argument("--mcp-url", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--session", default="")
    parser.add_argument("--keep-logs", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
