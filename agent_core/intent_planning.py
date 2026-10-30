"""Contract22 planning guidance; strategic diversity remains a model judgment."""

import json
from dataclasses import dataclass


REVIEW_HISTORY_GUIDANCE = (
    "later_intent_results links an earlier continuation review to the later committed result of the same Intent. "
    "Read both in order and within their recorded source versions and local scope. An earlier request for more "
    "evidence is not automatically an outstanding obligation after a later reviewed completion; observations "
    "and counterevidence remain relevant, and a later result is not proof of runtime effects or task success. "
    "Before reopening a completed phase, explain new counterevidence, a concrete uncovered criterion or a new "
    "validation purpose in the existing reason. Summary omissions or the earlier reviewer opinion alone do "
    "not establish that work was never completed. Revisit when justified; this is not a prohibition. "
)


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


DIAGNOSTIC_GUIDANCE = (
    "Selective diagnosis is part of the next normal decision, not a question for the user or an extra model call. "
    "Recheck assumptions when expanding enumeration while a key prerequisite or the ability to detect a correct "
    "candidate is unverified; when repeated variants neither exclude candidates nor distinguish explanations; "
    "or when a reliable observation contradicts an explicit prediction, even after one experiment. "
    "Ask what the observation actually rules out, which prerequisites remain unobserved, and what next check "
    "would separate the plausible explanations. Use existing reason or working_record only when useful, with "
    "original evidence refs; do not add output fields, force a fixed number of explanations or invent a cause. "
    "A planned variant is not mandatory merely because it appeared in an initial checklist. After a prerequisite "
    "is contradicted or an operation is infeasible, explain how the next check could change that prerequisite "
    "or distinguish another live explanation; otherwise consider a supported alternative or scoped closure. "
    "Source-supported alternatives remain hypotheses until their own prerequisites and effects are tested. "
    "A bounded enumeration with a working detection method and increasing coverage, or justified repetition "
    "for noise/timing/stability, can continue without diagnosis at each step. Elapsed time, request count, "
    "identical responses and absence of Facts alone never trigger diagnosis or prove stagnation. "
    "First distinguish tool, transport, permission or measurement failures from target counterevidence. "
    "For a related series testing one hypothesis, optionally reuse a short exploration_group label on tool "
    "decisions, including parameter variants or different tools; omit for unrelated work. Labels are advisory, "
    "not evidence or permissions. repetition_advice is a matching-observation checkpoint: consider relevant "
    "Facts, observations and working records, then decide whether to continue, discriminate or change method. "
    "An equal result can still exclude candidates or increase finite coverage; no forced explanation or switch. "
    "A completed tool result is feedback, including an error or negative result. In the next useful decision, "
    "account for what it supports, rules out or leaves unchanged. If an unchanged action still matters, "
    "explain its control, state-change or coverage purpose in the existing reason/working_record when useful. "
    "Do not mechanically change direction after every result or repeat a contradicted premise without a new check. "
    "A diagnostic explanation remains an unverified hypothesis; a negative observation need not identify its cause. "
    "Once a diagnostic check is planned and progressing, do not repeat the same reflection at each review. "
    "Revisit it only if new results contradict it, it is infeasible or cannot discriminate, exploration expands "
    "again, or its stated expected progress does not occur. Preserve tested and untested assumptions in useful "
    "handoff notes. Supervisors may question the experiment's discriminating value, but diagnostic advice is "
    "not a mandatory interruption; the inner may explain disagreement and continue within existing authority. "
    "When supported components have been identified but their connection remains untested, identify that "
    "specific connection and choose a bounded check with distinguishable outcomes, or explain the missing "
    "capability/prerequisite in an existing reason or working_record. Do not require another broad source "
    "inventory merely because more files exist. A static Fact does not verify a composed runtime route. "
    "For source work, reuse preserved reading indexes and current authorized attachment bindings; obtain a "
    "current-Intent materialization path through the declared attachment observation/share capability when "
    "needed. A sibling's workspace path is not current access permission. Do not scan the sandbox root to "
    "rediscover known input material, and do not confuse the local sandbox with the target deployment."
)

PUBLIC_KNOWLEDGE_GUIDANCE = (
    "When a next decision depends on an unconfirmed public technical claim and public retrieval is declared, "
    "identify that knowledge gap and choose either original-source search/fetch or a discriminating local check. "
    "Use retrieval for uncertain behavior, compatibility or mechanisms when it can change the next decision; "
    "a version string alone does not require search. When an uncertain public mechanism is the obstacle, "
    "make the next useful choice either an original-source lookup or a bounded discriminating local test, "
    "rather than repeatedly restating that knowledge is missing. Use existing reason/working_record to explain "
    "the decision this would change; do not request another model call just to decide whether to search. "
    "Prefer original documentation and verify applicability "
    "against current observations. Search results are references, not target evidence or verified causes. "
    "No search count, repeated response or elapsed time alone establishes stagnation. "
    "Use existing reason or working_record when useful, without an extra call or invented introspection. "
    "Supervisors may suggest a useful lookup; the inner may choose an effective local experiment instead. "
    "Respect the frozen public-data boundary and task retrieval permissions; do not send private material."
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
    "同一解决方向。沿前置方向深入或重访已完成阶段时，在现有理由中说明新增反证、具体未覆盖判据或新的验证用途；旧补证意见本身不等于当前仍有缺口。"
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
        from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
        raise ExecutionRecoveryRequired("d22_intent_selection_source_missing")
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


@dataclass(frozen=True)
class IntentTurnProposal:
    turn_target: str
    intents: tuple


def parse_directions(request,raw,revisions):
    from agent_core.handoff_contracts import checked_refs
    from agent_core.modeling import IntentCandidate
    from agent_core.handoff import D8PlanningResult,DependencyCandidatesRejected
    raw = without_order_reason(request, raw)
    if not isinstance(raw,dict):raise ValueError('intent.planning_invalid')
    if raw.get('kind')=='exhausted':
        if set(raw)!={'kind','reason','evidence_refs'} or not isinstance(raw['reason'],str) or not raw['reason'].strip():
            raise ValueError('intent.exhaustion_invalid')
        checked_refs(raw['evidence_refs'],{i.ref for i in request.history})
        return D8PlanningResult(None,(),(),raw['reason'])
    maximum=candidate_limit(request)
    if (set(raw)!={'kind','intents'} or raw['kind']!='candidates' or not isinstance(raw['intents'],list)
            or not raw['intents'] or maximum is not None and len(raw['intents'])>maximum):
        raise ValueError('intent.planning_invalid')
    available={i.ref for i in request.history}
    kept,deps,rejected=[],[],[]
    for index,item in enumerate(raw['intents']):
        if not isinstance(item,dict) or set(item)!={'description','intent_type','dependency_refs'}:
            raise ValueError('intent.candidate_fields_invalid')
        refs=checked_refs(item['dependency_refs'],available)
        if any(i.source_status!='available' or i.conflict or revisions and not revisions.reliable(i)
               for i in request.history if i.ref in refs):
            rejected.append(index)
            continue
        kept.append(IntentCandidate(item['description'],item['intent_type']))
        deps.append(refs)
    if not kept:raise DependencyCandidatesRejected(rejected)
    return D8PlanningResult(IntentTurnProposal(request.goal,tuple(kept)),tuple(deps),tuple(rejected))


def direction_schema(request):
    from agent_core.output_contracts import tagged, array, INTENT, REFS, text
    return {"anyOf": [candidate_schema(request, {"intents": array(INTENT, 1, candidate_limit(request))}),
                      tagged("exhausted", {"reason": text(512), "evidence_refs": REFS})]}
