#!/usr/bin/env python3
"""WorkBuddy expertTeam 专家交互流程 ↔ CaSee MCP 全链路仿真（可离线运行）。

目的
----
参照 ``WorkBuddy/expertTeam`` 的专家交互定义（``skills/casee-intelligence/SKILL.md``、
``agents/casee-team-lead.md``、``analysis/competition_workflow_zh.md``、``.mcp.json``），
把「专家团队怎么用 CaSee」还原成一条可执行的客户端轨迹，验证 v1.13.1 MCP Server
**是否能把一个模糊提问引导到「竞争价值链（CVC）检索」并跑通闭环**：

    模糊目标 → casee_guide / casee_plan_task 引导
             → search_cvc_companies 确认主体
             → create_cvc_model（lite）拿 cvc_id + 免费预热
             → search_with_cvc 采集语料
             → semantic_search_tool 语义检索
             → 洞察/报告（收费步骤先确认）

与 ``workbuddy_ux_sim.py`` 的区别
--------------------------------
后者验证「接入契约」（鉴权、工具分层、令牌、归因）；本脚本验证「专家工作流」
的引导质量与闭环可执行性，复用同一套 mock 后端 + 真实 MCP server 子进程 +
官方 MCP client 扮演 WorkBuddy。

运行
----
    cd design/CaSee-v2.0/AIAgent/casee_mcp_server
    PYTHONPATH=src uv run --no-project --with mcp --with httpx --with starlette \\
        --with ./casee-2.0.0-py3-none-any.whl --python 3.12 \\
        python tests/simulation/workbuddy_expert_team_sim.py

    --keep-logs  保留 MCP server 日志并打印路径
    --json       打印机器可读结果

退出码 0 表示全部通过。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from mock_casee_backend import MockBackend
from workbuddy_ux_sim import (  # noqa: F401 - 复用接入仿真框架
    ClientSession, Report, SIM_API_KEY, SRC_ROOT, _free_port, _stop, boot_server,
    option_shape_ok, payload_of, recommendations_of, wait_ready, with_session,
)

GOAL = "帮我持续跟踪长时储能竞争格局"
Q = "长时储能 竞争格局"
CVC_ID = "cvc_sim000001"
FRESH_CVC_ID = "cvc_uncollected01"


# ---------------------------------------------------------------------------
# 断言小工具
# ---------------------------------------------------------------------------

def rec_by_id(payload: dict, rec_id: str) -> dict:
    for rec in recommendations_of(payload):
        if rec.get("id") == rec_id:
            return rec
    return {}


def step_tools(option: dict) -> list[str]:
    return [str((s or {}).get("tool") or "") for s in option.get("steps") or []]


def option_by_id(payload: dict, option_id: str) -> dict:
    for opt in (payload.get("data") or {}).get("options") or []:
        if opt.get("option_id") == option_id:
            return opt
    return {}


def items_of(payload: dict) -> list:
    return payload.get("organic_results") or payload.get("items") or []


# ---------------------------------------------------------------------------
# 场景 1：模糊提问 → 首屏引导把「CVC 检索」讲清楚
# ---------------------------------------------------------------------------

async def scenario_guide(session, report: Report) -> None:
    area = "引导① CVC 首屏"

    full = payload_of(await session.call_tool("casee_guide", {}))
    data = full.get("data") or {}
    naming = data.get("naming") or {}
    cvc_hits = [v for k, v in naming.items() if "价值链检索" in k or "CVC 检索" in k]
    report.check(area, "命名对照把「价值链检索」映射到 semantic_search_tool",
                 cvc_hits == ["semantic_search_tool"], str(naming)[:240])

    report.check(area, "硬性规则要求收费/长耗时先确认",
                 any("确认" in str(r) for r in data.get("hard_rules") or []),
                 str(data.get("hard_rules"))[:240])

    themed = payload_of(await session.call_tool("casee_guide", {"topic": "cvc"}))
    themed_data = themed.get("data") or {}
    tree_text = json.dumps(themed_data.get("decision_tree") or [], ensure_ascii=False)
    report.check(area, "CVC 决策树给出「已有价值链→采集→语义检索」",
                 "search_with_cvc" in tree_text and "semantic_search_tool" in tree_text,
                 tree_text[:240])

    modes = {m.get("mode") for m in
             ((themed_data.get("creation_modes") or {}).get("modes") or [])}
    report.check(area, "建链三方式齐全（smart/precise/template）",
                 modes == {"smart", "precise", "template"}, str(modes))

    template_steps = next((m.get("steps") or [] for m in
                           ((themed_data.get("creation_modes") or {}).get("modes") or [])
                           if m.get("mode") == "template"), [])
    report.check(area, "模版路径含 dry_run→confirm→轮询准备状态",
                 any("dry_run" in str(s) for s in template_steps)
                 and any("provision_status" in str(s) for s in template_steps),
                 str(template_steps)[:240])

    profile = themed_data.get("tool_profile") or {}
    hidden = [str(n) for n in profile.get("hidden_tools") or []]
    report.note(f"当前工具分层 profile={profile.get('profile')} "
                f"hidden={len(hidden)}；search_with_cvc 隐藏={('search_with_cvc' in hidden)}")


# ---------------------------------------------------------------------------
# 场景 2：casee_plan_task 把目标转成可执行方案（含 0 credit 与建链）
# ---------------------------------------------------------------------------

async def scenario_plan(session, report: Report) -> None:
    area = "引导② 方案预检"
    res = payload_of(await session.call_tool("casee_plan_task", {"goal": GOAL}))
    data = res.get("data") or {}
    options = data.get("options") or []

    report.check(area, "intent 识别为持续跟踪（monitor）",
                 data.get("intent") == "monitor", str(data.get("intent")))
    report.check(area, "给出 1-3 个方案且字段契约完整",
                 1 <= len(options) <= 3 and all(option_shape_ok(o) for o in options),
                 str([o.get("option_id") for o in options]))
    report.check(area, "必含 0 credit 起步方案",
                 any(float(o.get("cost_credits") or 0) == 0 for o in options),
                 str([o.get("cost_credits") for o in options]))
    report.check(area, "ask_user 澄清话术可直接转述", bool(data.get("ask_user")),
                 str(data.get("ask_user"))[:160])

    build = option_by_id(res, "B")
    report.check(area, "建链方案按 search_cvc_companies→create_cvc_model 编排",
                 step_tools(build)[:2] == ["search_cvc_companies", "create_cvc_model"],
                 str(step_tools(build)))
    create_step = next((s for s in build.get("steps") or []
                        if (s or {}).get("tool") == "create_cvc_model"), {})
    report.check(area, "建链方案默认 lite（1.0 credit，约 3-5 分钟）",
                 (create_step.get("args") or {}).get("build_profile") == "lite"
                 and float(build.get("cost_credits") or 0) == 1.0,
                 f"{create_step.get('args')} cost={build.get('cost_credits')}")
    report.check(area, "建链方案告知首条免费与前置权限",
                 any("首条" in str(r) for r in build.get("requires") or []),
                 str(build.get("requires")))
    report.check(area, "plan 同时返回建链三方式供用户选择",
                 {m.get("mode") for m in
                  ((data.get("creation_modes") or {}).get("modes") or [])}
                 == {"smart", "precise", "template"},
                 str(data.get("creation_modes"))[:200])


# ---------------------------------------------------------------------------
# 场景 3：按方案建链，拿到 cvc_id 与免费预热
# ---------------------------------------------------------------------------

async def scenario_build(session, report: Report) -> str:
    area = "闭环① 建链"

    searched = payload_of(await session.call_tool(
        "search_cvc_companies", {"query": GOAL, "limit": 8}))
    search_data = searched.get("data") or {}
    candidates = [c for c in search_data.get("items") or [] if c.get("selected")]
    report.check(area, "企业检索返回可确认的候选公司",
                 searched.get("ok") is True and len(candidates) >= 2,
                 str(search_data.get("items"))[:240])
    report.check(area, "企业检索给出建议价值链名",
                 bool(search_data.get("suggested_cvc_name")),
                 str(search_data.get("suggested_cvc_name")))

    name = str(search_data.get("suggested_cvc_name") or f"{GOAL}价值链")
    created = payload_of(await session.call_tool("create_cvc_model", {
        "name": name,
        "companies": [{"name": c["name"]} for c in candidates],
        "objective": GOAL,
        "query_topic": GOAL,
        "build_profile": "lite",
    }))
    created_data = created.get("data") or {}
    cvc_id = str(created_data.get("cvc_id") or "")
    report.check(area, "建链成功并返回 cvc_id",
                 created.get("ok") is True and cvc_id.startswith("cvc_"),
                 str(created_data)[:240])
    report.check(area, "告知构建时长与完成邮件（不阻塞会话）",
                 "邮件" in str(created_data.get("user_message"))
                 and "3-5" in str(created_data.get("user_message")),
                 str(created_data.get("user_message"))[:240])
    report.check(area, "下一步指引 rebuild_cvc_model",
                 (created_data.get("next_step") or {}).get("tool") == "rebuild_cvc_model",
                 str(created_data.get("next_step"))[:200])
    warmup = [w.get("tool") for w in created_data.get("free_warmup") or []]
    report.check(area, "构建期间给出免费预热能力",
                 warmup == ["casee_get_cvc_dashboard", "casee_get_market_trends"],
                 str(warmup))
    start_build = rec_by_id(created, "start_build")
    report.check(area, "推荐 start_build 带成本/时长/确认标记",
                 start_build.get("tool") == "rebuild_cvc_model"
                 and int(start_build.get("eta_seconds") or 0) >= 300,
                 str(start_build)[:240])

    dashboard = payload_of(await session.call_tool(
        "casee_get_cvc_dashboard", {"cvc_id": cvc_id}))
    report.check(area, "预热看板可直接执行（无 404/403）",
                 dashboard.get("ok") is True
                 and bool((dashboard.get("data") or {}).get("summary")),
                 str(dashboard.get("data"))[:200])
    trends = payload_of(await session.call_tool(
        "casee_get_market_trends", {"cvc_id": cvc_id}))
    report.check(area, "预热市场趋势可直接执行（无 404/403）",
                 trends.get("ok") is True
                 and bool((trends.get("data") or {}).get("trend")),
                 str(trends.get("data"))[:200])
    return cvc_id


# ---------------------------------------------------------------------------
# 场景 4：两段式 CVC 检索闭环（核心）
# ---------------------------------------------------------------------------

async def scenario_two_stage(session, report: Report, mock: MockBackend,
                             cvc_id: str) -> None:
    area = "闭环② 采集→语义检索"

    collected = payload_of(await session.call_tool(
        "search_with_cvc", {"q": Q, "cvc_model_id": cvc_id, "days": 30}))
    sync = collected.get("cvc_sync_status") or {}
    report.check(area, "search_with_cvc 采集到语料并回传 cvc_sync_status",
                 "cvc_sync_status" in collected and int(sync.get("items_found") or 0) >= 3,
                 str(sync)[:200])
    after = rec_by_id(collected, "semantic_after_collect")
    report.check(area, "采集后引导调用 semantic_search_tool",
                 after.get("tool") == "semantic_search_tool"
                 and (after.get("args") or {}).get("cvc_model_id") == cvc_id,
                 str(after)[:240])
    searchx = mock.last("/v1/searchx") or {}
    report.check(area, "检索请求透传调用方 Key 与 cvc_model_id",
                 searchx.get("headers", {}).get("x-api-key") == SIM_API_KEY
                 and searchx.get("query", {}).get("cvc_model_id") == cvc_id,
                 f"{searchx.get('query')} key={searchx.get('headers', {}).get('x-api-key')}")
    report.check(area, "mock 侧确认语料已写入该 cvc",
                 bool(mock.collected(cvc_id)), f"{len(mock.collected(cvc_id))} links")

    semantic = payload_of(await session.call_tool(
        "semantic_search_tool", {"cvc_model_id": cvc_id, "q": Q, "mode": "hybrid"}))
    fusion = ((semantic.get("search_information") or {}).get("fusion") or {})
    report.check(area, "semantic_search_tool 返回命中结果",
                 "error" not in semantic and len(items_of(semantic)) >= 3,
                 f"items={len(items_of(semantic))}")
    report.check(area, "fusion 统计可见且未降级",
                 fusion.get("degraded") is False
                 and int(fusion.get("bm25_hits") or 0) > 0,
                 str(fusion))
    deep = rec_by_id(semantic, "deep_dive_insight")
    report.check(area, "命中后引导洞察（dry_run 预检、0.5 credit）",
                 deep.get("tool") == "casee_get_enterprise_insight"
                 and float(deep.get("cost_credits") or 0) == 0.5
                 and (deep.get("args") or {}).get("dry_run") is True,
                 str(deep)[:240])
    semantic_call = mock.last("/v1/semantic-search") or {}
    sent = json.loads(semantic_call.get("body") or "{}")
    report.check(area, "语义检索请求体带 cvc_model_id 与 mode",
                 sent.get("cvc_model_id") == cvc_id and sent.get("mode") == "hybrid",
                 str(sent)[:200])
    report.check(area, "推荐条数不超过 3 且字段齐全",
                 len(recommendations_of(semantic)) <= 3
                 and all({"id", "kind", "priority", "message", "tool",
                          "args", "eta_seconds"} <= set(r) for r in
                         recommendations_of(semantic)),
                 str([r.get("id") for r in recommendations_of(semantic)]))


# ---------------------------------------------------------------------------
# 场景 5：未采集就语义检索 → 推荐把用户拉回「先采集再检索」
# ---------------------------------------------------------------------------

async def scenario_uncollected(session, report: Report, mock: MockBackend) -> None:
    area = "引导③ 未采集反向引导"
    q = "客户对液流电池循环寿命的真实抱怨"

    first = payload_of(await session.call_tool(
        "semantic_search_tool", {"cvc_model_id": FRESH_CVC_ID, "q": q}))
    report.check(area, "未采集模型检索 0 条但仍 ok（不报错）",
                 "error" not in first and not items_of(first),
                 f"error={first.get('error')} items={len(items_of(first))}")
    retry = rec_by_id(first, "collect_then_retry")
    report.check(area, "推荐 collect_then_retry 指回 search_with_cvc",
                 retry.get("tool") == "search_with_cvc"
                 and (retry.get("args") or {}).get("cvc_model_id") == FRESH_CVC_ID,
                 str(retry)[:240])

    # WorkBuddy 会照 recommendations 执行——验证「照做就能成功」
    follow = payload_of(await session.call_tool(
        "search_with_cvc", {"q": q, "cvc_model_id": FRESH_CVC_ID}))
    report.check(area, "按推荐执行采集成功",
                 int((follow.get("cvc_sync_status") or {}).get("items_found") or 0) > 0,
                 str(follow.get("cvc_sync_status"))[:200])
    second = payload_of(await session.call_tool(
        "semantic_search_tool", {"cvc_model_id": FRESH_CVC_ID, "q": q}))
    report.check(area, "采集后重试语义检索命中（引导闭环成立）",
                 bool(items_of(second))
                 and not rec_by_id(second, "collect_then_retry"),
                 f"items={len(items_of(second))}")
    report.check(area, "mock 侧两次请求都打到同一 cvc",
                 len(mock.collected(FRESH_CVC_ID)) > 0,
                 str(sorted(mock.collected(FRESH_CVC_ID))[:2]))


# ---------------------------------------------------------------------------
# 场景 6：无 cvc_id / 0 结果 的兜底引导
# ---------------------------------------------------------------------------

async def scenario_fallbacks(session, report: Report) -> None:
    area = "引导④ 兜底路径"

    plain = payload_of(await session.call_tool("search_with_cvc", {"q": Q}))
    report.check(area, "不带 cvc_model_id 时明确告知「不会沉淀」",
                 "error" not in plain and "cvc_sync_status" not in plain
                 and rec_by_id(plain, "no_cvc_collection").get("tool") == "casee_plan_task",
                 str(plain.get("recommendations"))[:240])
    report.check(area, "普通检索仍返回结果（不阻断一次性问答）",
                 len(items_of(plain)) >= 3, f"items={len(items_of(plain))}")

    empty = payload_of(await session.call_tool(
        "search_intelligence", {"q": "空结果查询 长时储能"}))
    widen = rec_by_id(empty, "widen_window")
    report.check(area, "0 结果时引导放宽窗口（days=90 / min_tscore=0）",
                 not items_of(empty) and widen.get("tool") == "search_intelligence"
                 and (widen.get("args") or {}).get("days") == 90
                 and (widen.get("args") or {}).get("min_tscore") == 0.0,
                 str(widen)[:240])
    report.check(area, "0 结果同时引导 casee_plan_task 给出方案",
                 rec_by_id(empty, "plan_next_step").get("tool") == "casee_plan_task",
                 str(empty.get("recommendations"))[:240])


# ---------------------------------------------------------------------------
# 场景 7：语义检索降级 → 说明质量风险并回到采集
# ---------------------------------------------------------------------------

async def scenario_degraded(session, report: Report, cvc_id: str) -> None:
    area = "引导⑤ 语义降级"
    res = payload_of(await session.call_tool(
        "semantic_search_tool", {"cvc_model_id": cvc_id, "q": "降级 语义检索"}))
    fusion = ((res.get("search_information") or {}).get("fusion") or {})
    rec = rec_by_id(res, "semantic_degraded")
    report.check(area, "降级时 fusion.degraded=true 仍返回关键词结果",
                 fusion.get("degraded") is True and bool(items_of(res)),
                 str(fusion))
    report.check(area, "降级推荐说明风险并指回 search_with_cvc",
                 rec.get("kind") == "quality_warning"
                 and rec.get("tool") == "search_with_cvc"
                 and "降级" in str(rec.get("message")),
                 str(rec)[:240])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    report = Report()
    mock = MockBackend()
    backend = args.backend or mock.start()
    own_backend = not args.backend
    if own_backend:
        report.note(f"mock 后端已启动：{backend}")

    stamp = int(time.time())
    log_path = Path(tempfile.gettempdir()) / f"casee-expert-team-{stamp}.log"
    port = args.port or _free_port()
    proc = None
    try:
        proc = boot_server(backend, port, log_path, "core")
        if not await wait_ready(port, proc):
            print(f"ERROR: MCP server 启动失败，日志见 {log_path}", file=sys.stderr)
            return 2
        mcp_url = f"http://127.0.0.1:{port}/mcp"
        report.note(f"MCP server：{mcp_url}（profile=core）")

        await with_session(mcp_url, SIM_API_KEY, f"expert-guide-{stamp}",
                           lambda s: scenario_guide(s, report))
        await with_session(mcp_url, SIM_API_KEY, f"expert-plan-{stamp}",
                           lambda s: scenario_plan(s, report))

        holder: dict[str, str] = {}

        async def build_run(session) -> None:
            holder["cvc_id"] = await scenario_build(session, report)

        await with_session(mcp_url, SIM_API_KEY, f"expert-build-{stamp}", build_run)
        cvc_id = holder.get("cvc_id") or CVC_ID

        await with_session(mcp_url, SIM_API_KEY, f"expert-two-stage-{stamp}",
                           lambda s: scenario_two_stage(s, report, mock, cvc_id))
        await with_session(mcp_url, SIM_API_KEY, f"expert-uncollected-{stamp}",
                           lambda s: scenario_uncollected(s, report, mock))
        await with_session(mcp_url, SIM_API_KEY, f"expert-fallback-{stamp}",
                           lambda s: scenario_fallbacks(s, report))
        await with_session(mcp_url, SIM_API_KEY, f"expert-degraded-{stamp}",
                           lambda s: scenario_degraded(s, report, cvc_id))
    finally:
        _stop(proc)
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
        print(f"server log: {log_path}")
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if not failed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="",
                        help="复用已存在的 CaSee 后端（默认启动进程内 mock）")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--keep-logs", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
