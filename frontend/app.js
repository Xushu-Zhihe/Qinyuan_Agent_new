"use strict";

const state = { tasks: [], selected: null, pendingFiles: [], showArchived: false, selectMode: false, selectedTasks: new Set() };
const $ = (id) => document.getElementById(id);
let toastTimer;
let runPoll;
let runSelection = 0;
let runRead = null;
const pendingLiveControls = new Map();
const liveControlErrors = {
  operator_task_expired_or_terminal: "任务已到期或结束，不能靠加时恢复。",
  operator_task_not_active: "当前任务不在运行。",
  operator_task_initializing: "运行正在初始化，请稍后重试。",
  operator_extension_exceeds_ceiling: "加时超过整体或平台截止。",
  operator_hint_invalid: "请输入1至8192字的提示。",
  operator_extension_invalid: "请输入正整数分钟。",
  operator_run_changed: "运行已切换，本次提交未应用到新运行。",
};

function notify(message) {
  const toast = $("toast"); toast.textContent = message; toast.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
}
async function request(path, options = {}) {
  const headers = { ...(options.body instanceof File ? { "Content-Type": "application/octet-stream" } : {}), ...(options.headers || {}) };
  const response = await fetch(path, { ...options, headers });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || `request_${response.status}`);
  return value;
}
function statusLabel(task) {
  if (task.archived) return ["archived", "已归档"];
  const labels = { draft: ["draft", "草稿"], starting: ["running", "启动中"], running: ["running", "运行中"], cancelling: ["running", "取消中"], cleaning: ["running", "清理中"], cleanup_confirmed: ["finished", "资源已清理"], paused: ["cancelled", "已暂停"], recovery_required: ["cancelled", "需恢复"], cancelled: ["cancelled", "已取消"], deadline_exceeded: ["cancelled", "总时限已到"], finished: ["finished", "已结束"], failed: ["cancelled", "已停止"] };
  return labels[task.status] || ["draft", task.status || "草稿"];
}
const LIVE_STATUSES = ["starting", "running", "cancelling", "cleaning"];
const PIPE_FIELDS = [["turns", "Turn"], ["intents", "Road"], ["execs", "执行"], ["facts", "Fact"]];

function runCounts(task) {
  const report = task.run && task.run.result;
  if (!report) return null;
  const turns = report.turns || [];
  return {
    turns: turns.length,
    intents: turns.reduce((sum, turn) => sum + ((turn.roads || turn.intents || []).length), 0),
    execs: (report.tool_operations || report.executions || []).length,
    facts: (report.verified_facts || []).length,
  };
}

function renderBoardStats() {
  const host = $("board-stats"); if (!host) return;
  const tasks = state.tasks.filter((task) => !task.archived);
  const running = tasks.filter((task) => LIVE_STATUSES.includes(task.run?.status)).length;
  const facts = tasks.reduce((sum, task) => sum + (runCounts(task)?.facts || 0), 0);
  const recovering = tasks.filter((task) => task.run?.status === "recovery_required" || task.run?.result?.recovery?.required).length;
  const cards = [
    { tone: "info", label: "题目", value: String(tasks.length), hint: `${state.tasks.length - tasks.length} 个归档` },
    { tone: running ? "live" : "", label: "运行中", value: String(running), hint: running ? "Agent 进程持有 Sandbox" : "当前没有活动运行" },
    { tone: "info", label: "已验证 Fact", value: String(facts), hint: "外层复核通过后写入" },
    { tone: recovering ? "warn" : "", label: "待恢复", value: String(recovering), hint: recovering ? "需要显式恢复后才能继续" : "无未知操作" },
  ];
  RunView.patchChildren(host, cards.map((item) => {
    const card = document.createElement("div");
    card.className = "stat-card";
    if (item.tone) card.dataset.tone = item.tone;
    const label = document.createElement("span"); label.className = "stat-label"; label.textContent = item.label;
    const value = document.createElement("strong"); value.className = "stat-value"; value.textContent = item.value;
    const hint = document.createElement("span"); hint.className = "stat-hint"; hint.textContent = item.hint;
    card.append(label, value, hint);
    return card;
  }));
}

