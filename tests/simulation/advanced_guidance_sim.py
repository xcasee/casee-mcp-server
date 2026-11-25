#!/usr/bin/env python3
"""WorkBuddy advanced-feature interaction-guidance simulation (CaSee MCP v1.12).

Why this exists
---------------
``workbuddy_sim.py`` covers the baseline agent-facing contract (instructions,
tool metadata, plan options, recommendations, error contract). This harness
focuses on the *advanced capabilities* shipped with the lite build profile and
the public Demo sample, and asserts the **guidance** a WorkBuddy agent would
receive at each decision point:

1. entry guidance      — casee_guide exposes lite/full tiers + zero-cost Demo;
2. tier selection      — casee_plan_task recommends build_profile=lite with
                        1.0 credit / 300s, keeps a 0-credit option, and honours
                        budget / patience constraints;
3. zero-cost Demo      — dashboard / model / companies / build-status are
                        readable, insight dry_run is free, and write attempts
                        are rejected with a "create your own analysis" path;
4. own-CVC advanced    — insight dry_run → confirm prompt, rebuild dry-run
                        confirms profile passthrough, build-status progress;
5. fallback guidance   — VALIDATION_ERROR / NOT_FOUND / PRODUCT_ACCESS_DENIED
                        all carry executable alternatives.

Every check is a scripted "user turn → tool call → asserted guidance". The
harness boots the real MCP server over streamable HTTP with the official MCP
client, so it exercises exactly the protocol WorkBuddy uses.

Usage
-----
    export CASEE_IS_API_KEY=casee_xxx
    python tests/simulation/advanced_guidance_sim.py \
        --backend http://localhost:8009 \
        --upstream http://121.43.113.132:8000

Exit code 0 only when every check passes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workbuddy_sim import (  # noqa: E402 - sibling harness shared helpers
    CLIENT_NAME,
    CLIENT_VERSION,
    Report,
    _find_completed_cvc,
    _pick_cvc,
    _pick_company,
    boot_server,
    ctx_timeout,
    payload_of,
    wait_ready,
)

try:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
except ImportError as exc:  # pragma: no cover - environment guard
    print("Run inside the MCP venv (mcp + httpx2 required): " + str(exc), file=sys.stderr)
    raise SystemExit(2)

DEMO_CVC = "cvc_demoevbat01"
MISSING_CVC = "cvc_00000000000000000000000000000000"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _data(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _recs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = _data(payload)
    recs = data.get("recommendations")
    if recs is None:
        recs = payload.get("recommendations")
    return [r for r in (recs or []) if isinstance(r, dict)]


def _option_ids(options: List[Dict[str, Any]]) -> List[str]:
    return [str(o.get("option_id")) for o in options]


def _find_step(option: Dict[str, Any], tool: str) -> Dict[str, Any]:
    for step in option.get("steps") or []:
        if isinstance(step, dict) and step.get("tool") == tool:
            return step
    return {}


def _alternatives_ok(payload: Dict[str, Any]) -> Tuple[bool, str]:
    alts = payload.get("alternatives")
    if not isinstance(alts, list) or len(alts) < 2:
        return False, f"alternatives={alts!r}"
    bad = [a for a in alts if not isinstance(a, dict) or not a.get("tool")]
    if bad:
        return False, f"alternatives without tool: {bad[:2]}"
    return True, ""


def _error_contract_ok(payload: Dict[str, Any], report: Report, area: str,
                       prefix: str) -> bool:
    """Assert a structured error carries code + hint + action + alternatives."""
    ok = True
    ok &= report.check(area, f"{prefix}: ok=false", payload.get("ok") is False,
                       str(payload)[:200])
    ok &= report.check(area, f"{prefix}: error_code present",
                       bool(payload.get("error_code")), str(payload.get("error_code")))
    ok &= report.check(area, f"{prefix}: hint+action present",
                       bool(payload.get("hint")) and bool(payload.get("action")),
                       f"hint={payload.get('hint')!r} action={payload.get('action')!r}")
    alts_ok, detail = _alternatives_ok(payload)
    ok &= report.check(area, f"{prefix}: executable alternatives", alts_ok, detail)
    return bool(ok)


# ---------------------------------------------------------------------------
# S1 — entry guidance & tier selection
# ---------------------------------------------------------------------------

async def scenario_entry_guidance(session: ClientSession, report: Report) -> None:
    area = "S1 入口引导/档位认知"

    instructions = (session.instructions or "")
    report.check(area, "instructions state lite 1.0 credit",
                 "lite" in instructions and "1.0 credit" in instructions,
                 instructions[:0])
    report.check(area, "instructions state full 2.5 credits",
                 "2.5 credits" in instructions)
    report.check(area, "instructions require dry_run first",
                 "dry_run=true" in instructions)
    report.check(area, "instructions forbid blocking CVC waits",
                 "不要阻塞" in instructions)
    report.check(area, "instructions expose the zero-cost Demo sample",
                 DEMO_CVC in instructions and "0 credit" in instructions,
                 instructions[max(0, instructions.find(DEMO_CVC) - 40):][:120])

    guide = payload_of(await session.call_tool("casee_guide", {"topic": "cvc"}))
    data = _data(guide)
    report.check(area, "guide(cvc) ok", guide.get("ok") is True, str(guide)[:160])
    paid = ((data.get("free_vs_paid") or {}).get("paid") or {})
    build_text = str(paid.get("create_cvc_model") or "")
    report.check(area, "paid table exposes lite 1.0 / full 2.5",
                 "lite" in build_text and "1.0" in build_text and "2.5" in build_text,
                 build_text)
    report.check(area, "paid table keeps first-CVC-free promise",
                 "首条免费" in build_text, build_text)
    tree = data.get("decision_tree") or []
    demo_rows = [r for r in tree if DEMO_CVC in json.dumps(r, ensure_ascii=False)]
    report.check(area, "decision tree exposes zero-cost Demo sample", bool(demo_rows),
                 json.dumps(tree, ensure_ascii=False)[:300])
    checklist = " ".join(map(str, data.get("first_time_checklist") or []))
    report.check(area, "checklist tells agent to default to lite",
                 "lite" in checklist, checklist[:160])

    plan = payload_of(await session.call_tool(
        "casee_plan_task", {"goal": "帮我持续跟踪比亚迪在欧洲的动向，每周看一次"}))
    plan_data = _data(plan)
    options = plan_data.get("options") or []
    report.check(area, "monitor goal -> 1-3 options", 1 <= len(options) <= 3,
                 f"len={len(options)}")
    report.check(area, "monitor goal -> intent=monitor",
                 plan_data.get("intent") == "monitor", str(plan_data.get("intent")))
    build_opt = next((o for o in options if _find_step(o, "create_cvc_model")), None)
    if build_opt is None:
        report.check(area, "monitor goal offers a CVC build option", False,
                     str(_option_ids(options)))
    else:
        step = _find_step(build_opt, "create_cvc_model")
        args = step.get("args") or {}
        report.check(area, "build option defaults to build_profile=lite",
                     args.get("build_profile") == "lite", str(args))
        report.check(area, "build option costs 1.0 credit",
                     float(build_opt.get("cost_credits") or 0) == 1.0,
                     str(build_opt.get("cost_credits")))
        report.check(area, "build option ETA is 300s (3-5 min)",
                     int(build_opt.get("eta_seconds") or 0) == 300,
                     str(build_opt.get("eta_seconds")))
        requires = " ".join(map(str, build_opt.get("requires") or []))
        report.check(area, "build option explains first-CVC-free + full upgrade",
                     "首条" in requires and "2.5" in requires, requires)
    report.check(area, "monitor goal keeps a 0-credit option",
                 any(float(o.get("cost_credits") or 0) == 0 for o in options),
                 str([o.get("cost_credits") for o in options]))
    report.check(area, "plan returns an ask_user sentence",
                 bool(str(plan_data.get("ask_user") or "").strip()),
                 str(plan_data.get("ask_user"))[:160])
    report.check(area, "default_option is one of the options",
                 plan_data.get("default_option") in _option_ids(options),
                 f"default={plan_data.get('default_option')} ids={_option_ids(options)}")

    no_cvc = payload_of(await session.call_tool(
        "casee_plan_task", {"goal": "帮我盯住宁德时代的动态"}))
    demo_opt = next(
        (o for o in (_data(no_cvc).get("options") or [])
         if _find_step(o, "casee_get_cvc_dashboard")), None)
    report.check(area, "no cvc_id -> zero-cost Demo option offered", demo_opt is not None,
                 str(_option_ids(_data(no_cvc).get("options") or [])))
    if demo_opt:
        args = (_find_step(demo_opt, "casee_get_cvc_dashboard").get("args") or {})
        report.check(area, "Demo option points at the public sample",
                     args.get("cvc_id") == DEMO_CVC, str(args))
        report.check(area, "Demo option is 0 credit / seconds",
                     float(demo_opt.get("cost_credits") or 0) == 0
                     and int(demo_opt.get("eta_seconds") or 0) <= 10,
                     str(demo_opt.get("cost_credits")) + "/"
                     + str(demo_opt.get("eta_seconds")))

    budget = payload_of(await session.call_tool(
        "casee_plan_task", {"goal": "持续跟踪固态电池进展", "budget_credits": 0.5}))
    budget_opts = _data(budget).get("options") or []
    report.check(area, "budget=0.5 hides the 1.0-credit build",
                 all(float(o.get("cost_credits") or 0) <= 0.5 for o in budget_opts)
                 and not any(_find_step(o, "create_cvc_model") for o in budget_opts),
                 str([(o.get("option_id"), o.get("cost_credits")) for o in budget_opts]))

    impatient = payload_of(await session.call_tool(
        "casee_plan_task", {"goal": "持续跟踪固态电池进展", "max_wait_seconds": 30}))
    impatient_opts = _data(impatient).get("options") or []
    report.check(area, "max_wait=30s hides the 300s build",
                 all(int(o.get("eta_seconds") or 0) <= 30 for o in impatient_opts),
                 str([(o.get("option_id"), o.get("eta_seconds")) for o in impatient_opts]))

    rep = payload_of(await session.call_tool(
        "casee_plan_task", {"goal": "生成一份竞品分析报告"}))
    rep_opts = _data(rep).get("options") or []
    free_first = next((o for o in rep_opts if _find_step(o, "plan_report")), None)
    paid = next((o for o in rep_opts if _find_step(o, "generate_report")), None)
    report.check(area, "report plan: free pre-flight offered first",
                 free_first is not None
                 and float(free_first.get("cost_credits") or 0) == 0,
                 str(_option_ids(rep_opts)))
    report.check(area, "report plan: paid generation is marked async+paid",
                 paid is not None
                 and float(paid.get("cost_credits") or 0) == 1.0
                 and paid.get("risk") == "paid_async",
                 str(paid)[:200])


# ---------------------------------------------------------------------------
# S2 — public Demo zero-cost journey
# ---------------------------------------------------------------------------

async def scenario_public_demo(session: ClientSession, report: Report) -> Tuple[str, str]:
    area = "S2 公共 Demo 零成本体验"
    company = ""

    dash = payload_of(await session.call_tool(
        "casee_get_cvc_dashboard", {"cvc_id": DEMO_CVC}))
    report.check(area, "dashboard readable (0 credit)", dash.get("ok") is True,
                 str(dash)[:240])
    report.check(area, "dashboard returns business data", bool(_data(dash)),
                 str(list(_data(dash).keys())))

    model = payload_of(await session.call_tool(
        "casee_get_cvc_model", {"cvc_id": DEMO_CVC, "include": "companies"}))
    model_data = _data(model)
    model_info = model_data.get("model") or {}
    report.check(area, "model metadata readable", model.get("ok") is True,
                 str(model)[:240])
    report.check(area, "model is public visibility",
                 str(model_info.get("visibility") or "").lower() == "public",
                 str(model_info.get("visibility")))
    report.check(area, "model is flagged read-only for agents",
                 model_info.get("is_read_only") is True, str(model_info.get("is_read_only")))
    report.check(area, "model asset quality is ok (not degraded)",
                 str(model_info.get("quality_status") or "") == "ok",
                 str(model_info.get("quality_status")))
    aq = model_info.get("asset_quality") or {}
    report.check(area, "model asset quality is curated with real entities",
                 str(aq.get("status") or "") == "ok"
                 and str(aq.get("evaluation_mode") or "") == "curated_demo"
                 and int((aq.get("counts") or {}).get("fact_entities") or 0) > 0,
                 json.dumps(aq, ensure_ascii=False)[:240])

    companies = payload_of(await session.call_tool(
        "casee_list_cvc_companies", {"cvc_id": DEMO_CVC}))
    items = _data(companies).get("companies") or []
    report.check(area, "companies readable", companies.get("ok") is True,
                 str(companies)[:240])
    report.check(area, "companies non-empty", len(items) >= 1, f"len={len(items)}")
    if items:
        company = str(items[0].get("company_name") or items[0].get("name") or "")
    report.check(area, "insight can use a real company name", bool(company), company)

    status = payload_of(await session.call_tool(
        "get_cvc_build_status", {"cvc_id": DEMO_CVC, "include_logs": False}))
    status_data = _data(status)
    report.check(area, "build-status readable", status.get("ok") is True,
                 str(status)[:240])
    report.check(area, "Demo is reported as completed",
                 str(status_data.get("status") or "").lower() == "completed",
                 str(status_data.get("status")))
    report.check(area, "build-status carries a human status_label",
                 bool(str(status_data.get("status_label") or "").strip()),
                 str(status_data.get("status_label")))
    report.check(area, "build-status carries phase plan for progress UX",
                 isinstance(status_data.get("phases"), list)
                 and len(status_data.get("phases") or []) >= 1,
                 str(status_data.get("phases"))[:200])
    report.check(area, "build-status flags read-only sample",
                 status_data.get("is_read_only") is True,
                 str(status_data.get("is_read_only")))
    report.check(area, "build-status exposes public visibility",
                 str(status_data.get("visibility") or "").lower() == "public",
                 str(status_data.get("visibility")))
    report.check(area, "build-status exposes the data owner tenant",
                 str(status_data.get("owner_tenant_id") or "") == "demo_public",
                 str(status_data.get("owner_tenant_id")))
    report.check(area, "build-status keeps requester and owner tenants distinct",
                 str(status_data.get("tenant_id") or "") != "demo_public",
                 str(status_data.get("tenant_id")))
    report.check(area, "build-status reports ok asset quality",
                 str(status_data.get("quality_status") or "") == "ok",
                 str(status_data.get("quality_status")))

    if company:
        insight = payload_of(await session.call_tool(
            "casee_get_enterprise_insight",
            {"cvc_id": DEMO_CVC, "company_name": company, "dry_run": True}))
        ins_data = _data(insight)
        note = ins_data.get("cost_note") or {}
        report.check(area, "Demo insight dry_run readable", insight.get("ok") is True,
                     str(insight)[:240])
        report.check(area, "Demo insight dry_run charges 0", note.get("charged_credits") == 0,
                     str(note))
        report.check(area, "Demo insight asks for confirmation",
                     "0.5" in str(ins_data.get("confirm_prompt") or ""),
                     str(ins_data.get("confirm_prompt"))[:160])

    denied = payload_of(await session.call_tool(
        "rebuild_cvc_model", {"cvc_id": DEMO_CVC, "confirm": True,
                              "dry_run": False, "backup": False}))
    contract_ok = _error_contract_ok(denied, report, area, "Demo write rejected")
    report.check(area, "Demo write rejection says read-only",
                 "只读" in str(denied.get("message") or ""),
                 str(denied.get("message")))
    hint = str(denied.get("hint") or "")
    report.check(area, "hint guides to creating an own analysis",
                 "专属分析" in hint and "create_cvc_model" in hint, hint[:240])
    alts = [str(a.get("tool")) for a in (denied.get("alternatives") or [])]
    report.check(area, "alternatives include free read + create path",
                 "casee_get_cvc_dashboard" in alts and "search_cvc_companies" in alts,
                 str(alts))
    report.check(area, "Demo write rejection keeps 403", denied.get("status_code") == 403,
                 str(denied.get("status_code")))
    report.check(area, "Demo write rejection does not ask to recreate the key",
                 denied.get("action") == "use_alternative",
                 str(denied.get("action")))
    if not contract_ok:
        report.note("Demo write rejection payload: " + json.dumps(denied, ensure_ascii=False)[:400])
    return DEMO_CVC, company


# ---------------------------------------------------------------------------
# S3 — own-CVC advanced capabilities
# ---------------------------------------------------------------------------

async def scenario_own_cvc(session: ClientSession, report: Report,
                           cvc_id: str, company: str) -> None:
    area = "S3 自有 CVC 高级能力"
    if not cvc_id:
        report.check(area, "a completed own CVC is available", False, "none found")
        return
    report.note(f"S3 uses own cvc_id={cvc_id} company={company or '-'}")

    status = payload_of(await session.call_tool(
        "get_cvc_build_status", {"cvc_id": cvc_id, "include_logs": False}))
    st = _data(status)
    report.check(area, "own build-status readable", status.get("ok") is True,
                 str(status)[:240])
    report.check(area, "own status_label + phases for progress UX",
                 bool(st.get("status_label")) and isinstance(st.get("phases"), list),
                 f"label={st.get('status_label')} phases={len(st.get('phases') or [])}")

    if company:
        insight = payload_of(await session.call_tool(
            "casee_get_enterprise_insight",
            {"cvc_id": cvc_id, "company_name": company, "dry_run": True}))
        data = _data(insight)
        note = data.get("cost_note") or {}
        recs = _recs(insight)
        confirm = next((r for r in recs if r.get("id") == "confirm_insight"), None)
        report.check(area, "insight dry_run ok", insight.get("ok") is True,
                     str(insight)[:240])
        report.check(area, "dry_run is explicitly free",
                     note.get("dry_run") is True and note.get("charged_credits") == 0,
                     str(note))
        report.check(area, "dry_run exposes 0.5-credit confirm prompt",
                     "0.5" in str(data.get("confirm_prompt") or ""),
                     str(data.get("confirm_prompt"))[:160])
        report.check(area, "dry_run recommends confirm_insight", confirm is not None,
                     str([r.get("id") for r in recs]))
        if confirm:
            report.check(area, "confirm_insight requires explicit user consent",
                         confirm.get("requires_user_confirm") is True
                         and (confirm.get("args") or {}).get("dry_run") is False,
                         str(confirm)[:240])
            report.check(area, "confirm_insight quotes 0.5 credit / 20s",
                         float(confirm.get("cost_credits") or 0) == 0.5
                         and int(confirm.get("eta_seconds") or 0) == 20,
                         f"{confirm.get('cost_credits')}/{confirm.get('eta_seconds')}")
    else:
        report.note("own CVC has no companies; insight dry_run skipped")

    dry = payload_of(await session.call_tool(
        "rebuild_cvc_model", {"cvc_id": cvc_id, "dry_run": True, "confirm": False,
                              "build_profile": "lite"}))
    dry_data = _data(dry)
    report.check(area, "rebuild dry_run ok (no deletion)", dry.get("ok") is True,
                 str(dry)[:240])
    report.check(area, "rebuild dry_run requires confirm",
                 dry_data.get("requires_confirm") is True
                 and dry_data.get("executed") is False, str(dry_data)[:240])
    report.check(area, "rebuild dry_run echoes build_profile=lite passthrough",
                 dry_data.get("build_profile") == "lite",
                 str(dry_data.get("build_profile")))
    report.check(area, "rebuild dry_run previews will_delete",
                 isinstance(dry_data.get("will_delete"), dict),
                 str(dry_data.get("will_delete"))[:160])

    bad_profile = payload_of(await session.call_tool(
        "rebuild_cvc_model", {"cvc_id": cvc_id, "dry_run": True,
                              "build_profile": "turbo"}))
    _error_contract_ok(bad_profile, report, area, "invalid build_profile")
    report.check(area, "invalid build_profile is VALIDATION_ERROR",
                 bad_profile.get("error_code") == "VALIDATION_ERROR",
                 str(bad_profile.get("error_code")))

    plan = payload_of(await session.call_tool(
        "casee_plan_task",
        {"goal": "帮我分析一下这家公司的风险和差距", "cvc_id": cvc_id}))
    plan_data = _data(plan)
    options = plan_data.get("options") or []
    insight_opt = next(
        (o for o in options
         if _find_step(o, "casee_get_enterprise_insight")
         or _find_step(o, "casee_get_customer_insight")), None)
    report.check(area, "company deep-dive routes to insight", insight_opt is not None,
                 str(_option_ids(options)))
    report.check(area, "deep-dive default is the insight option",
                 plan_data.get("default_option") == "C"
                 and plan_data.get("default_option") in _option_ids(options),
                 f"default={plan_data.get('default_option')} ids={_option_ids(options)}")
    if insight_opt:
        report.check(area, "insight option is 0.5 credit and dry_run-first",
                     float(insight_opt.get("cost_credits") or 0) == 0.5
                     and _find_step(insight_opt, "casee_get_enterprise_insight")
                     .get("args", {}).get("dry_run") is True,
                     str(insight_opt)[:240])
        report.check(area, "insight option states CVC + module prerequisites",
                     "cvc_id" in " ".join(map(str, insight_opt.get("requires") or [])),
                     str(insight_opt.get("requires")))

    credits = payload_of(await session.call_tool("casee_get_credits", {}))
    cdata = _data(credits)
    report.check(area, "credits expose first_cvc_free onboarding hint",
                 bool(cdata.get("first_cvc_free")), str(cdata.get("first_cvc_free")))
    report.check(area, "credits expose free capability list",
                 len(cdata.get("free_capabilities") or []) >= 5,
                 str(cdata.get("free_capabilities"))[:200])


# ---------------------------------------------------------------------------
# S4 — fallback / error guidance
# ---------------------------------------------------------------------------

async def scenario_fallbacks(session: ClientSession, report: Report,
                             cvc_id: str) -> None:
    area = "S4 错误与替代引导"

    invalid = payload_of(await session.call_tool(
        "semantic_search_tool", {"cvc_model_id": "bad id/../x", "q": "测试"}))
    _error_contract_ok(invalid, report, area, "invalid cvc_id")

    missing = payload_of(await session.call_tool(
        "casee_get_cvc_dashboard", {"cvc_id": MISSING_CVC}))
    _error_contract_ok(missing, report, area, "unknown cvc_id")
    report.check(area, "unknown cvc_id maps to NOT_FOUND",
                 missing.get("error_code") == "NOT_FOUND",
                 str(missing.get("error_code")))

    empty = payload_of(await session.call_tool("casee_plan_task", {"goal": "  "}))
    _error_contract_ok(empty, report, area, "empty goal")

    no_companies = payload_of(await session.call_tool(
        "create_cvc_model", {"name": "仿真空主体", "companies": []}))
    _error_contract_ok(no_companies, report, area, "create_cvc_model without companies")

    if cvc_id:
        trends = payload_of(await session.call_tool(
            "casee_get_market_trends", {"cvc_id": cvc_id}))
        if trends.get("ok") is True:
            report.check(area, "market_trends readable for this tenant", True)
        else:
            _error_contract_ok(trends, report, area, "market_trends denied")
            hint = str(trends.get("hint") or "")
            report.check(area, "PRODUCT_ACCESS_DENIED hint offers local aggregation",
                         trends.get("error_code") != "PRODUCT_ACCESS_DENIED"
                         or "aggregate_intelligence" in hint, hint[:240])

    # In-process contract: every well-known code always ships >=2 alternatives.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from casee_mcp_server.server import _ALTERNATIVES  # noqa: PLC0415
    for code in ("PRODUCT_ACCESS_DENIED", "CREDITS_INSUFFICIENT",
                 "VALIDATION_ERROR", "NOT_FOUND", "SCOPE_DENIED_WRITE"):
        alts = _ALTERNATIVES.get(code) or []
        report.check(area, f"{code} ships >=2 alternatives", len(alts) >= 2, str(alts))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    report = Report()
    api_key = (args.api_key or os.environ.get("CASEE_IS_API_KEY", "")).strip()
    if not api_key:
        print("ERROR: provide --api-key or export CASEE_IS_API_KEY", file=sys.stderr)
        return 2

    session_id = args.session or f"adv-{uuid.uuid4().hex[:8]}"
    report.note(f"api_key={api_key[:14]}… session={session_id} backend={args.backend}")

    proc = None
    if args.mcp_url:
        mcp_url = args.mcp_url
    else:
        from workbuddy_sim import _free_port
        port = args.port or _free_port()
        log_path = Path("/tmp") / f"casee-adv-guidance-{session_id}.log"
        proc = boot_server(api_key, args.backend, args.upstream, port, log_path)
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
        # 会话 A：入口引导 + 公共 Demo 零成本体验
        async with streamable_http_client(mcp_url, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                report.note("session A initialized")

                await scenario_entry_guidance(session, report)
                _, demo_company = await scenario_public_demo(session, report)
    finally:
        await http_client.aclose()

    # 会话 B：自有 CVC 高级能力（新会话，避免跨会话推荐去重影响断言）
    own_headers = dict(headers)
    own_headers["X-Casee-Session"] = f"{session_id}-own"
    own_client = httpx2.AsyncClient(headers=own_headers, timeout=ctx_timeout())
    try:
        async with streamable_http_client(mcp_url, http_client=own_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                report.note("session B initialized")

                overview = payload_of(await session.call_tool(
                    "casee_home_overview", {"limit": 5}))
                cvc_id, cvc_name = _pick_cvc(overview)
                if not cvc_id or cvc_id == DEMO_CVC:
                    cvc_id, cvc_name = await _find_completed_cvc(session)
                report.note(f"own cvc_id={cvc_id or '-'} name={cvc_name or '-'}")
                company = await _pick_company(session, cvc_id) if cvc_id else ""

                await scenario_own_cvc(
                    session, report, cvc_id, company or cvc_name or demo_company)
                await scenario_fallbacks(session, report, cvc_id)
    finally:
        await own_client.aclose()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()

    failed = report.failed
    print()
    print(f"RESULT: {len(report.rows) - len(failed)}/{len(report.rows)} checks passed")
    for row in failed:
        print(f"  - {row['area']} :: {row['name']} — {row['detail']}")
    print(f"session_id={session_id}")
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
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
