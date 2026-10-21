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
  const label = (value) => labels[value] || value || "—";
  const short = (value) => value ? value.slice(0, 8) : "—";
  const node = (tag, text, className) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; };
  const empty = (container, text) => container.append(node("p", text, "run-empty"));
  const time = (value) => value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleTimeString("zh-CN", { hour12: false }) : "等待提交";
  const code = (title, value) => { const box = node("div", undefined, "run-code"); box.append(node("strong", title), node("pre", value || "（空）")); return box; };
  function detail(key, title) { const el = node("details", undefined, "run-event"); el.dataset.key = key; el.append(node("summary", title)); return el; }
  function badge(status) { const el = node("span", label(status), "run-badge"); if (Object.hasOwn(labels, status)) el.dataset.status = status; return el; }
  function metric(title, value) { const el = node("div", undefined, "run-metric"); el.append(node("span", title), node("strong", value)); return el; }

  // 把「一次运行走到哪」摊平成五个阶段。只做投影，不推断成功。
  function stageStates(progress, live) {
    const turns = progress.turns || [];
    const intents = turns.flatMap((turn) => turn.intents || []);
    const executions = progress.executions || [];
    const facts = progress.verified_facts || [];
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
        n: 2, name: "外层规划",
        detail: turns.length ? `${turns.length} Turn · ${intents.length} Intent` : "尚未提交 Turn",
        state: turns.length ? "done" : bad("tree") || bad("planning") ? "alert" : live ? "active" : "idle",
      },
      {
        n: 3, name: "内层执行",
        detail: executions.length ? `${executions.length} 次执行 · ${(progress.plans || []).length} Plan` : "当前 Intent 尚无动作",
        state: executions.length ? (executions.some((item) => ["pending", "running"].includes(item.status)) ? "active" : "done") : live && turns.length ? "active" : "idle",
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
    host.replaceChildren(...stageStates(progress, live).map((stage) => {
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
    host.replaceChildren();
    const executions = progress.executions || [];
    const running = executions.find((item) => ["pending", "running"].includes(item.status));
    const activeIntent = (progress.turns || []).flatMap((turn) => turn.intents || []).find((item) => item.status === "running");
    let state = "idle";
    let text;
    if (running) { state = "live"; text = `执行中 · step ${running.step_index ?? "—"} · ${short(running.plan_id)}`; }
    else if (live && activeIntent) { state = "live"; text = `Intent ${activeIntent.priority} · ${activeIntent.description || "（无描述）"}`; }
    else if (live) { state = "live"; text = "等待外层下一步决策"; }
    else if (progress.recovery?.required) { state = "warn"; text = `已停止 · ${progress.recovery.reason || "需要恢复"}`; }
    else { text = `已结束 · ${label(progress.task_run?.status) || label(run?.status)}`; }
    host.append(node("span", "", "focus-dot"), node("span", text, "focus-text"));
    host.firstChild.dataset.state = state;
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
    const executions = progress.executions || [];
    const activeExecution = executions.find((item) => ["pending", "running"].includes(item.status));
    const activeIntent = (progress.turns || []).flatMap((turn) => turn.intents).find((item) => item.status === "running");
    let phase = label(run?.status);
    if (run?.status === "cleaning") phase = "清理 Sandbox 资源";
    else if (live && run.status !== "cancelling") phase = activeExecution ? "等待命令结果" : activeIntent ? "推进当前 Intent" : "准备运行 / 生成下一轮";
    let remaining = "—";
    if (progress.task_run?.deadline_at) {
      const seconds = Math.max(0, Math.ceil(progress.task_run.deadline_at - Date.now() / 1000));
      remaining = progress.task_run.finished_at ? "已结束" : `${Math.floor(seconds / 60)}分 ${seconds % 60}秒`;
    }
    const goal = progress.protocol_version === 3 ? `${progress.mode === "simulated" ? "模拟：" : ""}${label(progress.goal_evaluation)}` : "未验证";
    const metrics = [metric("当前阶段", phase), metric("剩余总时限", remaining), metric("已验证 Fact", String(progress.verified_facts?.length || 0)), metric("待验证候选", String(progress.pending_candidates?.length || 0)), metric("目标评估", goal)];
    if (progress.protocol_version === 3) {
      metrics.push(metric("已提交 Checkpoint", String(progress.checkpoint?.sequence ?? "—")));
      if (progress.budget?.status === "available") metrics.push(metric("操作预算", progress.budget.total_limit == null ? `${progress.budget.total_used} 次（按 deadline）` : `${progress.budget.total_used} / ${progress.budget.total_limit}`));
    }
    renderStages(progress, live);
    renderFocus(progress, run, live);
    get("run-stages").hidden = !run;
    get("run-summary").hidden = !run;
    get("run-summary").style.setProperty("--cols", String(metrics.length));
    get("run-summary").classList.toggle("v3", progress.protocol_version === 3);
    get("run-summary").replaceChildren(...metrics);
    get("run-result").textContent = JSON.stringify({ ...data, challengeId: undefined }, null, 2);
    draw(progress, live);
  }
  function setTab(name) {
    activeTab = name;
    for (const tab of ["detail", "evidence", "log"]) {
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
    renderGraph(progress);
    renderDetail(progress);
    renderTimeline(progress, opened);
    renderEvidence(progress, live);
    Array.from(get("run-dashboard").querySelectorAll(".run-scroll")).forEach((el, i) => { el.scrollTop = scrolls[i].top; el.scrollLeft = scrolls[i].left; });
  }
  function select(value) { filter = value; setTab("detail"); draw(current.progress || current.run?.result || {}, ["starting", "running", "cancelling", "cleaning"].includes(current.run?.status)); }
  function graphButton(title, subtitle, value, status, kind) {
    const button = node("button", undefined, `graph-node graph-${kind}`); button.type = "button";
    button.classList.toggle("selected", filter?.id === value.id);
    button.append(node("strong", title), node("small", subtitle), badge(status));
    button.addEventListener("click", () => select(value));
    return button;
  }
  function renderGraph(progress) {
    const host = get("run-graph"); host.replaceChildren();
    const plane = node("div", undefined, "graph-plane");
    const board = node("div", undefined, "graph-board");
    const wires = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    wires.classList.add("graph-wires"); wires.setAttribute("aria-hidden", "true");
    wires.innerHTML = '<defs><marker id="graph-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7" fill="none" stroke="currentColor" stroke-width="1.5"/></marker></defs>';
    board.append(wires);
    const root = graphButton("TaskRun", progress.task_run_id || current.run?.agent_task_run_id || "运行中", { id: null, type: "task", title: "TaskRun" }, progress.task_run?.status, "task");
    board.append(root);
    const turnNodes = [];
    for (const turn of progress.turns || []) {
      const row = node("div", undefined, "graph-row");
      const turnNode = graphButton(`Turn ${turn.sequence}`, `${turn.intents.length} 个 Intent`, { id: turn.turn_id, type: "turn", title: `Turn ${turn.sequence}` }, turn.status, "turn");
      const intents = node("div", undefined, "graph-intents");
      const intentNodes = [];
      for (const intent of [...turn.intents].sort((a, b) => a.priority - b.priority)) {
        const plans = (progress.plans || []).filter((plan) => plan.intent_id === intent.intent_id);
        const item = graphButton(`Intent ${intent.priority}`, intent.description, { id: intent.intent_id, type: "intent", title: `Turn ${turn.sequence} / Intent ${intent.priority}` }, intent.status, "intent");
        item.title = `${intent.description}\n${plans.length} 个短 Plan`;
        intents.append(item); intentNodes.push(item);
      }
      if (!intentNodes.length) empty(intents, "尚无 Intent");
      row.append(turnNode, intents); board.append(row);
      turnNodes.push({ turn: turnNode, intents: intentNodes });
    }
    if (!turnNodes.length) board.append(node("p", "等待首轮 Intent 提交。", "graph-empty"));
    plane.append(board); host.append(plane);
    requestAnimationFrame(() => {
      if (!board.isConnected) return;
      wires.replaceChildren(wires.firstElementChild);
      wires.setAttribute("width", String(board.scrollWidth)); wires.setAttribute("height", String(board.scrollHeight));
      const bounds = board.getBoundingClientRect();
      const point = (el, side) => { const r = el.getBoundingClientRect(); return { x: (side === "right" ? r.right : side === "left" ? r.left : r.left + r.width / 2) - bounds.left, y: (side === "top" ? r.top : side === "bottom" ? r.bottom : r.top + r.height / 2) - bounds.top }; };
      const connect = (from, to, kind) => {
        const a = point(from, kind === "sequence" ? "bottom" : "right");
        const b = point(to, kind === "sequence" ? "top" : "left");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        const bend = kind === "sequence" ? `M${a.x} ${a.y} L${a.x} ${b.y - 12} L${b.x} ${b.y - 12} L${b.x} ${b.y}` : `M${a.x} ${a.y} C${a.x + 38} ${a.y} ${b.x - 38} ${b.y} ${b.x} ${b.y}`;
        path.setAttribute("d", bend); path.setAttribute("class", kind === "sequence" ? "graph-edge sequence" : "graph-edge"); path.setAttribute("marker-end", "url(#graph-arrow)"); wires.append(path);
      };
      if (turnNodes.length) connect(root, turnNodes[0].turn, "owner");
      turnNodes.forEach((group, index) => { if (index) connect(turnNodes[index - 1].turn, group.turn, "sequence"); group.intents.forEach((intent) => connect(group.turn, intent, "owner")); });
      applyGraphScale();
    });
  }
  function renderDetail(progress) {
    const host = get("run-detail"); host.replaceChildren();
    const turns = progress.turns || [];
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
      host.append(node("strong", `Turn ${selectedIntent.turn.sequence} / Intent ${selectedIntent.priority}`, "detail-title"), badge(selectedIntent.status), node("p", selectedIntent.description, "detail-description"));
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
  }
  function renderTimeline(progress, opened) {
    const timeline = get("run-timeline"); timeline.replaceChildren();
    get("timeline-scope").textContent = filter?.title || "全部已提交记录";
    get("clear-run-filter").hidden = !filter;
    get("clear-run-filter").onclick = () => select(null);
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
      item.append(node("p", `Plan ${short(execution.plan_id)} · step ${execution.step_index} · ${execution.execution_id}`, "run-ref"));
      const body = node("div"); item.append(body);
      const key = `${execution.execution_id}:${execution.status}`;
      const load = async () => {
        if (!item.open || body.dataset.loaded) return;
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
  }
  function renderEvidence(progress, live) {
    const stop = get("run-stop"); stop.replaceChildren();
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
    const facts = get("run-facts"); facts.replaceChildren();
    for (const fact of progress.verified_facts || []) {
      const item = node("article", undefined, "run-fact");
      if (progress.protocol_version === 3) item.append(node("p", `验证状态：${label(fact.status)}`));
      item.append(node("p", fact.content || "正文暂不可用，请刷新运行状态。"), node("p", `Fact ${fact.fact_id}\nExecution ${fact.source_execution_id || fact.source_ref}`, "run-ref"));
      facts.append(item);
    }
    if (!facts.children.length) empty(facts, "尚无已验证观察。原始输出不会自动成为 Fact。");
    const candidates = get("run-candidates"); candidates.replaceChildren();
    get("run-candidate-heading").textContent = progress.protocol_version === 3 ? "答案候选 · Candidates" : "待验证候选 · Pending";
    if (progress.protocol_version === 3) {
      for (const candidate of progress.answer_candidates || []) {
        const item = node("article", undefined, "run-candidate");
        item.append(node("strong", label(candidate.validation_state)), node("p", `候选 ${candidate.candidate_id}\n证据版本 ${candidate.evidence_version}\nExecution ${candidate.execution_id}`, "run-ref"));
        if ([19, 20].includes(progress.contract_version) && candidate.validation_state === 'pending') item.append(node('p', '等待可信验证反馈；候选尚未被确认。', 'run-meta'));
        for (const feedback of candidate.feedback || []) item.append(node("p", `${progress.mode === "simulated" ? "模拟反馈" : "反馈"}：${label(feedback.verdict)} · ${feedback.operation_id}`, "run-ref"));
        if ([19, 20].includes(progress.contract_version) && !live && candidate.checkable_submission_ids?.length) {
          const submission = candidate.checkable_submission_ids[0];
          const pending = (progress.external_validation_checks || []).find((check) => check.candidate_id === candidate.candidate_id && check.evidence_version === candidate.evidence_version && check.submission_operation_id === submission && check.status === "started");
          const key = `${progress.task_run_id}/${candidate.candidate_id}/${candidate.evidence_version}/${submission}`;
          const cached = validationQueries.get(key);
          if ((progress.external_validation_checks || []).some((check) => check.query_id === cached && check.status === "completed")) validationQueries.delete(key);
          const button = node("button", pending ? "继续核查原请求" : "核查反馈", "secondary-button");
          button.type = "button";
          button.addEventListener("click", () => {
            button.disabled = true;
            const query = pending?.query_id || validationQueries.get(key) || crypto.randomUUID();
            validationQueries.set(key, query);
            window.dispatchEvent(new CustomEvent("d9-validation-check", { detail: { challengeId: current.challengeId, selection: { candidate_id: candidate.candidate_id, evidence_version: candidate.evidence_version, submission_operation_id: submission, query_id: query } } }));
          });
          item.append(button);
        }
        candidates.append(item);
      }
    } else {
      for (const candidate of progress.pending_candidates || []) { const item = node("article", undefined, "run-candidate"); item.append(node("pre", candidate.value), node("p", `未验证 · 来源 ${candidate.source_execution_ids.join(", ")}`, "run-ref")); candidates.append(item); }
    }
    if (!candidates.children.length) empty(candidates, "尚无待验证候选。");
    const failures = get("run-failures"); failures.replaceChildren();
    for (const failure of progress.failure_summaries || []) { const item = node("article", undefined, "run-failure"); item.append(node("strong", failure.failure_type), node("p", failure.short_reason), node("p", failure.evidence_ref, "run-ref")); failures.append(item); }
    if (!failures.children.length) empty(failures, "尚无已提交失败摘要。");
  }
  document.addEventListener("DOMContentLoaded", () => {
    for (const tab of ["detail", "evidence", "log"]) get(`run-tab-${tab}`).addEventListener("click", () => setTab(tab));
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
  return { render, reset };
})();
