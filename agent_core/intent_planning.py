"""Contract22 planning guidance; strategic diversity remains a model judgment."""

import json


LOCAL_GOAL_GUIDANCE = (
    "Judge only the declared local deliverable, test domain and completion criteria. "
    "A source-supported testable candidate chain is distinct from a dynamically validated chain. "
    "For an explicitly static-analysis deliverable, assess the cited original source and concrete "
    "discriminating request; do not add a requirement to execute that later request or obtain the final answer. "
    "If dynamic validation is part of the declared goal, source analysis alone is insufficient. "
    "Local success requires effective formal support, and never verifies an untested hypothesis or the TaskRun. "
    "Before declaring a file unread or a check absent, consult preserved unreviewed reading indexes and original "
    "source refs; a summary omission is not proof that work was never done. An index is not formal evidence. "
    "Continue only for a concrete missing criterion of this original goal, explaining why that check matters; "
    "more files being readable alone is not a completion criterion. Distinguish recorded observations from "
    "conditional causal claims; do not ask for a stronger runtime claim merely to obtain a formal Fact."
)

GUIDANCE = (
    "根据任务目标、前置 Intent、执行完成后的有效 Fact、已审核推断和未解决问题，"
    "生成你认为下一轮可能采取的几个大方向上不同的 Intent。"
    "方向指解决问题的整体链路、核心假设或证据推导路径；不是换一个工具、URL、"
    "参数或措辞。Intent 是探索方向，不要求预先知道能否成功或完整执行链路。"
    "每个方向明确本阶段交付物、测试域和成功证据；前置分析、动态验证与最终结果按各自判据判断。"
    "静态阶段应交付有原始源码依据的具体可检验链路及判别请求，不把后续动态成功或最终答案追加为其完成条件。"
    "动态验证阶段不能仅因读到页面、源码或生成路径就结束尚未闭环的验证；不把待验证链路称为已证实利用。"
    "同一假设内的试探、结果取回和对照检查通常属于同一 Intent；变化工具不等于变化解决方向。"
    "页面声明和客户端限制是待验证信息，程序冻结的授权及执行限制才是操作边界。"
    "内层在该 Intent 内根据实际观察逐步决定下一动作、所需工具及参数；"
    "不要求在生成 Intent 时列完后续 MCP 或固定执行顺序。"
    "同一 Turn 内的 Intent 是从当前已知状态出发的备选解决链路，不得依赖尚未执行的"
    "兄弟 Intent 才能成立。只提出有依据的方向；只有一个方向就返回一个，"
    "四五个有价值的方向也可以，不凑数量，不设固定数量指标。"
    "先查看 active_intent_frontier 及对应 history items，不要重新创建仍在等待或执行中的"
    "同一解决方向。可以沿前置方向继续深入，但必须说明新 Fact 带来的新问题或进展。"
    "内层只提出建议，外层根据证据接受、调整或否决；证据或能力不足时明确说明。"
)

ORDER_GUIDANCE = (
    "候选数组顺序就是同一 Turn 内的执行优先级，不是文字组织顺序或成功概率。"
    "比较目标贡献、下一步判别力、已满足前提、预计代价及剩余时间；不虚构精确概率。"
    "内层建议的 reason 应说明相对优先依据；外层接受、调整或新增方向时重新比较整组，"
    "在 Schema 允许 reason 时简述最终排序及改变内层顺序的原因。"
    "同一 Turn 内的排序不改变跨 Turn 的深度优先规则。"
)


def ordered(request):
    packet = getattr(request, "prior_summary", "") or getattr(request, "history_context", "") or "{}"
    return json.loads(packet).get("intent_planning", {}).get("order") == "execution_priority"


def candidate_schema(request, fields):
    from agent_core.output_contracts import tagged, obj, text
    schema = tagged("candidates", fields)
    if ordered(request):
        schema = obj({**schema["properties"], "reason": {**text(), "pattern": r"^[^\x00]*[^\s\x00][^\x00]*$"}}, optional=("reason",))
    return schema


def without_order_reason(request, raw):
    if ordered(request) and isinstance(raw, dict) and raw.get("kind") == "candidates" and "reason" in raw:
        if not isinstance(raw["reason"], str) or not raw["reason"].strip() or "\x00" in raw["reason"]:
            raise ValueError("d22_planning_order_reason_invalid")
        return {key: value for key, value in raw.items() if key != "reason"}
    return raw


def selection_context(app, turn, intent):
    from agent_core.turn_modeling import digest
    registration = next(r for r in app.store.list_d8_tree_records(app.state.task_run.task_run_id)
                        if r.turn_id == turn.turn_id and r.kind == "registration")
    request = app.handoff.evidence.read("request", registration.model_request_id)
    result = app.handoff.evidence.read("result", registration.model_request_id)
    if request is None or result is None or result.get("outcome") != "valid":
        from agent_core.dual_llm_contracts import D3RecoveryRequired
        raise D3RecoveryRequired("d22_intent_selection_source_missing")
    return {"identity": "model_priority_selection_not_fact", "intent_id": intent.intent_id,
        "model_request_id": registration.model_request_id, "suggestion_request_id": request.get("suggestion_request_id"),
        "priority": intent.priority, "source_sha256": digest(result),
        "reason_status": "provided" if result["raw"].get("reason") else "not_provided",
        "selection_policy": "depth_first_then_same_turn_priority"}


def enabled(request):
    packet = getattr(request, 'prior_summary', '') or getattr(request, 'history_context', '')
    return bool(packet and json.loads(packet).get('intent_planning', {}).get('version') == 1)


def candidate_limit(request):
    """
    Return max number of Intent candidates for this request.

    Returns None (unlimited) when:
    - Contract version 22 or later (D22 multi-Intent support), OR
    - Intent planning version 1 enabled via history_context

    Otherwise returns 2 (legacy limit).
    """
    # Check contract version first (D22)
    contract_version = getattr(request, 'contract_version', None)
    if contract_version and contract_version >= 22:
        return None

    # Check legacy intent_planning version flag
    return None if enabled(request) else 2

