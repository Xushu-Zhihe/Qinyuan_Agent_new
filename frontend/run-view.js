"use strict";

// Presentation only: no scheduling, state transitions, or success inference.
const RunView = (() => {
  const get = (id) => document.getElementById(id);
  const labels = { pending: "待执行", suspended: "技术挂起", running: "运行中", awaiting_external_feedback: "等待外部反馈", achieved: "目标已确认", accepted: "已接受", unknown: "未知，待核查", conflict: "裁决冲突，待核查", not_evaluated: "未评估", succeeded: "已通过", completed: "已完成", failed: "失败", inconclusive: "无法判定", timed_out: "超时", cancelled: "已取消", rejected: "已拒绝", starting: "启动中", cancelling: "取消与清理中", cleaning: "资源清理中", cleanup_confirmed: "资源已清理", paused: "已暂停 · Sandbox 保留", recovery_required: "需要显式恢复", deadline_exceeded: "总时限已到", finished: "已结束" };
  const actionLabels = { probe: "提出下一次探测", complete_intent: "提出完成 Intent", abandon_intent: "放弃当前 Intent", candidate_flag: "提交待验证候选", awaiting_response: "等待模型响应", model_error: "模型错误", invalid_structure: "结构校验失败" };
  let current = null;
  let filter = null;
  let cache = new Map();
  const validationQueries = new Map();
  let generation = 0;
  let activeTab = "detail";
  let graphZoom = null;
  const label = (value) => value === "strategy_yielded" ? "策略让位 · 待续" : labels[value] || value || "—";
  const intentStatus = (intent) => intent.status === "suspended" && intent.suspension_kind === "strategy" ? "strategy_yielded" : intent.status;
  const routes = (turn) => turn.roads || turn.intents || [];
  const routeId = (route) => route.road_id || route.intent_id;
  const currentFacts = (progress) => (progress.verified_facts || []).filter(f => !f.conflict
    && (!f.source_status || f.source_status === "available") && (!f.current_status || f.current_status === "effective"));
  labels.awaiting_confirmation = "等待人工确认";
  labels.assumed_achieved = "已结束 · 超时默认确定";
  labels.in_flight = "执行结果待结算";
  labels.unsettled = "未结算";
  Object.assign(labels, { active: "可规划", waiting_child: "等待后继", exhausted: "当前方向耗尽" });
  Object.assign(labels, { verified: "已验证", effective: "当前有效", superseded: "已替代", withdrawn: "已撤回",
    conflicted: "存在冲突", unavailable: "当前不可用", withheld: "正文受限", unresolved: "引用未解析" });
  const short = (value) => value ? value.slice(0, 8) : "—";
  const reviewLabels = { pending: "待审", blocked: "审核受阻", accept: "已接受", accepted: "已接受", reject: "已拒绝", rejected: "已拒绝", superseded: "已失效", revise: "要求修订" };
  const stageLabels = { stage_intent: "阶段目标提案", stage_roads: "Road 规划", stage_action: "Inner 决策", stage_review: "Outer 审核", stage_completion: "阶段完成检查", stage_continuation: "后继规划", stage_supervision: "策略监督", stage_summary: "会话摘要" };
  const node = (tag, text, className) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; };
  const empty = (container, text) => container.append(node("p", text, "run-empty"));
  const time = (value) => value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleTimeString("zh-CN", { hour12: false }) : "等待提交";
  const code = (title, value) => { const box = node("div", undefined, "run-code"); box.append(node("strong", title), node("pre", value || "（空）")); return box; };
  function detail(key, title) { const el = node("details", undefined, "run-event"); el.dataset.key = key; el.append(node("summary", title)); return el; }
  function badge(status) { const el = node("span", label(status), "run-badge"); if (Object.hasOwn(labels, status)) el.dataset.status = status; return el; }
  function reviewBadge(subject) {
    const el = node("span", `规划审核：${reviewLabels[subject.review_state] || "状态未提供"}`, "run-badge review-badge");
    el.dataset.status = subject.review_state || "unknown"; return el;
  }
  function metric(title, value) { const el = node("div", undefined, "run-metric"); el.append(node("span", title), node("strong", value)); return el; }

  const turnName = (progress, turn) => `Turn ${turn.sequence || (progress.turns || []).indexOf(turn) + 1}`;
  const roadName = (turn, road) => `Road ${turn.roads.indexOf(road) + 1}`;
  function selection(progress) {
    const turns = progress.turns || [];
    if (!filter || filter.type === "task") return { type: "task", title: "全任务" };
    const turn = turns.find(t => t.turn_id === filter.id || (t.roads || []).some(r => r.road_id === filter.id));
    if (!turn) return { type: "missing", title: "所选节点未在当前快照中" };
    const road = (turn.roads || []).find(r => r.road_id === filter.id);
    return road ? { type: "road", turn, road, title: `${turnName(progress, turn)} / ${roadName(turn, road)}` }
      : { type: "turn", turn, title: turnName(progress, turn) };
  }
  function factRefs(progress, refs) {
    const byRef = new Map((progress.verified_facts || []).filter(f => f.ref).map(f => [f.ref, f]));
    return [...new Set(refs || [])].map(ref => ({ ref, fact: byRef.get(ref) }));
  }
  const roadFacts = (progress, road) => (progress.verified_facts || []).filter(f => f.road_id === road.road_id)
    .map(fact => ({ ref: fact.ref, fact }));
  function factGroups(progress, scope) {
    if (scope.type === "task") return [{ title: "全任务 Facts", entries: (progress.verified_facts || []).map(fact => ({ ref: fact.ref, fact })) }];
    if (scope.type === "turn") return [{ title: "Turn.Fact · 精选成果", entries: factRefs(progress, scope.turn.fact_refs) }];
    if (scope.type === "road") {
      const produced = roadFacts(progress, scope.road);
      const ownRefs = new Set(produced.map(entry => entry.ref));
      return [{ title: "Road.Fact · 本路产出", entries: produced }, { title: "引用的已有 Fact",
        entries: factRefs(progress, (scope.road.support_refs || []).filter(ref => ref.startsWith("verified_fact:") && !ownRefs.has(ref))) }];
    }
    return [];
  }
  function rawRefs(title, refs, key) {
    const box = detail(key, `${title} · ${(refs || []).length} 项`);
    box.append(code("精确引用", (refs || []).join("\n")));
    return box;
  }
  function factCard(progress, entry) {
    const { ref, fact } = entry;
    const card = node("article", undefined, "run-fact"); card.dataset.key = ref || fact?.fact_id;
    if (ref) card.dataset.ref = ref;
    if (!fact) {
      card.append(badge("unresolved"), node("p", "引用版本未在当前快照中提供，暂不可解析正文。", "fact-content"),
        code("保留的精确引用", ref));
      return card;
    }
    card.dataset.factId = fact.fact_id;
    const state = fact.conflict ? "conflicted" : fact.source_status && fact.source_status !== "available"
      ? fact.source_status : fact.current_status || fact.status;
    card.append(badge(state), node("p", fact.content || "正文暂不可用。", "fact-content"));
    const turn = (progress.turns || []).find(t => t.turn_id === fact.turn_id)
      || (progress.turns || []).find(t => (t.roads || []).some(r => r.road_id === fact.road_id));
    const road = (turn?.roads || []).find(r => r.road_id === fact.road_id);
    const origin = turn ? `${turnName(progress, turn)}${road ? ` / ${roadName(turn, road)}` : fact.road_id ? ` / Road ${short(fact.road_id)}（节点未提供）` : ""}`
      : `Turn ${short(fact.turn_id)} / Road ${short(fact.road_id)}`;
    const source = node("button", `来源：${origin}`, "quiet-button fact-origin"); source.type = "button";
    source.disabled = !turn;
    source.onclick = () => select({ type: road ? "road" : "turn", id: road?.road_id || turn.turn_id });
    card.append(source);
    const metadata = detail(`fact-source/${ref || fact.fact_id}`, "来源与版本");
    metadata.append(code("Fact ID", fact.fact_id), code("产出归属", `Turn ${fact.turn_id || turn?.turn_id || "未提供"}\nRoad ${fact.road_id || "未提供"}`), code("精确版本引用", ref),
      code("原始支持来源", (fact.source_refs || []).join("\n")));
    if (fact.effective_ref && fact.effective_ref !== ref) metadata.append(code("关联的当前引用（正文未自动替换）", fact.effective_ref));
    card.append(metadata);
    return card;
  }
  function appendFacts(host, progress, scope) {
    for (const group of factGroups(progress, scope)) {
      const section = node("section", undefined, "fact-group"); section.dataset.key = group.title;
      section.append(node("h4", `${group.title} · ${group.entries.length} 条`));
      for (const entry of group.entries) section.append(factCard(progress, entry));
      if (!group.entries.length) empty(section, scope.type === "turn" ? "尚未选入阶段成果。" : "当前范围尚无记录。");
      host.append(section);
    }
  }
  function stageStatus(turn) {
    const row = node("div", undefined, "detail-states");
    row.append(node("span", turn.achievement ? "阶段达成：已确认" : "阶段达成：未确认", "run-meta"),
      node("span", `调度：${label(turn.planning_state)}`, "run-meta"));
    row.append(reviewBadge(turn));
    return row;
  }

  // Reuse keyed DOM nodes. Polls update text/attributes and append new records;
  // existing focus, selection, open details and loaded evidence stay attached.
  const domKey = el => el.nodeType === 1 ? el.dataset.key || el.id || null : null;
  function patchChildren(host, children) {
    const old = Array.from(host.childNodes);
    const keyed = new Map(old.filter(domKey).map(el => [domKey(el), el]));
    const keep = new Set();
    children.forEach((next, index) => {
      const prior = domKey(next) ? keyed.get(domKey(next)) : old[index];
      const compatible = prior && prior.nodeType === next.nodeType && prior.nodeName === next.nodeName
        && domKey(prior) === domKey(next) && !keep.has(prior);
      const el = compatible ? patchNode(prior, next) : next;
      if (host.childNodes[index] !== el) host.insertBefore(el, host.childNodes[index] || null);
      keep.add(el);
    });
    for (const el of Array.from(host.childNodes)) if (!keep.has(el)) el.remove();
  }
  function patchNode(prior, next) {
    if (next._graphEdges) return prior;
    if (next._evidenceVersion) return prior._evidenceVersion === next._evidenceVersion ? prior : next;
    if (next.nodeName === "INPUT" && next.type === "checkbox") prior.checked = next.checked;
    if (prior.isEqualNode(next)) return prior;
    if (prior.nodeType === 3) { prior.nodeValue = next.nodeValue; return prior; }
    for (const attr of Array.from(prior.attributes)) {
      const layoutStyle = attr.name === "style" && prior.matches(".graph-plane, .graph-board");
      if (attr.name !== "open" && !layoutStyle && !next.hasAttribute(attr.name)) prior.removeAttribute(attr.name);
    }
    for (const attr of Array.from(next.attributes)) {
      if (attr.name !== "open" && prior.getAttribute(attr.name) !== attr.value) prior.setAttribute(attr.name, attr.value);
    }
    if (next.onclick) prior.onclick = next.onclick;
    patchChildren(prior, Array.from(next.childNodes));
    return prior;
  }
  function patchRegion(id, build) {
    const draft = document.createDocumentFragment();
    build(draft);
    patchChildren(get(id), Array.from(draft.childNodes));
  }

  // 把「一次运行走到哪」摊平成五个阶段。只做投影，不推断成功。
  function stageStates(progress, live) {
    const turns = progress.turns || [];
    const intents = turns.flatMap(routes);
    const executions = progress.tool_operations || progress.executions || [];
    const facts = currentFacts(progress);
    const recovery = progress.recovery || {};
    const reason = String(recovery.reason || "");
    const goal = progress.goal_evaluation;
    const goalDone = goal && goal !== "not_evaluated";
    const bad = (fragment) => reason.includes(fragment);
    return [
      {
        n: 1, name: "冻结根",
        detail: progress.task_run_id ? `${progress.mode || "unknown"} · ${progress.sandbox_backend || "unknown"}` : "等待运行",
        state: live ? "done" : progress.task_run_id ? "done" : "idle",
      },
      {
        n: 2, name: progress.goal_validation ? "阶段规划与审核" : "外层规划",
        detail: turns.length ? `${turns.length} Turn · ${intents.length} ${progress.goal_validation ? "Road" : "Intent"}` : "尚未提交 Turn",
        state: turns.length ? "done" : bad("tree") || bad("planning") ? "alert" : live ? "active" : "idle",
      },
      {
        n: 3, name: "内层执行",
        detail: executions.length ? `${executions.length} 次执行${progress.goal_validation ? "" : ` · ${(progress.plans || []).length} Plan`}` : "当前路线尚无动作",
        state: executions.length ? (executions.some((item) => ["pending", "running", "in_flight"].includes(item.status)) ? "active" : "done") : live && turns.length ? "active" : "idle",
      },
      {
        n: 4, name: "取证与复核",
        detail: `${facts.length} 条 verified · ${(progress.retrospective || []).length} 次复盘`,
        state: facts.length ? "done" : live && executions.length ? "active" : bad("fact") || bad("review") ? "alert" : "idle",
      },
      {
        n: 5, name: "结论边界",
        detail: recovery.required ? reason || "需要显式恢复" : goalDone ? label(goal) : live ? "运行中，尚未判定" : label(progress.task_run?.status) || "已结束",
        state: recovery.required ? "alert" : goalDone ? "done" : "idle",
      },
    ];
  }

  function renderStages(progress, live) {
    const host = get("run-stages"); if (!host) return;
    patchChildren(host, stageStates(progress, live).map((stage) => {
      const cell = node("div", undefined, "stage");
      cell.dataset.state = stage.state;
      const top = node("div", undefined, "stage-top");
      top.append(node("span", String(stage.n), "stage-num"), node("span", stage.name, "stage-name"));
      cell.append(top, node("span", stage.detail, "stage-detail"));
      return cell;
    }));
  }

  function renderFocus(progress, run, live) {
    const host = get("run-focus"); if (!host) return;
    const executions = progress.tool_operations || progress.executions || [];
    const running = executions.find((item) => ["pending", "running", "in_flight"].includes(item.status));
    const activeIntent = (progress.turns || []).flatMap(routes).find((item) => item.status === "running");
    let state = "idle";
    let text;
    if (progress.task_run?.status === "awaiting_confirmation") { state = "warn"; text = `等待人工决定 · ${progress.goal_validation?.timeout_action === "assume" ? "五分钟无操作按默认确定结束" : "五分钟无决定则未验证结束"}`; }
    else if (running) { state = "live"; text = `执行中 · ${short(running.operation_id || running.plan_id)}`; }
    else if (live && activeIntent) { state = "live"; text = `${activeIntent.road_id ? "Road" : `Intent ${activeIntent.priority}`} · ${activeIntent.description || "（无描述）"}`; }
    else if (live) { state = "live"; text = "等待下一步规划或审核"; }
    else if (progress.recovery?.required) { state = "warn"; text = `已停止 · ${progress.recovery.reason || "需要恢复"}`; }
    else { text = `已结束 · ${label(progress.task_run?.status) || label(run?.status)}`; }
    const dot = node("span", "", "focus-dot"); dot.dataset.state = state;
    patchChildren(host, [dot, node("span", text, "focus-text")]);
  }
  function reset() { generation += 1; current = null; filter = null; cache = new Map(); validationQueries.clear(); graphZoom = null; setTab("detail"); get("run-read-error").hidden = true; }
  function render(data) {
    if (current?.run?.run_id !== data.run?.run_id) { generation += 1; cache.clear(); filter = null; }
    current = data;
    const { run, events } = data;
    const progress = data.progress || run?.result || {};
    const live = ["starting", "running", "cancelling", "cleaning"].includes(run?.status);
    get("run-dashboard").hidden = !run; get("run-raw").hidden = !run;
    const backend = progress.mode === "simulated" ? "本地模拟 · " : progress.sandbox_backend === "docker" ? "Docker · " : progress.sandbox_backend === "local_simulated" ? "真实模型／本地模拟 Sandbox · " : "";
    get("run-status").textContent = run ? `${label(run.status)} · ${backend}${progress.task_run_id || run.agent_task_run_id || run.run_id}` : "保存题目后，可开始运行。";
    const executions = progress.tool_operations || progress.executions || [];
    const activeExecution = executions.find((item) => ["pending", "running", "in_flight"].includes(item.status));
    const activeIntent = (progress.turns || []).flatMap(routes).find((item) => item.status === "running");
    let phase = label(run?.status);
    if (run?.status === "cleaning") phase = "清理 Sandbox 资源";
    else if (live && run.status !== "cancelling") phase = activeExecution ? "等待命令结果" : activeIntent ? `推进当前 ${progress.goal_validation ? "Road" : "Intent"}` : "准备运行 / 生成下一轮";
    if (progress.task_run?.status === "awaiting_confirmation") phase = "等待人工确认";
    let remaining = "—";
    if (progress.task_run?.deadline_at) {
      const seconds = Math.max(0, Math.ceil(progress.task_run.deadline_at - Date.now() / 1000));
      remaining = progress.task_run.finished_at ? "已结束" : `${Math.floor(seconds / 60)}分 ${seconds % 60}秒`;
    }
    const goal = progress.protocol_version === 3 ? `${progress.mode === "simulated" ? "模拟：" : ""}${label(progress.goal_evaluation)}` : "未验证";
    const metrics = [metric("当前阶段", phase), metric("剩余总时限", remaining), metric("当前有效 Fact", String(currentFacts(progress).length)), metric("待验证候选", String((progress.answer_candidates || progress.pending_candidates || []).length)), metric("目标评估", goal)];
    if (progress.protocol_version === 3) {
      if (progress.review_queue) metrics.push(metric("待审 / 受阻", `${progress.review_queue.filter(r => r.state === "pending").length} / ${progress.review_queue.filter(r => r.state === "blocked").length}`));
      metrics.push(metric("已提交 Checkpoint", String(progress.checkpoint?.sequence ?? "—")));
      if (progress.budget?.status === "available") metrics.push(metric("操作预算", progress.budget.total_limit == null ? `${progress.budget.total_used} 次（按 deadline）` : `${progress.budget.total_used} / ${progress.budget.total_limit}`));
    }
    renderStages(progress, live);
    renderFocus(progress, run, live);
    renderConfirmation(progress);
    get("run-stages").hidden = !run;
    get("run-summary").hidden = !run;
    get("run-summary").style.setProperty("--cols", String(metrics.length));
    get("run-summary").classList.toggle("v3", progress.protocol_version === 3);
    patchChildren(get("run-summary"), metrics);
    const raw = JSON.stringify({ ...data, challengeId: undefined }, null, 2);
    if (get("run-result").textContent !== raw) get("run-result").textContent = raw;
    draw(progress, live);
  }
  function renderConfirmation(progress) {
    const host = get("run-confirmation");
    const waiting = progress.goal_validation?.mode === "human" && progress.task_run?.status === "awaiting_confirmation";
    const assumed = progress.task_run?.status === "assumed_achieved" && progress.task_run?.reason === "human_confirmation_timeout_assumed";
    host.hidden = !waiting && !assumed;
    patchRegion("run-confirmation", panel => {
      if (assumed) {
        panel.append(node("strong", "已按超时默认确定结束"), node("p", "人工确认窗口内未收到决定；保留默认确定来源，未伪造人工或平台验证。", "run-meta"));
        return;
      }
      if (!waiting) return;
      panel.append(node("strong", "发现候选答案，请确定或指出错误"));
      const candidate = (progress.answer_candidates || []).find(item => item.candidate_id === progress.goal_validation.candidate_id);
      if (!candidate) { panel.append(node("p", "当前快照缺少待确认候选，请刷新；不会对缺失身份发送决定。", "run-meta")); return; }
      const seconds = Math.max(0, Math.ceil(progress.goal_validation.confirmation_deadline - Date.now() / 1000));
      panel.append(node("p", `人工决定剩余 ${seconds} 秒；${progress.goal_validation.timeout_action === "assume" ? "无操作按默认确定结束，保留超时来源。" : "此运行按原规则，无决定则未验证结束。"}`, "run-meta"));
      panel.append(node("p", `候选 ${candidate.candidate_id} · 来源 ${candidate.source_ref || candidate.execution_id}`, "run-ref"));
      panel.append(node("p", "候选正文通过交互终端 candidate-read 查看并自主提交；普通进度和日志仅展示引用。选择错误后 Agent 返回原规划继续探索。", "run-meta"));
      const stateDir = current.run?.directory ? `${current.run.directory}/agent-state` : "<state-dir>";
      panel.append(code("在项目交互终端查看候选", `python -m agent candidate-read --state-dir "${stateDir}" --task-run-id ${progress.task_run_id} --candidate-id ${candidate.candidate_id} --evidence-version ${candidate.evidence_version}`));
      for (const [action, text] of [["accept", "确定"], ["continue", "错误"]]) {
        const button = node("button", text, action === "accept" ? "primary-button" : "secondary-button"); button.type = "button";
        button.dataset.key = `human/${candidate.candidate_id}/${action}`;
        if (!validationQueries.has(button.dataset.key)) validationQueries.set(button.dataset.key, crypto.randomUUID());
        button.onclick = event => { event.currentTarget.disabled = true; window.dispatchEvent(new CustomEvent("stage-human-decision", { detail: { challengeId: current.challengeId, selection: { candidate_id: candidate.candidate_id, action, operation_id: validationQueries.get(button.dataset.key) } } })); };
        panel.append(button);
      }
    });
  }
  function setTab(name) {
    activeTab = name;
    for (const tab of ["detail", "evidence", "activity", "log"]) {
      const selected = tab === name;
      get(`run-tab-${tab}`).classList.toggle("active", selected);
      get(`run-tab-${tab}`).setAttribute("aria-selected", String(selected));
      get(`run-pane-${tab}`).hidden = !selected;
    }
  }
  function applyGraphScale() {
    const host = get("run-graph");
    const plane = host.firstElementChild;
    const board = plane?.firstElementChild;
    if (!board || !host.clientWidth || !host.clientHeight) return;
    const width = board.scrollWidth;
    const height = board.scrollHeight;
    const fitted = Math.min(1.25, (host.clientWidth - 24) / width, (host.clientHeight - 24) / height);
    const scale = graphZoom ?? Math.max(0.65, fitted);
    board.style.transform = `scale(${scale})`;
    plane.style.width = `${Math.ceil(width * scale)}px`;
    plane.style.height = `${Math.ceil(height * scale)}px`;
    get("graph-zoom-level").textContent = `${Math.round(scale * 100)}%`;
    get("graph-zoom-level").dataset.scale = String(scale);
  }
  function zoomGraph(delta) {
    const visible = Number(get("graph-zoom-level").dataset.scale) || 1;
    graphZoom = Math.min(1.8, Math.max(0.65, Math.round((visible + delta) * 10) / 10));
    applyGraphScale();
  }
  function draw(progress, live) {
    const opened = new Set(Array.from(get("run-dashboard").querySelectorAll("details[open]")).map((el) => el.dataset.key));
    const scrolls = Array.from(get("run-dashboard").querySelectorAll(".run-scroll")).map((el) => ({ top: el.scrollTop, left: el.scrollLeft }));
    if (progress.goal_validation) {
      get("run-scope-label").textContent = selection(progress).title;
      get("run-scope-all").hidden = !filter || filter.type === "task";
      get("run-scope-all").onclick = () => select(null, true);
    } else {
      get("run-scope-label").textContent = filter?.title || "全任务";
      get("run-scope-all").hidden = true;
    }
    renderGraph(progress);
    renderDetail(progress);
    renderTimeline(progress, opened);
    renderEvidence(progress, live);
    renderActivity(progress);
    Array.from(get("run-dashboard").querySelectorAll(".run-scroll")).forEach((el, i) => { el.scrollTop = scrolls[i].top; el.scrollLeft = scrolls[i].left; });
  }
  function select(value, keepTab = false) { filter = value; if (!keepTab) setTab("detail"); draw(current.progress || current.run?.result || {}, ["starting", "running", "cancelling", "cleaning"].includes(current.run?.status)); }
  function graphButton(title, subtitle, value, status, kind) {
    const button = node("button", undefined, `graph-node graph-${kind}`); button.type = "button";
    button.classList.toggle("selected", filter ? filter.id === value.id : value.type === "task");
    button.append(node("strong", title), node("small", subtitle), badge(status));
    button.dataset.key = `${kind}-${value.id || current.run?.run_id}`;
    button.onclick = () => select(value);
    return button;
  }
  function renderGraph(progress) {
    patchRegion("run-graph", host => {
    const plane = node("div", undefined, "graph-plane");
    const board = node("div", undefined, "graph-board");
    const wires = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    wires.classList.add("graph-wires"); wires.setAttribute("aria-hidden", "true");
    wires._graphEdges = true;
    wires.innerHTML = '<defs><marker id="graph-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7" fill="none" stroke="currentColor" stroke-width="1.5"/></marker></defs>';
    board.append(wires);
    const root = graphButton("TaskRun", progress.task_run_id || current.run?.agent_task_run_id || "运行中", { id: null, type: "task", title: "TaskRun" }, progress.task_run?.status, "task");
    board.append(root);
    const turnNodes = [];
    for (const turn of progress.turns || []) {
      const row = node("div", undefined, "graph-row");
      row.dataset.key = turn.turn_id; row.dataset.parent = turn.parent_turn_id || "";
      const index = (progress.turns || []).indexOf(turn) + 1;
      const title = `Turn ${turn.sequence || index}`;
      const turnNode = graphButton(title, turn.intent?.description || `${routes(turn).length} 个 Intent`, { id: turn.turn_id, type: "turn", title }, turn.planning_state || turn.status, "turn");
      if (turn.roads) turnNode.append(reviewBadge(turn), node("small", `${turn.achievement ? "阶段已达成" : "阶段未达成"} · ${(turn.fact_refs || []).length} 条精选成果`, "graph-outcome"));
      const intents = node("div", undefined, "graph-intents");
      const intentNodes = [];
      for (const intent of [...routes(turn)]) {
        const plans = (progress.plans || []).filter((plan) => plan.intent_id === intent.intent_id);
        const routeTitle = intent.road_id ? `Road ${routes(turn).indexOf(intent) + 1}` : `Intent ${intent.priority}`;
        const item = graphButton(routeTitle, intent.description, { id: routeId(intent), type: intent.road_id ? "road" : "intent", title: `${title} / ${routeTitle}` }, intentStatus(intent), "intent");
        if (intent.road_id) item.append(reviewBadge(intent));
        item.title = intent.road_id ? `${intent.description}\n本路产出 ${roadFacts(progress, intent).length} 条 Fact`
          : `${intent.description}\n${plans.length} 个短 Plan`;
        intents.append(item); intentNodes.push(item);
      }
      if (!intentNodes.length) empty(intents, "尚无已登记路线");
      row.append(turnNode, intents); board.append(row);
      turnNodes.push({ turn: turnNode, intents: intentNodes, identity: turn.turn_id, parent: turn.parent_turn_id });
    }
    if (!turnNodes.length) board.append(node("p", "等待首轮 Intent 提交。", "graph-empty"));
    plane.append(board); host.append(plane);
    });
    requestAnimationFrame(() => {
      const board = get("run-graph").querySelector(".graph-board");
      if (!board?.isConnected) return;
      const wires = board.querySelector(".graph-wires");
      const root = board.querySelector(".graph-task");
      const turnNodes = Array.from(board.querySelectorAll(".graph-row")).map(row => ({
        turn: row.querySelector(".graph-turn"), intents: Array.from(row.querySelectorAll(".graph-intent")),
        identity: row.dataset.key, parent: row.dataset.parent }));
      const paths = [];
      wires.setAttribute("width", String(board.scrollWidth)); wires.setAttribute("height", String(board.scrollHeight));
      const bounds = board.getBoundingClientRect();
      const unscale = bounds.width ? board.offsetWidth / bounds.width : 1;
      const point = (el, side) => { const r = el.getBoundingClientRect(); return { x: ((side === "right" ? r.right : side === "left" ? r.left : r.left + r.width / 2) - bounds.left) * unscale, y: ((side === "top" ? r.top : side === "bottom" ? r.bottom : r.top + r.height / 2) - bounds.top) * unscale }; };
      const connect = (from, to, kind) => {
        const a = point(from, kind === "sequence" ? "bottom" : "right");
        const b = point(to, kind === "sequence" ? "top" : "left");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        const bend = kind === "sequence" ? `M${a.x} ${a.y} L${a.x} ${b.y - 12} L${b.x} ${b.y - 12} L${b.x} ${b.y}` : `M${a.x} ${a.y} C${a.x + 38} ${a.y} ${b.x - 38} ${b.y} ${b.x} ${b.y}`;
        path.dataset.key = `${kind}/${from.dataset.key}/${to.dataset.key}`;
        path.setAttribute("d", bend); path.setAttribute("class", kind === "sequence" ? "graph-edge sequence" : "graph-edge"); path.setAttribute("marker-end", "url(#graph-arrow)"); paths.push(path);
      };
      turnNodes.forEach((group, index) => {
        const parent = progress.goal_validation ? turnNodes.find(t => t.identity === group.parent)?.turn : index ? turnNodes[index - 1].turn : null;
        connect(parent || root, group.turn, parent ? "sequence" : "owner");
        group.intents.forEach((intent) => connect(group.turn, intent, "owner"));
      });
      patchChildren(wires, [wires.firstElementChild, ...paths]);
      applyGraphScale();
    });
  }
  function renderDetail(progress) {
    patchRegion("run-detail", host => {
    const turns = progress.turns || [];
    if (progress.goal_validation) {
      const scope = selection(progress);
      host.append(node("strong", scope.type === "task" ? "TaskRun · 任务总览" : scope.title, "detail-title"));
      if (scope.type === "missing") { empty(host, "保留了节点选择；当前快照未提供该节点，请查看全任务或刷新。"); return; }
      if (scope.type === "task") {
        host.append(code("任务总目标", progress.task_run?.goal || "当前快照未提供任务总目标。"),
          node("p", `目标评估：${label(progress.goal_evaluation)} · ${turns.length} 个阶段 · ${turns.flatMap(routes).length} 条 Road`, "run-meta"));
        for (const turn of turns) {
          const item = node("article", undefined, "stage-overview"); item.dataset.key = turn.turn_id;
          const button = node("button", `${turnName(progress, turn)} · ${turn.intent.description}`, "scope-link"); button.type = "button";
          button.onclick = () => select({ type: "turn", id: turn.turn_id });
          const parent = turns.find(t => t.turn_id === turn.parent_turn_id);
          item.append(button, stageStatus(turn), node("p", `${parent ? `父阶段：${turnName(progress, parent)} · ` : "根阶段 · "}${turn.roads.length} 条 Road · ${(turn.fact_refs || []).length} 条精选成果`, "run-meta"));
          host.append(item);
        }
        if (!turns.length) empty(host, "等待首个阶段登记。");
        return;
      }
      const { turn, road } = scope;
      if (road) {
        host.append(badge(road.status), reviewBadge(road), code("Road · 解题思路", road.description));
        const parent = node("button", `所属 ${turnName(progress, turn)} · ${turn.intent.description}`, "scope-link"); parent.type = "button";
        parent.onclick = () => select({ type: "turn", id: turn.turn_id }); host.append(parent);
        host.append(rawRefs("Road 前提来源", road.support_refs, `road-premises/${road.road_id}`));
      } else {
        host.append(stageStatus(turn), code("Turn.Intent · 阶段目标", turn.intent.description), code("阶段完成依据", turn.intent.completion_criterion));
        if (turn.achievement) host.append(code("已登记达成说明", turn.achievement.completion.reason));
        host.append(rawRefs("阶段前提来源", turn.intent.support_refs, `turn-premises/${turn.turn_id}`));
        host.append(node("h4", "本阶段 Roads"));
        for (const candidate of turn.roads) {
          const item = node("article", undefined, "stage-overview"); item.dataset.key = candidate.road_id;
          const button = node("button", `${roadName(turn, candidate)} · ${candidate.description}`, "scope-link"); button.type = "button";
          button.onclick = () => select({ type: "road", id: candidate.road_id });
          item.append(button, badge(candidate.status), reviewBadge(candidate), node("p", `本路产出 ${roadFacts(progress, candidate).length} 条 Fact`, "run-meta"));
          host.append(item);
        }
      }
      appendFacts(host, progress, scope);
      return;
    }
    const selectedTurn = turns.find((turn) => turn.turn_id === filter?.id);
    const intents = turns.flatMap((turn) => turn.intents.map((intent) => ({ ...intent, turn })));
    const selectedIntent = intents.find((intent) => intent.intent_id === filter?.id)
      || (filter ? null : intents.find((intent) => intent.status === "running") || intents.at(-1));
    if (selectedIntent) {
      const plans = (progress.plans || []).filter((plan) => plan.intent_id === selectedIntent.intent_id);
      const planIds = new Set(plans.map((plan) => plan.plan_id));
      const executions = (progress.executions || []).filter((execution) => planIds.has(execution.plan_id));
      const executionIds = new Set(executions.map((execution) => execution.execution_id));
      const facts = (progress.verified_facts || []).filter((fact) => executionIds.has(fact.source_execution_id || fact.source_ref));
      host.append(node("strong", `Turn ${selectedIntent.turn.sequence} / Intent ${selectedIntent.priority}`, "detail-title"), badge(intentStatus(selectedIntent)), node("p", selectedIntent.description, "detail-description"));
      host.append(node("p", `${plans.length} 个短 Plan · ${executions.length} 次执行 · ${facts.length} 条已验证观察`, "run-meta"));
      for (const plan of plans) {
        const item = detail(`detail-${plan.plan_id}`, `Plan ${short(plan.plan_id)} · ${label(plan.status)}`);
        for (const condition of plan.conditions || []) item.append(node("p", condition.literal !== undefined ? `${condition.stream} 包含：${condition.literal}` : `${condition.path} SHA-256 = ${condition.expected_sha256}`, "run-condition"));
        host.append(item);
      }
      host.append(node("p", selectedIntent.intent_id, "run-ref"));
    } else if (selectedTurn) {
      host.append(node("strong", `Turn ${selectedTurn.sequence}`, "detail-title"), badge(selectedTurn.status), node("p", `${selectedTurn.intents.length} 个已提交 Intent`, "run-meta"), node("p", selectedTurn.turn_id, "run-ref"));
    } else {
      host.append(node("strong", "TaskRun", "detail-title"), badge(progress.task_run?.status), node("p", `${turns.length} 轮已提交`, "run-meta"));
    }
    if (progress.protocol_version === 3) {
      const binding = detail("v3-binding", "已提交运行信息");
      binding.append(node("p", `Checkpoint ${progress.checkpoint?.sequence ?? "—"} · 事件水位 ${progress.checkpoint?.watermark_event_sequence ?? "—"}`, "run-meta"));
      if (progress.budget?.status === "available") binding.append(code("分类预算", JSON.stringify(progress.budget.category_counts, null, 2)));
      for (const imported of progress.imports || []) binding.append(node("p", `${imported.name} · ${imported.byte_size} B · ${imported.status}`, "run-ref"));
      host.append(binding);
      for (const review of progress.retrospective || []) {
        const item = detail(`review-${review.review_id}`, `复盘 · ${review.scope} · ${review.decision}`);
        item.append(node("p", review.review_id, "run-ref"));
        if (review.paused) item.append(node("p", review.pause_reason, "run-condition"));
        host.append(item);
      }
      for (const fact of progress.process_facts || []) host.append(node("p", `Process_Fact ${fact.process_fact_id} · v${fact.version} · ${fact.source_count} 项来源 · ${fact.conflict_count} 项冲突`, "run-ref"));
    }
    });
  }
  function activityFits(scope, item) {
    if (scope.type === "task") return true;
    if (scope.type === "missing") return false;
    const targets = item.targets || [item];
    return targets.some(target => target.turn_id === scope.turn?.turn_id && (!scope.road
      || target.road_id === scope.road.road_id || item.kind === "turn_intent" && !target.road_id));
  }
  function activityLocation(progress, target) {
    const turn = (progress.turns || []).find(turn => turn.turn_id === target.turn_id);
    const road = (turn?.roads || []).find(road => road.road_id === target.road_id);
    return `${turn ? turnName(progress, turn) : target.turn_id ? `Turn ${short(target.turn_id)}` : "目标未登记"}${target.road_id ? ` / ${road ? roadName(turn, road) : `Road ${short(target.road_id)}`}` : ""}`;
  }
  function renderActivity(progress) {
    const scope = progress.goal_validation ? selection(progress) : { type: "task" };
    const calls = new Map([...(progress.inner_activity || []), ...(progress.retrospective || [])].map(call => [call.operation_id, call]));
    const kinds = { turn_intent: "Turn.Intent", roads: "Road 规划", fact_candidates: "Fact 提案", turn_completion: "Turn 收尾", road_resolution: "Road 收尾", conclusion_change: "结论修订", exhausted: "方向耗尽", return_to_parent: "返回父规划", request_final_validation: "最终验证申请", select_road: "选路" };
    patchRegion("run-review-queue", host => {
      for (const review of progress.review_queue || []) {
        if (!activityFits(scope, review)) continue;
        const last = calls.get(review.attempt_ids?.at(-1));
        const decisions = review.candidates?.map(candidate => candidate.decision) || [];
        const accepted = decisions.filter(decision => decision === "accept").length;
        const outcome = accepted && accepted < decisions.length ? "部分接受" : reviewLabels[review.decision] || label(review.decision);
        const state = review.state === "settled" ? `已结算 · ${outcome}` : review.state === "blocked" ? "审核受阻" : review.state === "superseded" ? "提案已失效" :
          !last ? "排队待审" : last.status === "unsettled" ? "审核请求已登记 · 未结算" : last.status === "completed" ? "结果已保存 · 待应用" : "最近尝试失败 · 待核查或纠正";
        const item = detail(`review-proposal/${review.proposal_operation_id}`, `${kinds[review.kind] || review.kind || "审核提案"} · ${state}`);
        item.append(node("p", `审核对象：${(review.targets || []).map(target => activityLocation(progress, target)).join("、") || "归属未提供"}`, "run-meta"));
        const authorRoad = review.author?.scope === "road_execution";
        const authorTurn = (progress.turns || []).find(turn => turn.turn_id === review.author?.owner_id || (turn.roads || []).some(road => road.road_id === review.author?.owner_id));
        const author = node("button", `提案作者：${authorTurn ? activityLocation(progress, { turn_id: authorTurn.turn_id, road_id: authorRoad ? review.author.owner_id : null }) : short(review.author?.owner_id)} · ${authorRoad ? "执行会话" : "规划会话"}`, "scope-link");
        author.type = "button"; author.disabled = !authorTurn;
        author.onclick = () => select({ type: authorRoad ? "road" : "turn", id: authorRoad ? review.author.owner_id : authorTurn.turn_id }, true);
        item.append(author);
        if (review.reason) item.append(code("审核意见", review.reason));
        for (const candidate of review.candidates || []) {
          const card = node("article", undefined, "review-candidate"); card.dataset.key = `${review.proposal_operation_id}/${candidate.index ?? "single"}`;
          card.append(node("strong", `${candidate.inferred ? "推断提案" : "Fact 提案"}${candidate.index == null ? "" : ` ${candidate.index + 1}`} · ${reviewLabels[candidate.decision] || (review.state === "superseded" ? "已失效" : "未正式接受")}`), node("p", candidate.content));
          if (candidate.reason) card.append(node("p", candidate.reason, "run-meta"));
          card.append(rawRefs("逐项来源", candidate.support_refs, `claim-refs/${review.proposal_operation_id}/${candidate.index}`));
          if (candidate.fact_id) card.append(code("已登记 Fact ID", candidate.fact_id));
          else if (candidate.inferred && candidate.decision === "accept") card.append(node("p", "已审核推断，仍不是正式 Fact。", "run-meta"));
          item.append(card);
        }
        if (review.suggested_indices?.length) item.append(node("p", `Outer 建议顺序（原候选序号）：${review.suggested_indices.map(index => index + 1).join(" → ")}；实际调度由原 Inner 决定。`, "run-meta"));
        const refs = detail(`review-identities/${review.proposal_operation_id}`, "提案与审核记录");
        refs.append(code("原提案", review.proposal_operation_id));
        if (review.review_operation_id) refs.append(code(review.state === "superseded" ? "使提案失效的审核" : "结算审核", review.review_operation_id));
        for (const identity of review.attempt_ids || []) {
          const call = calls.get(identity);
          refs.append(node("p", `${identity}\n${call ? `${time(call.started_at)} · ${call.status === "unsettled" ? "未结算；存活未确认" : label(call.status)}${call.finished_at == null ? "" : ` · 结束 ${time(call.finished_at)}`}` : "当前快照未提供调用记录"}`, "run-ref"));
        }
        item.append(refs); host.append(item);
      }
      if (!host.children.length) empty(host, progress.review_queue ? "当前范围暂无审核提案。" : "当前快照未提供审核队列。");
    });
    patchRegion("run-branches", host => {
      for (const branch of progress.branches || []) {
        if (!activityFits(scope, branch)) continue;
        const kind = { inheritance: "规划前缀派生", continuation: "提交后继续", correction: "审核纠正" }[branch.kind] || branch.kind;
        const item = detail(`branch/${branch.branch_id}`, `${activityLocation(progress, branch)} · ${kind} · ${branch.current ? "该会话当前前缀" : "保留的前缀"}`);
        item.append(code("分支 ID", branch.branch_id), code("原作者提案", branch.proposal_operation_id));
        if (branch.review_operation_id) item.append(code("纠正审核", branch.review_operation_id));
        if (branch.parent_branch_id) {
          const parent = (progress.branches || []).find(parent => parent.branch_id === branch.parent_branch_id);
          const button = node("button", `上游前缀：${short(branch.parent_branch_id)}${parent ? ` · ${activityLocation(progress, parent)}` : "（当前快照未提供）"}`, "scope-link");
          button.type = "button"; button.disabled = !parent;
          button.onclick = () => {
            select({ type: parent.road_id ? "road" : "turn", id: parent.road_id || parent.turn_id }, true);
            const target = Array.from(get("run-branches").querySelectorAll("details")).find(el => el.dataset.key === `branch/${parent.branch_id}`);
            if (target) { target.open = true; target.scrollIntoView({ block: "nearest" }); }
          };
          item.append(button);
        }
        host.append(item);
      }
      if (!host.children.length) empty(host, progress.branches ? "当前范围暂无分支记录。" : "当前快照未提供分支记录。");
    });
    patchRegion("run-discarded", host => {
      for (const call of progress.inner_activity || []) {
        if (!activityFits(scope, call) || call.disposition !== "discarded" && !(progress.model_failures || []).some(failure => failure.operation_id === call.operation_id && failure.code === "stage.generation_superseded")) continue;
        const item = detail(`unused/${call.operation_id}`, `${stageLabels[call.stage] || call.stage} · 生成未采用`);
        item.append(code("原操作", call.operation_id), node("p", "不代表工具执行；已派发工具仍沿原身份结算。", "run-meta"));
        if (call.disposition_ref) item.append(code("关联审核", call.disposition_ref));
        host.append(item);
      }
      if (!host.children.length) empty(host, "当前范围暂无未采用的生成记录。");
    });
  }
  function renderTimeline(progress, opened) {
    patchRegion("run-timeline", timeline => {
    get("timeline-scope").textContent = progress.goal_validation ? selection(progress).title : filter?.title || "全部已提交记录";
    get("clear-run-filter").hidden = progress.goal_validation ? selection(progress).type === "task" : !filter;
    get("clear-run-filter").onclick = () => select(null, true);
    if (progress.goal_validation) {
      const scope = selection(progress);
      if (scope.type === "missing") { empty(timeline, "当前快照未提供所选节点的记录。"); return; }
      for (const op of progress.tool_operations || []) {
        if (scope.road && op.road_id !== scope.road.road_id || scope.type === "turn" && !scope.turn.roads.some(r => r.road_id === op.road_id)) continue;
        const item = detail(`tool-${op.operation_id}`, `工具 · ${label(op.status)} · Road ${short(op.road_id)}`);
        item.append(node("p", op.operation_id, "run-ref")); timeline.append(item);
      }
      for (const op of progress.retrospective || []) {
        if (scope.road && op.road_id !== scope.road.road_id || scope.type === "turn" && op.turn_id !== scope.turn.turn_id) continue;
        const item = detail(`review-${op.operation_id}`, `${op.phase} · ${label(op.status)}`);
        item.append(node("p", op.operation_id, "run-ref")); timeline.append(item);
      }
      if (scope.type !== "task" && (progress.retrospective || []).some(op => !op.turn_id)) {
        empty(timeline, "部分审核记录未提供归属，可在全任务视图查看。");
      }
      for (const el of timeline.querySelectorAll("details")) el.open = opened.has(el.dataset.key);
      if (!timeline.children.length) empty(timeline, "尚无已提交操作。");
      return;
    }
    const plans = new Map((progress.plans || []).map((item) => [item.plan_id, item]));
    const intentTurns = new Map((progress.turns || []).flatMap((turn) => turn.intents.map((intent) => [intent.intent_id, turn.turn_id])));
    const fits = (planId, intentId) => !filter || filter.type === "task" || (filter.type === "plan" ? planId === filter.id : filter.type === "turn" ? intentTurns.get(intentId || plans.get(planId)?.intent_id) === filter.id : (intentId || plans.get(planId)?.intent_id) === filter.id);
    const entries = [];
    for (const action of progress.actions || []) {
      if (filter?.type === "plan" || !fits(null, action.intent_id)) continue;
      const item = detail(`action-${action.model_request_id}`, `${time(action.started_at)} · ${actionLabels[action.kind] || action.kind}`);
      item.append(node("p", `Intent ${short(action.intent_id)} · ${action.applied_at ? "已应用" : action.finished_at ? "结果已记录，尚未应用" : "调用尚未完成"}`, "run-meta"));
      item.append(node("p", action.model_request_id, "run-ref"));
      entries.push({ at: action.started_at, el: item });
    }
    for (const [index, execution] of (progress.executions || []).entries()) {
      if (!fits(execution.plan_id)) continue;
      const stamp = execution.started_at ? time(execution.started_at) : `执行 #${index + 1}`;
      const outcome = execution.status === "succeeded" ? "执行已结束" : label(execution.status);
      const item = detail(`execution-${execution.execution_id}`, `${stamp} · ${progress.protocol_version === 3 ? "受控动作" : "Bash"} · ${outcome}${execution.exit_code !== undefined && execution.exit_code !== null ? ` · exit ${execution.exit_code}` : ""}`);
      item._evidenceVersion = JSON.stringify(execution);
      item.append(node("p", `Plan ${short(execution.plan_id)} · step ${execution.step_index} · ${execution.execution_id}`, "run-ref"));
      const body = node("div"); item.append(body);
      const key = `${execution.execution_id}:${execution.status}`;
      const load = async () => {
        if (!item.isConnected || !item.open || body.dataset.loaded) return;
        if (progress.protocol_version === 3) {
          body.dataset.loaded = "true";
          body.replaceChildren(code("执行引用", JSON.stringify(execution, null, 2)), node("p", "正文：受保护", "run-meta"));
          return;
        }
        body.dataset.loaded = "true"; empty(body, "读取命令证据…");
        const captured = generation;
        try {
          let value = cache.get(key);
          if (!value) {
            const response = await fetch(`/api/challenges/${current.challengeId}/executions/${execution.execution_id}`);
            value = await response.json(); if (!response.ok) throw new Error(value.error || `request_${response.status}`);
            if (captured !== generation) return; cache.set(key, value);
          }
          if (captured !== generation) return;
          body.replaceChildren(node("p", `${value.working_directory} · 有效上限 ${value.effective_timeout_seconds} 秒`, "run-meta"), code("脚本", value.script));
          if (value.output) {
            const output = value.output;
            body.append(node("p", `未验证的原始观察 · ${output.dispatched ? "已派发" : "未派发"} · ${output.termination_confirmed ? "停止已确认" : "停止未确认"}${output.stream_complete ? "" : " · 输出不完整"}`, "run-meta"));
            body.append(code(`stdout${output.stream_complete && output.stdout_truncated ? " · 已截断" : ""}`, output.stdout), code(`stderr${output.stream_complete && output.stderr_truncated ? " · 已截断" : ""}`, output.stderr));
          } else empty(body, "尚无已提交输出。");
        } catch (error) { body.replaceChildren(node("p", `证据读取失败：${error.message}`, "run-read-error")); delete body.dataset.loaded; }
      };
      item.addEventListener("toggle", load);
      if (opened.has(item.dataset.key)) { item.open = true; load(); }
      entries.push({ at: execution.started_at ?? (Number.MIN_SAFE_INTEGER + index), el: item });
    }
    if (!filter || filter.type === "task") for (const event of current.events || []) {
      const item = detail(`event-${event.sequence}`, `${time(event.timestamp)} · ${event.details?.operation || event.type} · ${label(event.status)}`);
      item.append(code(`已提交事件 #${event.sequence}`, JSON.stringify(event.details, null, 2)));
      entries.push({ at: typeof event.timestamp === "number" ? event.timestamp : Date.parse(event.timestamp) / 1000, el: item });
    }
    entries.sort((a, b) => a.at - b.at);
    for (const entry of entries) { if (opened.has(entry.el.dataset.key)) entry.el.open = true; timeline.append(entry.el); }
    if (!entries.length) empty(timeline, "暂无已提交的动作或执行记录。");
    });
  }
  function renderEvidence(progress, live) {
    const stop = document.createDocumentFragment();
    const historicalUnknown = progress.recovery?.reason === "execution_outcome_unknown" || current.run?.result?.recovery?.reason === "execution_outcome_unknown";
    if (progress.recovery?.reason === "state_or_evidence_unreadable") stop.append(node("strong", "状态或证据读取失败，当前视图无法确认"));
    else if (live) stop.append(node("strong", "运行中，持续读取已提交状态"));
    else if (current.run?.status === "paused") stop.append(node("strong", "已暂停，Sandbox 保留"));
    else if (current.run?.status === "cleanup_confirmed" && historicalUnknown) stop.append(node("strong", "任务已结束并清理 · 历史 Execution 结果仍未知"));
    else if (current.run?.status === "recovery_required" || progress.recovery?.required) stop.append(node("strong", "需要显式恢复 · 未自动重新执行"));
    else stop.append(node("strong", label(current.run?.status)));
    if (!live && progress.stop) stop.append(node("p", `${progress.stop.reason || progress.stop.kind || "调度已停止"}`, "run-condition"), code("停止记录", JSON.stringify(progress.stop, null, 2)));
    const recoveryMessages = {
      d8_local_goal_inconclusive: "当前 Intent 的完成证据不足，已暂停；不是操作结果未知。",
      d8_intent_evaluation_reason_too_long: "局部目标审核 reason 超过当次请求的长度上限，已暂停。",
      d8_model_format_rejected: "模型输出格式被确定拒绝，已暂停；详见格式诊断。",
      d8_review_call_failed: "审核调用失败，已暂停；不是操作结果未知。",
    };
    if (!live && progress.recovery?.required) stop.append(node("p", recoveryMessages[progress.recovery.reason] || progress.recovery.reason, "run-condition"));
    if (progress.review_call_diagnostics?.length) stop.append(code("审核调用诊断", JSON.stringify(progress.review_call_diagnostics, null, 2)));
    if (progress.model_format_diagnostics?.length) stop.append(code("模型格式诊断（含已纠正的历史记录）", JSON.stringify(progress.model_format_diagnostics, null, 2)));
    if (!live) stop.append(node("p", `清理：${progress.cleanup?.status || "未确认"}`, "run-meta"));
    for (const error of [...(progress.errors || []), ...(current.run?.result?.errors || [])]) stop.append(node("p", error.code || error.category, "run-read-error"));
    patchChildren(get("run-stop"), Array.from(stop.childNodes));
    const facts = document.createDocumentFragment();
    if (progress.goal_validation) {
      const scope = selection(progress);
      get("run-facts-heading").textContent = `${scope.title} · 成果`;
      appendFacts(facts, progress, scope);
    } else for (const fact of progress.verified_facts || []) {
      const item = node("article", undefined, "run-fact");
      item.dataset.key = fact.fact_id;
      if (progress.protocol_version === 3) item.append(node("p", `当前适用性：${label(fact.current_status || fact.status)}`));
      item.append(node("p", fact.content || "正文暂不可用，请刷新运行状态。"), node("p", `Fact ${fact.fact_id}\n${fact.road_id ? `Road ${fact.road_id}\n${(fact.source_refs || []).join("\n")}` : `Execution ${fact.source_execution_id || fact.source_ref}`}`, "run-ref"));
      facts.append(item);
    }
    if (!progress.goal_validation) get("run-facts-heading").textContent = "已验证观察 · Facts";
    if (!facts.children.length) empty(facts, "当前范围尚无可展示的成果。");
    patchChildren(get("run-facts"), Array.from(facts.childNodes));
    const hints = (progress.user_hints || []).map(hint => {
      const item = node("article", undefined, "run-fact"); item.dataset.key = hint.operation_id;
      item.append(node("p", hint.content), node("p", `用户提示 · 待验证 · ${time(hint.created_at)}`, "run-meta"));
      return item;
    });
    if (get("run-user-hints")) patchChildren(get("run-user-hints"), hints.length ? hints : [node("p", "尚无用户提示。", "run-empty")]);
    const candidates = document.createDocumentFragment();
    get("run-candidate-heading").textContent = progress.protocol_version === 3 ? "任务答案候选 · Candidates" : "待验证候选 · Pending";
    if (progress.protocol_version === 3) {
      for (const candidate of progress.answer_candidates || []) {
        const item = node("article", undefined, "run-candidate");
        item.dataset.key = candidate.candidate_id;
        item.append(node("strong", candidate.validation_state === "partial" ? "候选正确 · 整题未完成" : label(candidate.validation_state)), node("p", `候选 ${candidate.candidate_id}\n证据版本 ${candidate.evidence_version}\n来源 ${candidate.source_ref || candidate.execution_id}`, "run-ref"));
        if ([19, 20, 21, 22].includes(progress.contract_version) && candidate.validation_state === 'pending') item.append(node('p', '等待可信验证反馈；候选尚未被确认。', 'run-meta'));
        if (progress.competition && !live && candidate.validation_state === "pending") {
          const button=node("button","提交此候选到比赛平台","secondary-button");button.type="button";
          button.onclick=event=>{event.currentTarget.disabled=true;window.dispatchEvent(new CustomEvent("competition-submit",{detail:{challengeId:current.challengeId}}));};
          item.append(button);
        }
        for (const feedback of candidate.feedback || []) if (feedback.correct_flag_count !== undefined) item.append(node("p",`平台 Flag 进度：${feedback.correct_flag_count} / ${feedback.total_flag_count}`,"run-meta"));
        for (const feedback of candidate.feedback || []) item.append(node("p", `${progress.mode === "simulated" ? "模拟反馈" : "反馈"}：${label(feedback.verdict)} · ${feedback.operation_id}`, "run-ref"));
        if ([19, 20, 21, 22].includes(progress.contract_version) && !live && candidate.checkable_submission_ids?.length) {
          const submission = candidate.checkable_submission_ids[0];
          const pending = (progress.external_validation_checks || []).find((check) => check.candidate_id === candidate.candidate_id && check.evidence_version === candidate.evidence_version && check.submission_operation_id === submission && check.status === "started");
          const key = `${progress.task_run_id}/${candidate.candidate_id}/${candidate.evidence_version}/${submission}`;
          const cached = validationQueries.get(key);
          if ((progress.external_validation_checks || []).some((check) => check.query_id === cached && check.status === "completed")) validationQueries.delete(key);
          const button = node("button", pending ? "继续核查原请求" : "核查反馈", "secondary-button");
          button.type = "button";
          button.onclick = event => {
            event.currentTarget.disabled = true;
            const query = pending?.query_id || validationQueries.get(key) || crypto.randomUUID();
            validationQueries.set(key, query);
            window.dispatchEvent(new CustomEvent("d9-validation-check", { detail: { challengeId: current.challengeId, selection: { candidate_id: candidate.candidate_id, evidence_version: candidate.evidence_version, submission_operation_id: submission, query_id: query } } }));
          };
          item.append(button);
        }
        candidates.append(item);
      }
    } else {
      for (const candidate of progress.pending_candidates || []) { const item = node("article", undefined, "run-candidate"); item.append(node("pre", candidate.value), node("p", `未验证 · 来源 ${candidate.source_execution_ids.join(", ")}`, "run-ref")); candidates.append(item); }
    }
    if (!candidates.children.length) empty(candidates, "尚无待验证候选。");
    patchChildren(get("run-candidates"), Array.from(candidates.childNodes));
    const failures = document.createDocumentFragment();
    for (const failure of progress.failure_summaries || []) { const item = node("article", undefined, "run-failure"); item.dataset.key = failure.evidence_ref; item.append(node("strong", failure.failure_type), node("p", failure.short_reason), node("p", failure.evidence_ref, "run-ref")); failures.append(item); }
    if (!failures.children.length) empty(failures, "尚无已提交失败摘要。");
    patchChildren(get("run-failures"), Array.from(failures.childNodes));
  }
  document.addEventListener("DOMContentLoaded", () => {
    for (const tab of ["detail", "evidence", "activity", "log"]) get(`run-tab-${tab}`).addEventListener("click", () => setTab(tab));
    get("graph-zoom-out").addEventListener("click", () => zoomGraph(-0.1));
    get("graph-zoom-in").addEventListener("click", () => zoomGraph(0.1));
    get("graph-fit").addEventListener("click", () => { graphZoom = null; applyGraphScale(); });
    const graph = get("run-graph");
    let drag = null;
    graph.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest?.(".graph-node")) return;
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY, left: graph.scrollLeft, top: graph.scrollTop };
      graph.setPointerCapture(event.pointerId);
      graph.classList.add("dragging");
    });
    graph.addEventListener("pointermove", (event) => {
      if (!drag || drag.id !== event.pointerId) return;
      graph.scrollLeft = drag.left - (event.clientX - drag.x);
      graph.scrollTop = drag.top - (event.clientY - drag.y);
    });
    const endDrag = (event) => { if (drag?.id === event.pointerId) { drag = null; graph.classList.remove("dragging"); } };
    graph.addEventListener("pointerup", endDrag);
    graph.addEventListener("pointercancel", endDrag);
    window.addEventListener("resize", () => { if (graphZoom === null) applyGraphScale(); });
  });
  return { render, reset, patchChildren };
})();
