"use strict";

const state = { tasks: [], selected: null, pendingFiles: [], showArchived: false };
const $ = (id) => document.getElementById(id);
let toastTimer;
let runPoll;
let runSelection = 0;

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
function renderTaskList() {
  const list = $("task-list"); list.replaceChildren();
  const tasks = state.tasks.filter((task) => state.showArchived || !task.archived);
  if (!tasks.length) { const empty = document.createElement("div"); empty.className = "empty-state"; empty.textContent = "还没有题目，先创建一个准备任务。"; list.append(empty); return; }
  tasks.forEach((task) => {
    const [kind, label] = statusLabel(task); const row = document.createElement("button"); row.type = "button"; row.className = `task-row${state.selected === task.id ? " selected" : ""}`; row.addEventListener("click", () => selectTask(task.id));
    const title = document.createElement("strong"); title.textContent = task.title; const meta = document.createElement("small"); meta.textContent = `${task.category.toUpperCase()} · ${task.attachments.length} 个附件 · ${task.endpoint || "无远程目标"}`; const badge = document.createElement("span"); badge.className = `status-badge ${kind}`; badge.textContent = label; row.append(title, meta, badge); list.append(row);
  });
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
function resetForm() {
  runSelection += 1; RunView.reset(); $("run-panel").hidden = true;
  clearTimeout(runPoll); $("task-id").value = ""; $("task-title").value = ""; $("task-category").value = "pwn"; $("task-deadline").value = "60"; $("step-timeout").value = "60"; $("task-endpoint").value = ""; $("task-description").value = ""; $("editor-title").textContent = "新建题目"; $("editor-state").textContent = "尚未保存"; $("detail-task-title").textContent = "新建题目"; $("detail-task-status").textContent = ""; $("task-actions").hidden = true; state.selected = null; state.pendingFiles = []; $("attachments").value = ""; renderAttachments(); renderTaskList();
}
function selectTask(id, navigate = true) {
  const task = state.tasks.find((item) => item.id === id); if (!task) return;
  clearTimeout(runPoll); runSelection += 1; RunView.reset(); state.selected = id; $("task-id").value = task.id; $("task-title").value = task.title; $("task-category").value = task.category; $("task-deadline").value = task.deadline_minutes || 60; $("step-timeout").value = task.step_timeout_seconds || 60; $("task-endpoint").value = task.endpoint; $("task-description").value = task.description; $("editor-title").textContent = "题目详情"; $("editor-state").textContent = task.archived ? "已归档" : task.status; $("detail-task-title").textContent = task.title; $("detail-task-status").textContent = task.archived ? "已归档" : statusLabel(task)[1]; $("task-actions").hidden = false; $("archive-task").hidden = task.archived; $("restore-task").hidden = !task.archived; $("purge-task").hidden = !task.archived; state.pendingFiles = []; renderAttachments(); renderSavedAttachments(task); if (navigate) showView("detail"); renderRun(task.run); renderTaskList(); refreshRun(id, runSelection);
}
function renderRun(run, events = [], progress = null) {
  const active = ["starting", "running", "cancelling", "cleaning", "paused", "recovery_required"].includes(run?.status);
  $("run-panel").hidden = !state.selected;
  $("start-run").hidden = !state.selected || active || !!state.tasks.find((task) => task.id === state.selected)?.archived;
  $("cancel-run").hidden = !["starting", "running", "cancelling", "paused"].includes(run?.status);
  $("cancel-run").disabled = run?.status === "cancelling";
  $("cancel-run").textContent = run?.status === "paused" ? "取消并清理" : "取消运行";
  const terminal = ["achieved", "assumed_achieved", "partial", "inconclusive", "failed", "cancelled", "deadline_exceeded"].includes(run?.result?.task_run?.status);
  $("cleanup-run").hidden = run?.status !== "recovery_required" || !terminal || run?.result?.cleanup?.status === "succeeded";
  RunView.render({ run, events, progress, challengeId: state.selected });
}
async function refreshRun(id, selection = runSelection) {
  if (state.selected !== id || selection !== runSelection) return;
  try {
    const data = await request(`/api/challenges/${id}/progress`);
    if (state.selected !== id || selection !== runSelection) return;
    $("run-read-error").hidden = true;
    renderRun(data.run, data.events, data.progress);
    const task = state.tasks.find((item) => item.id === id);
    if (task && data.run) { task.run = data.run; task.status = data.run.status; $("editor-state").textContent = task.status; $("detail-task-status").textContent = statusLabel(task)[1]; renderTaskList(); }
  } catch (error) {
    if (state.selected !== id || selection !== runSelection) return;
    $("run-read-error").hidden = false; $("run-read-error").textContent = `进度读取失败，保留上次快照：${error.message}`;
  }
  if (state.selected === id && selection === runSelection && ["starting", "running", "cancelling", "cleaning"].includes(state.tasks.find((task) => task.id === id)?.run?.status)) {
    clearTimeout(runPoll); runPoll = setTimeout(() => refreshRun(id, selection), 1000);
  }
}
async function startRun() {
  const id = state.selected; if (!id) return;
  try { const run = await request(`/api/challenges/${id}/run`, { method: "POST" }); renderRun(run); await loadTasks(); await refreshRun(id); notify("Agent 进程已启动"); }
  catch (error) { notify(`启动失败：${error.message}`); }
}
async function cancelRun() {
  const id = state.selected; if (!id) return;
  try { const run = await request(`/api/challenges/${id}/cancel`, { method: "POST" }); if (state.selected === id) { renderRun(run); const task = state.tasks.find((item) => item.id === id); if (task) task.run = run; await refreshRun(id); } notify("取消请求已发送"); }
  catch (error) { notify(`取消失败：${error.message}`); }
}
async function cleanupRun() {
  const id = state.selected; if (!id) return;
  try { const run = await request(`/api/challenges/${id}/cleanup`, { method: "POST" }); renderRun(run); await loadTasks(); notify("已启动资源清理；成功后可归档题目"); }
  catch (error) { notify(`资源清理未启动：${error.message}`); }
}
async function loadTasks() { state.tasks = await request("/api/challenges"); renderTaskList(); if (state.selected && state.tasks.some((task) => task.id === state.selected)) selectTask(state.selected, false); else resetForm(); }
async function saveTask(event) {
  event.preventDefault(); const value = { title: $("task-title").value.trim(), category: $("task-category").value, description: $("task-description").value, endpoint: $("task-endpoint").value.trim(), deadline_minutes: Number($("task-deadline").value), step_timeout_seconds: Number($("step-timeout").value) };
  const existing = state.tasks.find((item) => item.id === $("task-id").value);
  if (state.pendingFiles.length + (existing?.attachments.length || 0) > 16) { notify("每题最多 16 个附件"); return; }
  try { const id = $("task-id").value; const task = id ? await request(`/api/challenges/${id}`, { method: "PUT", body: JSON.stringify(value), headers: { "Content-Type": "application/json" } }) : await request("/api/challenges", { method: "POST", body: JSON.stringify(value), headers: { "Content-Type": "application/json" } }); for (const file of state.pendingFiles) await request(`/api/challenges/${task.id}/attachments?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file }); notify("题目已保存"); await loadTasks(); selectTask(task.id); } catch (error) { notify(`保存失败：${error.message}`); }
}
async function archiveTask() { const task = state.tasks.find((item) => item.id === state.selected); if (!task) return; try { await request(`/api/challenges/${task.id}/archive`, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } }); state.showArchived = true; $("show-archived").checked = true; notify("题目已归档；现在可永久清理"); await loadTasks(); } catch (error) { notify(`归档失败：${error.message === "cancel_running_challenge_first" ? "请先取消并清理运行" : error.message}`); } }
async function restoreTask() { const task = state.tasks.find((item) => item.id === state.selected); if (!task) return; try { await request(`/api/challenges/${task.id}/restore`, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } }); notify("题目已恢复"); await loadTasks(); } catch (error) { notify(`恢复失败：${error.message}`); } }
async function purgeTask() { const task = state.tasks.find((item) => item.id === state.selected); if (!task) return; const confirmation = window.prompt(`永久清理“${task.title}”及其附件？\n请输入题目名称确认。`); if (confirmation === null) return; try { await request(`/api/challenges/${task.id}/purge`, { method: "POST", body: JSON.stringify({ confirm_title: confirmation }), headers: { "Content-Type": "application/json" } }); notify("题目和附件已永久清理"); resetForm(); await loadTasks(); showView("tasks"); } catch (error) { notify(`清理失败：${error.message === "archive_terminal_challenge_first" ? "请先结束运行并归档题目" : error.message}`); } }
async function loadModel() { try { const value = await request("/api/model"); $("model-provider").value = value.provider; $("model-name").value = value.model; $("model-url").value = value.base_url; $("model-key").value = ""; $("model-state").textContent = value.key_configured ? "Key 已在内存" : "未配置 Key"; } catch (error) { notify(`读取模型配置失败：${error.message}`); } }
async function saveModel(event) { event.preventDefault(); const key = $("model-key").value; if (!key) { notify("请输入 API Key；页面不会保存它"); return; } try { const value = await request("/api/model", { method: "PUT", body: JSON.stringify({ provider: $("model-provider").value, model: $("model-name").value.trim(), base_url: $("model-url").value.trim(), api_key: key }), headers: { "Content-Type": "application/json" } }); $("model-key").value = ""; $("model-state").textContent = value.key_configured ? "Key 已在内存" : "未配置 Key"; notify("模型配置已应用"); } catch (error) { notify(`配置失败：${error.message}`); } }
function showView(name, updateHistory = true) {
  if (name !== "detail") { clearTimeout(runPoll); runSelection += 1; }
  document.body.classList.toggle("detail-open", name === "detail");
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === (name === "detail" ? "tasks" : name)));
  document.querySelectorAll(".view").forEach((view) => { const active = view.id === `${name}-view`; view.hidden = !active; view.classList.toggle("active", active); });
  $("current-view").textContent = name === "model" ? "模型配置" : name === "detail" ? "题目详情" : "题目管理";
  if (name === "model") loadModel();
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
  showView(route === "#model" ? "model" : "tasks", false);
}
async function health() { try { const value = await request("/api/health"); $("service-status").textContent = value.status === "ready" ? "服务已连接" : "服务状态异常"; $("connection-text").textContent = "回环服务"; } catch (_) { $("service-status").textContent = "服务未连接"; $("connection-text").textContent = "未连接"; } }
function init() { document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view))); $("new-task").addEventListener("click", () => { resetForm(); showView("detail"); }); $("back-to-tasks").addEventListener("click", () => showView("tasks")); $("cancel-edit").addEventListener("click", () => showView("tasks")); $("refresh").addEventListener("click", () => loadTasks().catch((error) => notify(`刷新失败：${error.message}`))); $("show-archived").addEventListener("change", (event) => { state.showArchived = event.target.checked; renderTaskList(); }); $("task-form").addEventListener("submit", saveTask); $("attachments").addEventListener("change", (event) => { const files = Array.from(event.target.files); const existing = state.tasks.find((item) => item.id === state.selected); if (state.pendingFiles.length + files.length + (existing?.attachments.length || 0) > 16) { notify("每题最多 16 个附件"); return; } state.pendingFiles.push(...files); renderAttachments(); if (existing) renderSavedAttachments(existing); }); $("archive-task").addEventListener("click", archiveTask); $("restore-task").addEventListener("click", restoreTask); $("purge-task").addEventListener("click", purgeTask); $("start-run").addEventListener("click", startRun); $("cancel-run").addEventListener("click", cancelRun); $("cleanup-run").addEventListener("click", cleanupRun); $("model-form").addEventListener("submit", saveModel); $("clear-key").addEventListener("click", async () => { try { await request("/api/model/key", { method: "DELETE" }); $("model-key").value = ""; $("model-state").textContent = "未配置 Key"; notify("服务端内存 Key 已清除"); } catch (error) { notify(`清除失败：${error.message}`); } }); window.addEventListener("popstate", syncRoute); window.addEventListener("hashchange", syncRoute); health(); loadTasks().then(syncRoute).catch((error) => { $("task-list").textContent = `服务未连接：${error.message}`; }); }
document.addEventListener("DOMContentLoaded", init);
