"""Entry-point guidance tools: ``casee_guide`` and ``casee_plan_task``.

Agents choose the shallowest tool that answers the current sentence. The
capability ladder (retrieval → CVC → semantic search → insight → report) is
invisible at that decision point, so high-value capabilities are skipped.

These two tools make the ladder explicit:

- ``casee_guide`` is the one-screen manual (tiers, decision tree, naming
  synonyms, free vs. paid) that clients can quote instead of the agent
  guessing from 60 tool names.
- ``casee_plan_task`` turns a natural-language goal into 2-3 executable
  options with cost, ETA, prerequisites and a confirmation sentence. It is
  rule-based on purpose: deterministic output is testable and cannot invent
  capabilities.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .server import _report_invalid, mcp, tool_profile_summary


DEMO_PUBLIC_CVC_ID = "cvc_demoevbat01"

_GUIDE: Dict[str, Any] = {
    "one_line": (
        "CaSee 把「公开情报检索 → 价值链建模 → 语义检索 → 经营洞察 → 报告交付」"
        "串成一条链路；先判断用户要的是「看一眼」还是「持续跟踪/深度研判/出报告」。"
    ),
    "capability_tiers": [
        {
            "tier": "T0",
            "label": "免费即得",
            "cost_credits": 0,
            "eta": "秒级",
            "tools": [
                "search_intelligence", "casee_home_overview", "casee_get_credits",
                "casee_get_cvc_dashboard", "casee_get_market_trends",
                "list_report_templates", "casee_list_cvc_companies",
                "list_cvc_templates", "get_cvc_template",
                "get_cvc_template_provision_status",
            ],
        },
        {
            "tier": "T1",
            "label": "一次性增值",
            "cost_credits": 0,
            "eta": "秒级",
            "tools": ["analyze_trend", "aggregate_intelligence", "search_with_cvc",
                      "casee_search_charts", "plan_report"],
        },
        {
            "tier": "T2",
            "label": "需要承诺（先说明成本与时长，得到确认再执行）",
            "cost_credits": "0.5-2.5",
            "eta": "8 秒 - 10 分钟（快速建链 lite ≈3-5 分钟 / 全量 full ≈10 分钟）",
            "tools": ["create_cvc_model", "create_cvc_from_template",
                      "rebuild_cvc_model",
                      "casee_get_enterprise_insight", "casee_get_market_insight",
                      "casee_get_customer_insight", "generate_report"],
        },
        {
            "tier": "T3",
            "label": "高风险（会覆盖或删除数据，必须二次确认）",
            "cost_credits": 0,
            "eta": "分钟级",
            "tools": ["rebuild_cvc_model", "intelligence_purge_tool", "cvc_cache_purge_tool"],
        },
    ],
    "decision_tree": [
        {"if": "只想看最近发生了什么 / 一次性问答", "then": "search_intelligence（免费）"},
        {"if": "要持续跟踪、跨时间对比、盯竞争对手", "then": "建链：search_cvc_companies → create_cvc_model（build_profile=lite 快速建链 1.0 credit / full 2.5 credit）"},
        {"if": "只知道一句目标 / 手上有地区、客户、竞品、行业规约",
         "then": "建链时选方式：智能推理传 objective；精确定义传 objective_definition（四维组合，地区→客户→竞品→行业规约）"},
        {"if": "不确定该搜哪些公司 / 想复用专家行业口径 / 想看现成案例",
         "then": "行业模版：list_cvc_templates → get_cvc_template → create_cvc_from_template（先 dry_run 预检，2.5 credit，首个 CVC 免费）→ get_cvc_template_provision_status → get_cvc_build_status"},
        {"if": "还没想好要不要建链 / 想先看效果", "then": f"直接看公共演示样本 cvc_id={DEMO_PUBLIC_CVC_ID}（0 credit，只读）"},
        {"if": "已有价值链，要按语义找证据", "then": "search_with_cvc（收集）→ semantic_search_tool（检索）"},
        {"if": "要判断一家公司值不值得合作 / 风险 / 差距", "then": "casee_get_enterprise_insight（0.5 credit，先 dry_run=true）"},
        {"if": "看行业规模、驱动因素、市场机会", "then": "先 casee_get_market_trends（免费），需要解释再 casee_get_market_insight"},
        {"if": "要知道我们自己的客户在想什么", "then": "casee_get_customer_insight（需 cvc_id + 对应模块）"},
        {"if": "要交付报告 / PPT / 图表", "then": "list_report_templates → plan_report → generate_report"},
    ],
    "creation_modes": {
        "note": "建链有三种方式（与门户主页一致）：按用户输入选择，不要默认只走智能推理。",
        "modes": [
            {
                "mode": "smart",
                "label": "智能推理",
                "when": "用户只给了一句自然语言目标（如「分析长时储能竞争态势」）",
                "steps": ["search_cvc_companies(query=目标)", "create_cvc_model(objective=目标)"],
                "cost_credits": 2.5,
                "eta": "构建约 3-10 分钟（lite 1.0 credit ≈3-5 分钟）",
            },
            {
                "mode": "precise",
                "label": "精确定义",
                "when": "用户给出地区 / 目标客户 / 竞品 / 行业规约中的任意一项",
                "steps": ["create_cvc_model(objective_definition={mode:'precise', ...})"],
                "rules": ("四维至少一项；服务端按 地区→客户→竞品→行业规约 组合；"
                          "组合文本 ≤200 字，单字段 ≤40 字，地区 ≤4 个"),
                "cost_credits": 2.5,
                "eta": "构建约 3-10 分钟（lite 1.0 credit ≈3-5 分钟）",
            },
            {
                "mode": "template",
                "label": "行业模版",
                "when": "用户不确定搜哪些公司 / 想复用专家行业口径 / 想看现成的行业案例",
                "steps": [
                    "list_cvc_templates（免费浏览）",
                    "get_cvc_template（免费看企业清单与准备状态）",
                    "create_cvc_from_template(dry_run=true) → 用户确认后 confirm=true",
                    "get_cvc_template_provision_status（5 秒轮询至 ready/failed）",
                    "get_cvc_build_status（看构建阶段）",
                ],
                "cost_credits": 2.5,
                "eta": "实例秒级返回，构建约 3-10 分钟；完成发邮件",
                "note": "租户首个 CVC 免费；模版实例全租户共享，已就绪的模版不重复扣费",
            },
        ],
    },
    "naming": {
        "价值链 / 竞争价值链模型 / CVC / 产业竞争模型": "同一个 cvc_model",
        "价值链检索 / 语义检索 / 深度检索 / CVC 检索": "semantic_search_tool",
        "经营洞察 / 企业洞察 / 深度研判 / 竞争情报分析": "casee_get_enterprise_insight",
        "行业趋势 / 市场趋势（免费）": "casee_get_market_trends",
        "市场洞察（收费、含驱动因素解释）": "casee_get_market_insight",
    },
    "free_vs_paid": {
        "free": ["search_intelligence", "analyze_trend", "aggregate_intelligence",
                 "casee_get_cvc_dashboard", "casee_get_market_trends",
                 "casee_get_credits", "casee_get_quota", "list_report_templates",
                 "plan_report", "list_cvc_templates", "get_cvc_template",
                 "get_cvc_template_provision_status"],
        "paid": {
            "create_cvc_model": "2.5 credits（full，约 10 分钟）/ 1.0 credit（lite 快速建链，约 3-5 分钟）；每个租户首条免费",
            "create_cvc_from_template": "2.5 credits（模版实例化 + 共享构建）；每个租户首条 CVC 免费；已就绪模版不重复扣费",
            "casee_get_enterprise_insight": "0.5 credit/次",
            "casee_get_market_insight": "0.5 credit/次",
            "casee_get_customer_insight": "0.5 credit/次",
            "generate_report": "1.0 credit/次",
        },
    },
    "free_first": {
        "why": "第一步先做一次 0 credit 的免费检索，确认情报覆盖与质量，再决定是否建链或购买洞察。",
        "try_this": {
            "tool": "search_intelligence",
            "args": {"q": "固态电池 量产 进展", "days": 30},
            "cost_credits": 0,
            "eta_seconds": 8,
        },
        "next_if_good": "需要持续跟踪再走建链：search_cvc_companies → create_cvc_model（lite 快速建链 1.0 credit，首条免费）。",
    },
    "first_time_checklist": [
        "第一步：调用 search_intelligence（免费，0 credit）看最近 30 天的高可信资讯，确认情报覆盖",
        "想看已经建好的效果：casee_get_cvc_dashboard（公共样本 cvc_id=cvc_demoevbat01，0 credit）",
        "准备建链前再用 casee_get_credits 看余额与首条 CVC 免费额度",
        "需要建链时先 search_cvc_companies 确认公司主体，再 create_cvc_model"
        "（智能推理传 objective；精确定义传 objective_definition 四维）",
        "不确定搜哪些公司时走行业模版：list_cvc_templates → create_cvc_from_template",
        "（2.5 credits，首个 CVC 免费；先 dry_run 预检拿 confirm_token，再确认执行）",
        "建链默认走 lite 快速建链（约 3-5 分钟，1.0 credit）+ 完成邮件；需要全量再 rebuild 成 full",
    ],
    "hard_rules": [
        "收费或超过 30 秒的操作，先说清 cost_credits / eta_seconds，并等待用户确认。",
        "company_name 必须来自 casee_list_cvc_companies 的返回值，不要臆造。",
        "工具返回的 recommendations / next_actions 是下一步的权威建议，优先采用。",
        "遇到 PRODUCT_ACCESS_DENIED / 0 结果 / 语义降级，按 alternatives 给出替代路径，不要静默重试。",
        "CVC 构建是异步的：同一轮对话最多轮询 3 次，其余交给 get_cvc_build_status / 邮件。",
    ],
}


@mcp.tool(
    title="CaSee 使用指南（能力分层与决策树）",
    annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 1},
)
def casee_guide(topic: str = "") -> dict:
    """一屏看懂 CaSee 该用哪个能力（免费/收费、前置、时长、命名对照）。

    USE WHEN:
    - 会话开始、用户问「你能做什么 / 有哪些能力 / 怎么用」；
    - 不确定该用检索、建链、语义检索、洞察还是报告；
    - 用户使用的词和工具名对不上（价值链 / CVC / 深度研判 / 行业趋势）。

    DO NOT USE WHEN: 用户已经明确指定对象和动作，直接调用对应工具即可。

    COST: 0 credit；无副作用。RETURNS: 能力分层、决策树、建链三种方式
    （智能推理 / 精确定义 / 行业模版）、命名对照、免费清单、首次使用清单与硬性规则。
    NEXT: 按 decision_tree 选择工具，或在不确定时调用 casee_plan_task 让服务端给出
    2-3 个方案。

    Args:
        topic: 可选，聚焦主题（如 cvc / insight / report）；留空返回完整指南
    """
    data = dict(_GUIDE)
    topic_key = (topic or "").strip().lower()
    if topic_key:
        buckets = {
            "cvc": ["decision_tree", "first_time_checklist", "free_vs_paid",
                    "creation_modes"],
            "insight": ["decision_tree", "free_vs_paid", "naming"],
            "report": ["decision_tree", "free_vs_paid", "capability_tiers"],
        }
        tree_hints = {
            "cvc": ("建链", "价值链", "语义", "cvc", "模版"),
            "insight": ("洞察", "研判", "市场", "趋势"),
            "report": ("报告", "PPT", "图表", "deck"),
        }
        keep = buckets.get(topic_key, [])
        if keep:
            data = {k: v for k, v in data.items() if k in keep or k == "one_line"}
            hints = tree_hints.get(topic_key)
            if hints and isinstance(data.get("decision_tree"), list):
                data["decision_tree"] = [
                    row for row in data["decision_tree"]
                    if any(h in f"{row.get('if', '')} {row.get('then', '')}" for h in hints)
                ]
    # 工具分层信息始终返回：告诉 Agent 当前可见工具范围，以及如何拿到完整目录。
    data["tool_profile"] = tool_profile_summary()
    return {"ok": True, "data": data, "meta": {"tier": "T0", "cost_credits": 0}}


# ---------------------------------------------------------------------------
# casee_plan_task: rule-based routing
# ---------------------------------------------------------------------------

_MONITOR_WORDS = ("持续", "跟踪", "监控", "盯着", "盯住", "盯一下", "追踪", "跟进",
                  "持续关注", "每周", "每天", "提醒", "对比历史",
                  "monitor", "track", "weekly", "alert")
_COMPANY_WORDS = ("这家公司", "供应商", "客户", "值不值得", "靠谱", "风险", "差距", "画像",
                  "供应链", "competitor", "supplier", "risk", "profile")
_MARKET_WORDS = ("市场", "行业", "赛道", "规模", "份额", "趋势", "机会", "market", "industry", "trend")
_REPORT_WORDS = ("报告", "ppt", "汇报", "图表", "简报", "report", "deck", "chart")
_CUSTOMER_WORDS = ("我们自己的客户", "客户要什么", "客户需求", "客户在想", "customer need", "voice of customer")
_ONEOFF_WORDS = ("最新", "最近", "发生了什么", "新闻", "动态", "latest", "news", "what happened")


def _detect_intent(goal: str) -> str:
    text = (goal or "").lower()
    if any(w in text for w in _CUSTOMER_WORDS):
        return "customer_analysis"
    if any(w in text for w in _REPORT_WORDS):
        return "report"
    if any(w in text for w in _MONITOR_WORDS):
        return "monitor"
    if any(w in text for w in _COMPANY_WORDS):
        return "company_deep_dive"
    if any(w in text for w in _MARKET_WORDS):
        return "market_analysis"
    if any(w in text for w in _ONEOFF_WORDS):
        return "one_off_search"
    return "unknown"


def _option(option_id: str, title: str, why: str, steps: List[dict], *,
            cost: float, eta: int, requires: List[str] | None = None,
            risk: str = "none", free_warmup: List[str] | None = None) -> dict:
    option = {
        "option_id": option_id,
        "title": title,
        "why": why,
        "steps": steps,
        "cost_credits": cost,
        "eta_seconds": eta,
        "requires": requires or [],
        "risk": risk,
    }
    if free_warmup:
        option["free_warmup"] = free_warmup
    return option


def _search_option(goal: str) -> dict:
    return _option(
        "A",
        "先做一次免费体检",
        "不需要建链，30 秒内看到最近 30 天的高可信资讯与趋势。",
        [{"tool": "search_intelligence", "args": {"q": goal, "days": 30}}],
        cost=0,
        eta=8,
    )


def _build_option(goal: str) -> dict:
    return _option(
        "B",
        "建立价值链并持续跟踪（推荐：lite 快速建链）",
        "lite 档位约 3-5 分钟、1.0 credit 即可拿到可用的最小价值链（核心企业/情报/看板齐备），"
        "之后可随时 rebuild 升级为 full 全量（2.5 credit、约 10 分钟）。",
        [
            {"tool": "search_cvc_companies", "args": {"query": goal, "limit": 8}},
            {"tool": "create_cvc_model",
             "args": {"name": f"{goal}价值链", "companies": [{"name": goal}],
                      "objective": goal, "query_topic": goal,
                      "build_profile": "lite"}},
        ],
        cost=1.0,
        eta=300,
        requires=["credits>=1.0（首条 CVC 免费；full 全量为 2.5）", "cvc:write"],
        risk="long_wait",
        free_warmup=["casee_get_cvc_dashboard", "casee_get_market_trends"],
    )


def _demo_option() -> dict:
    """零成本体验公共演示样本（只读，不计费）。"""
    return _option(
        "D",
        "0 credit 体验公共演示样本",
        "直接查看已构建好的公共样本（动力电池产业链），0 credit、秒级返回，只读；"
        "确认效果后再建自己的价值链。",
        [{"tool": "casee_get_cvc_dashboard", "args": {"cvc_id": DEMO_PUBLIC_CVC_ID}}],
        cost=0,
        eta=5,
        free_warmup=["casee_get_market_trends"],
    )


def _insight_option(goal: str, cvc_id: str, tool: str = "casee_get_enterprise_insight") -> dict:
    return _option(
        "C",
        "对已有价值链做深度洞察",
        "已有 cvc_id 时可直接生成画像、差距与风险；先 dry_run 预检，不产生扣费。",
        [{"tool": tool,
          "args": {"cvc_id": cvc_id, "company_name": goal, "dry_run": True}}],
        cost=0.5,
        eta=20,
        requires=["cvc_id", "insight:generate", "对应产品模块已开通"],
        risk="permission",
    )


@mcp.tool(
    title="任务规划（把目标转成 2-3 个可选方案）",
    annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    meta={"tier": "T0", "cost_credits": 0, "eta_seconds": 1},
)
def casee_plan_task(
    goal: str,
    cvc_id: str = "",
    budget_credits: float = 0,
    max_wait_seconds: int = 600,
    language: str = "zh",
) -> dict:
    """把用户目标转成 2-3 个可执行方案（含成本、时长、前置条件、确认话术）。

    USE WHEN:
    - 用户表达「我想… / 帮我… / 怎么…」，但没有指明用检索、建链、洞察还是报告；
    - 同一句话可以用免费检索解决，也可能需要建链，需要让用户选；
    - 不确定用户是否已有价值链（cvc_id）。

    DO NOT USE WHEN: 用户已经明确指定能力和对象（直接调用对应工具）。

    COST: 0 credit；不产生副作用，不调用后端。
    RETURNS: {ok, data: {intent, understanding, options[], ask_user, default_option,
    next_tool_if_yes}}；options 最多 3 个，且必定包含一个 0 credit 的 A 方案。
    NEXT: 把 ask_user 原样转述给用户；用户选定后按 option.steps 执行，收费步骤先确认。

    Args:
        goal: 用户目标原话（如「帮我盯一下比亚迪在欧洲的动作」）
        cvc_id: 已有的价值链 id；为空表示可能还没有建链
        budget_credits: 用户可接受的预算（0 = 未说明，仍会给出免费方案）
        max_wait_seconds: 用户可接受的最长等待秒数（0/≥300 保留 lite 建链；<300 时降级为「先看免费」）
        language: 输出语言 zh | en
    """
    text = (goal or "").strip()
    if not text:
        return _report_invalid(
            "goal is required; pass the user's original goal,"
            " e.g. goal='持续跟踪固态电池进展'",
            {"goal": goal},
        )

    intent = _detect_intent(text)
    options: List[dict] = []
    requires_cvc = intent in ("company_deep_dive", "customer_analysis")

    if intent == "report":
        options.append(_option(
            "A",
            "先预检再生成报告",
            "plan_report 免费，会列出章节、图表、缺口与兼容性；确认后再生成（1 credit）。",
            [
                {"tool": "list_report_templates", "args": {}},
                {"tool": "plan_report", "args": {"cvc_id": cvc_id or "<cvc_id>",
                                                 "template_id": "<template_id>"}},
            ],
            cost=0, eta=10, requires=["cvc_id", "template_id", "chart:read"],
        ))
        options.append(_option(
            "B",
            "直接生成报告",
            "跳过预检直接生成，1 credit、约 1-3 分钟；失败时需要重新扣费重试。",
            [{"tool": "generate_report",
              "args": {"cvc_id": cvc_id or "<cvc_id>", "template_id": "<template_id>"}}],
            cost=1.0, eta=180, requires=["report:generate", "cvc_id"],
            risk="paid_async",
        ))
    elif intent == "market_analysis":
        options.append(_option(
            "A",
            "先看免费市场趋势",
            "market_trends 免费、秒级，先看热度、来源与话题分布。",
            [{"tool": "casee_get_market_trends",
              "args": {"cvc_id": cvc_id} if cvc_id else {"topic": text}}],
            cost=0, eta=5,
        ))
        options.append(_option(
            "B",
            "生成市场洞察",
            "会解释驱动因素、机会与风险，0.5 credit、约 20 秒；需要模块权限。",
            [{"tool": "casee_get_market_insight",
              "args": {"cvc_id": cvc_id or "<cvc_id>", "topic": text, "dry_run": True}}],
            cost=0.5, eta=20, requires=["cvc_id", "insight:generate"],
            risk="permission",
        ))
        options.append(_build_option(text))
    elif requires_cvc and cvc_id:
        options.append(_insight_option(text, cvc_id,
                                       "casee_get_customer_insight"
                                       if intent == "customer_analysis"
                                       else "casee_get_enterprise_insight"))
        options.append(_search_option(text))
        options.append(_option(
            "B",
            "先看免费看板",
            "dashboard 免费，能看到已入库资讯、实体与趋势，再决定要不要生成洞察。",
            [{"tool": "casee_get_cvc_dashboard", "args": {"cvc_id": cvc_id}}],
            cost=0, eta=5,
        ))
    else:
        options.append(_search_option(text))
        options.append(_build_option(text))
        if cvc_id:
            options.append(_insight_option(text, cvc_id))
        else:
            # 没有 cvc_id 时提供零成本公共样本体验入口
            options.append(_demo_option())

    # Never offer a paid option that exceeds the stated budget.
    if budget_credits > 0:
        options = [o for o in options if o["cost_credits"] <= budget_credits] or options[:1]
    # Respect the user's patience: hide the 10-minute build when they asked for
    # a short wait, but keep the free option and one paid alternative.
    if 0 < max_wait_seconds < 300:
        options = [o for o in options if o["eta_seconds"] <= max_wait_seconds] or options[:1]
    options = options[:3]

    ids = [o["option_id"] for o in options]
    default = "C" if cvc_id and requires_cvc and "C" in ids else (
        "A" if "A" in ids else ids[0]
    )
    free_option = next((o for o in options if o["cost_credits"] == 0), options[0])
    steps = free_option.get("steps") or []
    next_yes = steps[0]["tool"] if steps else "casee_guide"

    understanding = {
        "one_off_search": "你想快速了解最近发生的事情，先用免费检索即可。",
        "monitor": "你想持续跟踪一个主题或公司，建链后历史才会沉淀并可复用。",
        "company_deep_dive": "你想判断一家公司/供应商的画像、风险或差距。",
        "market_analysis": "你想看行业规模、驱动因素与市场机会。",
        "report": "你想把结论交付成报告或图表。",
        "customer_analysis": "你想了解自己客户的需求与声音。",
        "unknown": "目标还不够具体；先免费看一次，再决定是否建链做深度分析。",
    }[intent]

    data = {
        "intent": intent,
        "understanding": understanding,
        "options": options,
        # 建链方式与主页一致：智能推理 / 精确定义 / 行业模版。
        # 用户说「不知道搜哪些公司」或「有没有现成模版」时，按 creation_modes.template 走。
        "creation_modes": _GUIDE["creation_modes"],
        "ask_user": (
            f"你更想要 {options[0]['option_id']}（{options[0]['title']}）"
            + (f" 还是 {options[1]['option_id']}（{options[1]['title']}）？"
               if len(options) > 1 else "？")
        ),
        "default_option": default,
    }
    if next_yes:
        data["next_tool_if_yes"] = next_yes
    if language == "en":
        data["note"] = "Tool descriptions are in English; ask_user follows the goal language."
    return {"ok": True, "data": data, "meta": {"tier": "T0", "cost_credits": 0}}