function renderTaskList() {
  const list = document.createDocumentFragment();
  const tasks = state.tasks.filter((task) => state.showArchived || !task.archived);
  renderBoardStats();
  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "还没有题目。创建一道题，选择运行模式后即可启动 Agent。";
    list.append(empty);
    RunView.patchChildren($("task-list"), Array.from(list.childNodes));
    $("batch-cleanup").hidden = true;
    return;
  }
  tasks.forEach((task) => {
    const [kind, label] = statusLabel(task);
    const live = LIVE_STATUSES.includes(task.run?.status);
    const card = document.createElement("button");
    card.dataset.key = task.id;
    card.type = "button";
    card.className = [
      "task-card",
      state.selected === task.id ? "selected" : "",
      state.selectMode && state.selectedTasks.has(task.id) ? "multi-selected" : "",
      state.selectMode ? "checking" : "",
      live ? "is-live" : "",
    ].filter(Boolean).join(" ");
    if (task.archived) card.style.opacity = "0.62";
    card.addEventListener("click", () => {
      if (state.selectMode) toggleTaskSelection(task.id);
      else selectTask(task.id);
    });

    const top = document.createElement("div"); top.className = "task-card-top";
    const chip = document.createElement("span"); chip.className = "cat-chip"; chip.textContent = task.category || "unknown";
    const pill = document.createElement("span"); pill.className = `status-pill ${kind}`;
    const dot = document.createElement("i"); pill.append(dot, document.createTextNode(label));
    top.append(chip, pill);

    const title = document.createElement("strong"); title.className = "task-card-title"; title.textContent = task.title;
    const target = document.createElement("p");
    target.className = `task-card-target${task.endpoint ? "" : " is-empty"}`;
    target.textContent = task.endpoint || "无远程目标 · 仅附件观察";

    const counts = runCounts(task);
    const pipe = document.createElement("div"); pipe.className = "task-pipe";
    for (const [key, text] of PIPE_FIELDS) {
      const item = document.createElement("span");
      item.className = `pipe-item${key === "facts" && counts ? " accent" : ""}${counts ? "" : " idle"}`;
      const num = document.createElement("b"); num.textContent = counts ? String(counts[key]) : "—";
      item.append(num, document.createTextNode(key === "intents" && !task.run?.result?.goal_validation ? "Intent" : text));
      pipe.append(item);
    }

    const foot = document.createElement("div"); foot.className = "task-card-foot";
    const attachmentText = document.createElement("span");
    attachmentText.textContent = counts ? `${task.attachments.length} 附件 · ${task.deadline_minutes || 60} 分钟时限` : `${task.attachments.length} 附件 · 尚未运行`;
    const go = document.createElement("span"); go.className = "go"; go.textContent = task.archived ? "查看" : counts ? "进入 →" : "准备运行 →";
    foot.append(attachmentText, go);

    card.append(top, title, target, pipe, foot);

    if (state.selectMode) {
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "task-checkbox";
      checkbox.checked = state.selectedTasks.has(task.id);
      checkbox.addEventListener("click", (event) => event.stopPropagation());
      card.prepend(checkbox);
    }
    list.append(card);
  });
  RunView.patchChildren($("task-list"), Array.from(list.childNodes));
  $("batch-cleanup").hidden = !state.selectMode || state.selectedTasks.size === 0;
}
function renderAttachments() {
  const list = $("attachment-list"); list.replaceChildren(); $("attachment-count").textContent = `${state.pendingFiles.length} / 16`;
  if (!state.pendingFiles.length) { const empty = document.createElement("div"); empty.className = "empty-inline"; empty.textContent = "没有附件也可以保存题目。"; list.append(empty); return; }
  state.pendingFiles.forEach((file, index) => { const row = document.createElement("div"); row.className = "pending-file"; const info = document.createElement("div"); const name = document.createElement("strong"); name.textContent = file.name; const size = document.createElement("small"); size.textContent = `${file.size.toLocaleString()} B · 待上传`; info.append(name, size); const remove = document.createElement("button"); remove.type = "button"; remove.className = "icon-button"; remove.textContent = "×"; remove.title = `移除 ${file.name}`; remove.addEventListener("click", () => { state.pendingFiles.splice(index, 1); renderAttachments(); }); row.append(info, remove); list.append(row); });
}
function renderSavedAttachments(task) {
  if (!task || !task.attachments.length) return;
  task.attachments.forEach((file) => {
    const row = document.createElement("div"); row.className = "pending-file saved-file";
    const info = document.createElement("div"); const name = document.createElement("strong"); name.textContent = file.name; const size = document.createElement("small"); size.textContent = `${Number(file.byte_size).toLocaleString()} B · SHA-256 ${file.sha256}`; info.append(name, size);
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "icon-button"; remove.textContent = "×"; remove.title = `删除 ${file.name}`; remove.addEventListener("click", async () => { try { await request(`/api/challenges/${task.id}/attachments/${file.id}`, { method: "DELETE" }); notify("附件已删除"); await loadTasks(); selectTask(task.id); } catch (error) { notify(`删除失败：${error.message}`); } }); row.append(info, remove); $("attachment-list").append(row);
  });
}
function setHttpMethods(methods = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH"]) {
  document.querySelectorAll('[name="http-method"]').forEach((input) => { input.checked = methods.includes(input.value); input.disabled = !!state.selected && state.tasks.find((task) => task.id === state.selected)?.status !== "draft"; });
}
function resetForm() {
  runRead?.abort();
  $("user-hint-content").value = "";
  setHttpMethods();
  runSelection += 1; RunView.reset(); $("run-panel").hidden = true;
  clearTimeout(runPoll); $("task-id").value = ""; $("task-title").value = ""; $("task-category").value = "pwn"; $("task-deadline").value = "60"; $("step-timeout").value = "60"; $("task-endpoint").value = ""; $("task-description").value = ""; const flagHeadInput = $("task-flag-head"); if (flagHeadInput) flagHeadInput.value = ""; $("editor-title").textContent = "新建题目"; $("editor-state").textContent = "尚未保存"; $("detail-task-title").textContent = "新建题目"; $("detail-task-status").textContent = ""; $("task-actions").hidden = true; state.selected = null; state.pendingFiles = []; $("attachments").value = ""; renderAttachments(); renderTaskList();
}
function selectTask(id, navigate = true) {
  const task = state.tasks.find((item) => item.id === id); if (!task) return;
  const changed = state.selected !== id;
  if (changed) { runRead?.abort(); runReadBackoff = 0; RunView.reset(); }
  if (state.selected !== id) {
    $("user-hint-content").value = pendingLiveControls.get(`${id}/user_hint`)?.value.payload.content || "";
    $("user-hint-status").textContent = "下一次新决策可读取；不会打断当前生成，也不会自动成为已验证事实。";
  }
  clearTimeout(runPoll); runSelection += 1; state.selected = id; $("task-id").value = task.id; $("task-title").value = task.title; $("task-category").value = task.category; $("task-deadline").value = task.deadline_minutes || 60; $("step-timeout").value = task.step_timeout_seconds || 60; $("task-endpoint").value = task.endpoint; $("task-description").value = task.description; const flagHeadInput = $("task-flag-head"); if (flagHeadInput) flagHeadInput.value = task.flag_head || ""; setHttpMethods(task.http_methods); $("editor-title").textContent = "题目详情"; $("editor-state").textContent = task.archived ? "已归档" : task.status; $("detail-task-title").textContent = task.title; $("detail-task-status").textContent = task.archived ? "已归档" : statusLabel(task)[1]; $("task-actions").hidden = false; $("archive-task").hidden = task.archived; $("restore-task").hidden = !task.archived; $("purge-task").hidden = !task.archived; state.pendingFiles = []; renderAttachments(); renderSavedAttachments(task); if (navigate) showView("detail"); if (changed) renderRun(task.run); renderTaskList(); refreshRun(id, runSelection);
}
function renderRun(run, events = [], progress = null) {
  const active = ["starting", "running", "cancelling", "cleaning", "paused", "recovery_required"].includes(run?.status);
  const recoverableCancel = run?.status === "recovery_required" && [2, 3].includes(run?.result?.protocol_version) && !!run?.agent_task_run_id && run?.result?.task_run_id === run?.agent_task_run_id && ["running", "awaiting_confirmation", "awaiting_external_feedback"].includes(run?.result?.task_run?.status);
  $("run-panel").hidden = !state.selected;
  $("start-run").hidden = !state.selected || active || !!state.tasks.find((task) => task.id === state.selected)?.archived;
  $("cancel-run").hidden = !recoverableCancel && !["starting", "running", "cancelling", "paused"].includes(run?.status);
  $("cancel-run").disabled = run?.status === "cancelling";
  $("cancel-run").textContent = recoverableCancel ? "结束并清理" : run?.status === "paused" ? "取消并清理" : "取消运行";
  const terminal = ["achieved", "assumed_achieved", "partial", "inconclusive", "failed", "cancelled", "deadline_exceeded"].includes(run?.result?.task_run?.status);
  $("cleanup-run").hidden = run?.status !== "recovery_required" || !terminal || run?.result?.cleanup?.status === "succeeded";
  $("force-repair").hidden = !run || !["recovery_required", "failed", "cancelled", "deadline_exceeded"].includes(run.status);
  const report = progress || run?.result;
  const editable = run?.status === "running" && report?.goal_validation && report.task_run?.deadline_at > Date.now() / 1000;
  $("extend-run-control").hidden = !report?.goal_validation;
  for (const [action, buttonId, inputId] of [["extend_question", "extend-run", "extend-run-minutes"], ["user_hint", "user-hint-submit", "user-hint-content"]]) {
    const pending = pendingLiveControls.get(`${state.selected}/${action}`);
    $(buttonId).disabled = pending?.busy || (!editable && !pending);
    $(inputId).disabled = !!pending || !editable;
    $(buttonId).textContent = pending ? "确认原提交" : action === "user_hint" ? "提交提示" : "加时";
  }
  $("resume-run").hidden = ![19, 20, 21, 22].includes(report?.contract_version) || report?.task_run?.status !== "running" || !["paused", "recovery_required"].includes(run?.status) || (report?.recovery?.required && report.recovery.reason !== "operation_or_evidence_unknown");
  RunView.render({ run, events, progress, challengeId: state.selected });
}
let runReadBackoff = 0;
async function refreshRun(id, selection = runSelection) {
  if (state.selected !== id || selection !== runSelection) return;
  if (runRead?.taskId === id && runRead?.selection === selection) return;
  clearTimeout(runPoll);
  runRead?.abort();
  const reader = new AbortController(); reader.taskId = id; reader.selection = selection; runRead = reader;
  let retry = false;
  const timeout = setTimeout(() => reader.abort(), 15000);
  try {
    const data = await request(`/api/challenges/${id}/progress`, { signal: reader.signal, cache: "no-store" });
    if (runRead !== reader || state.selected !== id || selection !== runSelection) return;
    retry = data.snapshot?.state === "refreshing" || data.snapshot?.state === "unavailable";
    const snapshotFailed = data.snapshot?.state === "unavailable" || data.progress?.recovery?.reason === "state_read_failed";
    const initialSnapshot = data.snapshot?.state === "refreshing" && !data.progress;
    $("run-read-error").hidden = !snapshotFailed && !initialSnapshot;
    if (snapshotFailed) $("run-read-error").textContent = "进度暂不可用，稍后重试。";
    else if (initialSnapshot) $("run-read-error").textContent = "正在生成进度快照。";
    if (data.snapshot?.state !== "refreshing" || data.progress) renderRun(data.run, data.events, data.progress);
    const task = state.tasks.find((item) => item.id === id);
    if (task && data.run) { task.run = { ...data.run, result: data.progress || data.run.result }; task.status = data.run.status; $("editor-state").textContent = task.status; $("detail-task-status").textContent = statusLabel(task)[1]; renderTaskList(); }
  } catch (error) {
    if (runRead !== reader || state.selected !== id || selection !== runSelection) return;
    retry = true;
    $("run-read-error").hidden = false; $("run-read-error").textContent = reader.signal.aborted
      ? "进度读取超时，保留上次快照并重试。" : `进度读取失败，保留上次快照：${error.message}`;
  } finally {
    clearTimeout(timeout);
  }
  if (runRead === reader && state.selected === id && selection === runSelection) {
    runRead = null;
    const live = LIVE_STATUSES.includes(state.tasks.find((task) => task.id === id)?.run?.status);
    runReadBackoff = retry ? Math.min(runReadBackoff ? runReadBackoff * 2 : 2000, 15000) : 0;
    runPoll = setTimeout(() => refreshRun(id, selection), runReadBackoff || (live ? 1000 : 5000));
  }
}
async function startRun() {
  const id = state.selected; if (!id) return;
  const options = { method: "POST" };
  if (["d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"].includes(state.executionProfile)) {
    const mode = $("run-mode").value;
    if (!mode) { notify("请先选择运行模式"); return; }
    options.body = JSON.stringify({ mode });
    options.headers = { "Content-Type": "application/json" };
  }
  try { const run = await request(`/api/challenges/${id}/run`, options); renderRun(run); await loadTasks(); await refreshRun(id); notify("Agent 进程已启动"); }
  catch (error) { notify(`启动失败：${error.message === "competition_schedule_active" ? "当前任务由赛程管理，请回比赛控制继续原赛程。" : error.message}`); }
}
async function cancelRun() {
  const id = state.selected; if (!id) return;
  try { const run = await request(`/api/challenges/${id}/cancel`, { method: "POST" }); if (state.selected === id) { renderRun(run); const task = state.tasks.find((item) => item.id === id); if (task) task.run = run; await refreshRun(id); } notify("取消请求已发送"); }
  catch (error) { notify(`取消失败：${error.message}`); }
}
async function submitLiveControl(action) {
  const challengeId = state.selected;
  const run = state.tasks.find(task => task.id === challengeId)?.run;
  if (!run) return;
  const key = `${challengeId}/${action}`;
  let pending = pendingLiveControls.get(key);
  if (pending?.busy) return;
  if (!pending) {
    const payload = action === "user_hint" ? { content: $("user-hint-content").value } : { seconds: Number($("extend-run-minutes").value) * 60 };
    if (action === "user_hint" && !payload.content.trim()) { notify("请先填写提示。"); return; }
    if (action === "extend_question" && (!Number.isSafeInteger(payload.seconds) || payload.seconds <= 0 || payload.seconds % 60)) { notify("请输入正整数分钟。"); return; }
    pending = { value: { run_id: run.run_id, operation_id: crypto.randomUUID(), action, payload }, busy: false };
    pendingLiveControls.set(key, pending);
  }
  pending.busy = true; renderRun(run);
  try {
    await request(`/api/challenges/${challengeId}/operator-control`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pending.value) });
    pendingLiveControls.delete(key);
    if (state.selected === challengeId && action === "user_hint") {
      $("user-hint-content").value = "";
      $("user-hint-status").textContent = "提示已登记，下一次新决策可读取；当前生成继续使用原上下文。";
    }
    notify(action === "user_hint" ? "用户提示已登记" : "任务已加时，在途规划沿原请求等待");
  } catch (error) {
    if (Object.hasOwn(liveControlErrors, error.message) || ["operator_operation_conflict", "operator_payload_invalid", "operator_scope_invalid", "operator_task_cancelled"].includes(error.message)) pendingLiveControls.delete(key);
    notify(liveControlErrors[error.message] || `提交结果待确认，请用原按钮重试：${error.message}`);
  } finally {
    pending.busy = false;
    if (state.selected === challengeId) await refreshRun(challengeId);
  }
}
document.addEventListener("DOMContentLoaded", () => {
  $("extend-run").addEventListener("click", () => submitLiveControl("extend_question"));
  $("user-hint-form").addEventListener("submit", event => { event.preventDefault(); submitLiveControl("user_hint"); });
});
async function cleanupRun() {
  const id = state.selected; if (!id) return;
  try { const run = await request(`/api/challenges/${id}/cleanup`, { method: "POST" }); renderRun(run); await loadTasks(); notify("已启动资源清理；成功后可归档题目"); }
  catch (error) { notify(`资源清理未启动：${error.message}`); }
}
async function forceRepair() {
  const id = state.selected; if (!id) return;
  try {
    const result = await request(`/api/challenges/${id}/force-repair`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    await loadTasks(); await refreshRun(id);
    notify(`已强行修正：删除 ${result.deleted_runs} 条运行记录，题目回到草稿；备份 ${result.backup.split(/[\\/]/).pop()}`);
  } catch (error) { notify(`强行修正失败：${error.message}`); }
}
async function resumeRun() {
  const id = state.selected; if (!id) return;
  try { const run = await request(`/api/challenges/${id}/resume`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); renderRun(run); await refreshRun(id); notify("已显式恢复运行，沿原配置与预算继续"); }
  catch (error) { notify(`恢复未启动：${error.message}`); }
}
window.addEventListener("d9-validation-check", async (event) => {
  const { challengeId, selection } = event.detail;
  if (!challengeId || challengeId !== state.selected) return;
  try { const run = await request(`/api/challenges/${challengeId}/validation-check`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(selection) }); renderRun(run); await refreshRun(challengeId); notify("核查请求已交给可信入口；不会自动执行任务"); }
  catch (error) { notify(`核查未启动：${error.message}`); }
});
  window.addEventListener("stage-human-decision", async (event) => {
    const { challengeId, selection } = event.detail;
    if (!challengeId || challengeId !== state.selected) return;
    try { const run = await request(`/api/challenges/${challengeId}/human-decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(selection) }); renderRun(run); await refreshRun(challengeId); notify("人工决定已登记"); }
    catch (error) { notify(`人工决定未完成：${error.message}`); }
  });
async function loadTasks() { state.tasks = await request("/api/challenges"); renderTaskList(); if (state.selected && state.tasks.some((task) => task.id === state.selected)) selectTask(state.selected, false); else resetForm(); }
async function saveTask(event) {
  event.preventDefault(); const value = { title: $("task-title").value.trim(), category: $("task-category").value, description: $("task-description").value, endpoint: $("task-endpoint").value.trim(), deadline_minutes: Number($("task-deadline").value), step_timeout_seconds: Number($("step-timeout").value) };
  const flagHeadInput = $("task-flag-head"); const flagHead = flagHeadInput ? flagHeadInput.value.trim() : ""; if (flagHead) value.flag_head = flagHead;
  if (["d8_v20", "d8_v21", "d8_v22"].includes(state.executionProfile)) value.http_methods = Array.from(document.querySelectorAll('[name="http-method"]:checked')).map((input) => input.value);
  const existing = state.tasks.find((item) => item.id === $("task-id").value);
  if (state.pendingFiles.length + (existing?.attachments.length || 0) > 16) { notify("每题最多 16 个附件"); return; }
  try { const id = $("task-id").value; const task = id ? await request(`/api/challenges/${id}`, { method: "PUT", body: JSON.stringify(value), headers: { "Content-Type": "application/json" } }) : await request("/api/challenges", { method: "POST", body: JSON.stringify(value), headers: { "Content-Type": "application/json" } }); for (const file of state.pendingFiles) await request(`/api/challenges/${task.id}/attachments?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file }); notify("题目已保存"); await loadTasks(); selectTask(task.id); } catch (error) { notify(`保存失败：${error.message}`); }
}
async function archiveTask() { const task = state.tasks.find((item) => item.id === state.selected); if (!task) return; try { await request(`/api/challenges/${task.id}/archive`, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } }); state.showArchived = true; $("show-archived").checked = true; notify("题目已归档；现在可永久清理"); await loadTasks(); } catch (error) { notify(`归档失败：${error.message === "cancel_running_challenge_first" ? "请先取消并清理运行" : error.message}`); } }
async function restoreTask() { const task = state.tasks.find((item) => item.id === state.selected); if (!task) return; try { await request(`/api/challenges/${task.id}/restore`, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } }); notify("题目已恢复"); await loadTasks(); } catch (error) { notify(`恢复失败：${error.message}`); } }
async function purgeTask() { const task = state.tasks.find((item) => item.id === state.selected); if (!task) return; const confirmation = window.prompt(`永久清理“${task.title}”及其附件？\n请输入题目名称确认。`); if (confirmation === null) return; try { await request(`/api/challenges/${task.id}/purge`, { method: "POST", body: JSON.stringify({ confirm_title: confirmation }), headers: { "Content-Type": "application/json" } }); notify("题目和附件已永久清理"); resetForm(); await loadTasks(); showView("tasks"); } catch (error) { notify(`清理失败：${error.message === "archive_terminal_challenge_first" ? "请先结束运行并归档题目" : error.message}`); } }
function renderWindowsSecurityHint() {
  const value = state.windowsSecurity;
  const realMode = $("run-mode-control").hidden ? state.executionProfile === "d7" : $("run-mode").value === "real";
  $("windows-security-hint").hidden = !value?.supported || !realMode || (value.verified && value.status === "excluded" && value.evidence_access?.status === "passed" && !value.busy);
}
async function refreshWindowsSecurity() {
  try { state.windowsSecurity = await request("/api/windows-security"); renderWindowsSecurityHint(); }
  catch (_) { state.windowsSecurity = { supported: true, status: "unknown" }; renderWindowsSecurityHint(); }
}
function showView(name, updateHistory = true) {
  if (name !== "detail") { clearTimeout(runPoll); runSelection += 1; }
  document.body.classList.toggle("detail-open", name === "detail");
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === (name === "detail" ? "tasks" : name)));
  document.querySelectorAll(".view").forEach((view) => { const active = view.id === `${name}-view`; view.hidden = !active; view.classList.toggle("active", active); });
  $("current-view").textContent = name === "competition" ? "比赛控制" : name === "detail" ? "题目详情" : "任务与运行";
  if (name === "competition") CompetitionPage.load();
  if (updateHistory) {
    const route = name === "detail" ? (state.selected ? `#task/${state.selected}` : "#new") : `#${name}`;
    if (location.hash !== route) history.pushState(null, "", route);
  }
}
function syncRoute() {
  const route = location.hash;
  if (route.startsWith("#task/")) {
    const id = route.slice(6);
    if (state.tasks.some((task) => task.id === id)) { selectTask(id, false); showView("detail", false); return; }
  }
  if (route === "#new") { resetForm(); showView("detail", false); return; }
  showView(route === "#competition" ? "competition" : "tasks", false);
}
async function health() { try { const value = await request("/api/health"); state.executionProfile = value.capabilities?.execution_profile; $("profile-pill").textContent = state.executionProfile ? `profile ${state.executionProfile}` : `协议 v${value.capabilities?.protocol_version || 3}`; $("http-methods-control").hidden = !["d8_v20", "d8_v21", "d8_v22"].includes(state.executionProfile); $("run-mode").querySelector('option[value="real"]').textContent = value.capabilities?.real_sandbox_backend === "docker" ? "真实模型＋Docker Sandbox" : "真实模型＋本地模拟 Sandbox"; $("run-mode-control").hidden = !["d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"].includes(state.executionProfile); if (["d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"].includes(state.executionProfile)) { $("run-mode").value = value.capabilities.d7_mode || ""; $("run-mode").disabled = !!value.capabilities.d7_mode; } $("service-status").textContent = value.status === "ready" ? "服务已连接" : "服务状态异常"; $("connection-text").textContent = "回环服务"; } catch (_) { $("service-status").textContent = "服务未连接"; $("connection-text").textContent = "未连接"; $("profile-pill").textContent = "离线"; } }
function toggleTaskSelection(taskId) {
  if (state.selectedTasks.has(taskId)) {
    state.selectedTasks.delete(taskId);
  } else {
    state.selectedTasks.add(taskId);
  }
  renderTaskList();
}
function toggleSelectMode(enabled) {
  state.selectMode = enabled;
  if (!enabled) {
    state.selectedTasks.clear();
  }
  renderTaskList();
}
async function batchCleanup() {
  const ids = Array.from(state.selectedTasks);
  if (ids.length === 0) {
    notify("请先选择需要清理的任务");
    return;
  }
  const confirmation = window.confirm(`确定要清理 ${ids.length} 个任务的资源吗？`);
  if (!confirmation) return;
  try {
    const result = await request("/api/challenges/batch-cleanup", {
      method: "POST",
      body: JSON.stringify({ challenge_ids: ids }),
      headers: { "Content-Type": "application/json" }
    });
    const succeeded = result.results.filter(r => r.status === "started").length;
    const failed = result.results.filter(r => r.status === "failed").length;
    const skipped = result.results.filter(r => r.status === "skipped").length;

    // Show detailed error info for failed tasks
    const failedDetails = result.results
      .filter(r => r.status === "failed")
      .map(r => `${r.error || "unknown"}`)
      .join(", ");

    let message = `批量清理结果：${succeeded} 个已启动`;
    if (failed > 0) message += `，${failed} 个失败 (${failedDetails})`;
    if (skipped > 0) message += `，${skipped} 个跳过`;

    notify(message);
    console.log("Batch cleanup results:", result.results);
    state.selectedTasks.clear();
    await loadTasks();
  } catch (error) {
    notify(`批量清理失败：${error.message}`);
  }
}
function init() { document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view))); $("new-task").addEventListener("click", () => { resetForm(); showView("detail"); }); $("back-to-tasks").addEventListener("click", () => showView("tasks")); $("cancel-edit").addEventListener("click", () => showView("tasks")); $("refresh").addEventListener("click", () => loadTasks().catch((error) => notify(`刷新失败：${error.message}`))); $("show-archived").addEventListener("change", (event) => { state.showArchived = event.target.checked; renderTaskList(); }); $("select-mode").addEventListener("change", (event) => { toggleSelectMode(event.target.checked); }); $("batch-cleanup").addEventListener("click", batchCleanup); $("task-form").addEventListener("submit", saveTask); $("attachments").addEventListener("change", (event) => { const files = Array.from(event.target.files); const existing = state.tasks.find((item) => item.id === state.selected); if (state.pendingFiles.length + files.length + (existing?.attachments.length || 0) > 16) { notify("每题最多 16 个附件"); return; } state.pendingFiles.push(...files); renderAttachments(); if (existing) renderSavedAttachments(existing); }); $("archive-task").addEventListener("click", archiveTask); $("restore-task").addEventListener("click", restoreTask); $("purge-task").addEventListener("click", purgeTask); $("start-run").addEventListener("click", startRun); $("cancel-run").addEventListener("click", cancelRun); $("cleanup-run").addEventListener("click", cleanupRun); window.addEventListener("popstate", syncRoute); window.addEventListener("hashchange", syncRoute); health().then(refreshWindowsSecurity); loadTasks().then(syncRoute).catch((error) => { $("task-list").textContent = `服务未连接：${error.message}`; }); }
document.addEventListener("DOMContentLoaded", init);
document.addEventListener("DOMContentLoaded", () => {
  $("run-mode").addEventListener("change", renderWindowsSecurityHint);
});
document.addEventListener("DOMContentLoaded", () => $("resume-run").addEventListener("click", resumeRun));
document.addEventListener("DOMContentLoaded", () => $("force-repair").addEventListener("click", forceRepair));

window.addEventListener("competition-submit",async event=>{
  const challengeId=event.detail.challengeId;
  if (!challengeId || challengeId!==state.selected) return;
  try {const run=await request(`/api/challenges/${challengeId}/competition-submit`,{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});renderRun(run);await refreshRun(challengeId);notify("候选已交给宿主提交器；结果未知时不会重发。");}
  catch(error){notify(`提交未启动：${error.message}`);}
});
