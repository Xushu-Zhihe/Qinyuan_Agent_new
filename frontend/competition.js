"use strict";

const CompetitionPage = (() => {
  const el = (id) => document.getElementById(`competition-${id}`);
  const endpoint = "/api/competition/profiles";
  const errors = {
    competition_profile_in_use: "活跃赛程锁定平台协议和根地址；可以修正同源凭据。",
    competition_schedule_active: "已有未结束赛程或正在运行的任务，请先处理当前运行。",
    competition_queue_invalid: "Benchmark 请填入不重复的题目标识；Arena 队列留空。",
    competition_arena_local_deadline_active: "本地单题已到期，平台仍有效。赛程等待处理，不自动放弃或领取。",
    competition_platform_operation_unknown: "平台操作结果未知；恢复时只查询状态，不自动重发。",
    competition_submission_requires_attention: "提交被拒绝或结果未知，请先核对回执、凭据及平台状态。",
    competition_task_recovery_required: "当前 Agent 需要核验恢复，已保留进展。",
    competition_workbench_restarted: "工作台已重启；补充同源凭据后显式继续。",
    operator_task_initializing: "TaskRun 正在登记，请稍后用同一操作再次确认。",
    operator_command_outcome_unknown: "控制命令结果待确认；请用原按钮再次确认，不重复增加预算。",
    operator_unknown_operation: "有未核验的执行结果，暂不能继续。",
    operator_task_expired_or_terminal: "本题已到期或终态，不能加时或恢复。",
    operator_extension_exceeds_ceiling: "加时超过整体或平台截止，请先核对预算。",
    competition_extension_exceeds_platform: "整体预算超过平台截止，未应用。",
    competition_overall_deadline: "整体预算已到期，不能延时或继续。",
    competition_target_address_not_supported: "目标需为公网地址或明确的 RFC1918 私网 IPv4；回环、链路本地等特殊地址不支持。",
    competition_import_invalid: "导入参数不正确。",
    competition_question_not_found: "平台当前未返回这道题，请刷新。",
    competition_question_not_active: "平台题目已结束或有效期已过。",
    competition_target_selection_required: "请选择明确的 HTTP(S) 入口；无协议地址需要你指定。",
    competition_target_protocol_unsupported: "当前启动入口支持 HTTP(S)；其他协议需要单独接入。",
    competition_target_not_in_question: "所选地址不属于这道平台题目。",
    competition_question_input_required: "请在任务页上传附件或补齐有效入口后启动。",
    competition_requires_contract22: "比赛提交需要当前契约22的运行入口。",
    competition_credential_missing: "尚未配置平台凭据，请输入后保存。",
    competition_auth_failed: "平台认证失败，请检查凭据及所属任务。",
    competition_timeout: "平台读取超时；未领取或提交题目。",
    competition_tls_failed: "平台 TLS 连接失败。",
    competition_transport_failed: "无法连接平台，请检查地址、网络或 VPN。",
    competition_response_invalid: "平台返回格式与所选协议不一致。",
    competition_response_too_large: "平台响应超过 2 MiB，未继续读取。",
    competition_redirect_blocked: "平台要求跳转；为保持认证归属，请填写最终平台根地址。",
    competition_rate_limited: "平台限制请求频率，请稍后手动检查。",
    competition_invalid_state: "平台任务当前不可操作，请核对平台状态。",
    competition_resource_unavailable: "平台资源暂时不可用。",
    competition_request_rejected: "平台拒绝读取请求，请检查所选协议。",
    competition_http_error: "平台返回 HTTP 错误。",
    competition_business_error: "平台返回业务失败，未将其当作认证成功。",
    competition_origin_invalid: "地址应为 http(s) 平台根地址，不含账号、路径、查询或片段。",
    competition_credential_invalid: "凭据格式不正确；Benchmark 需要 UUID，Agent Token 不含 Bearer 前缀。",
    competition_profile_invalid: "配置字段不正确，请检查名称、协议和 1–60 秒读取超时。",
    competition_profile_changed: "配置在读取期间发生变化，请重新检查。"
  };
  let session = null, sessionBusy = false, previewItems = [], pendingExtension = null;
  try {pendingExtension = JSON.parse(sessionStorage.getItem("competition-extension") || "null");} catch {}
  let profiles = [], current = null, initialized = false, busy = false, dirty = true;
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; };
  const status = (text) => { el("status").textContent = text; };
  function controls() {
    el("fields").disabled = busy;
    sessionControls();
    for (const id of ["profile", "new", "save"]) el(id).disabled = busy;
    el("check").disabled = busy || dirty || !current;
    el("clear").disabled = busy || dirty || !current?.credential_configured;
  }
  function resetPreview() { el("preview").replaceChildren(node("p", "尚未读取当前配置的题目信息。")); el("summary").replaceChildren(); }
  function credentialLabel() { el("credential-label").textContent = el("adapter").value === "arena" ? "Agent Token（不含 Bearer 前缀）" : "BENCHMARK_TOKEN（UUID）"; }
  function options() {
    el("profile").replaceChildren(new Option("新建配置", ""), ...profiles.map(p => new Option(p.name, p.id)));
    el("profile").value = current?.id || "";
  }
  function select(profile) {
    current = profile;
    el("name").value = profile?.name || "";
    el("adapter").value = profile?.adapter || "arena";
    el("url").value = profile?.base_url || "https://www.nssctf.cn";
    el("timeout").value = profile?.timeout_seconds || 10;
    el("credential").value = "";
    dirty = !profile;
    credentialLabel(); options(); resetPreview(); controls();
    status(profile ? (profile.credential_configured ? "凭据已配置，尚未检查连接。" : "配置已保存；请输入平台凭据后保存。") : "新建比赛配置后即可检查连接。");
  }
  async function load() {
    if (busy) return;
    try {
      profiles = await request(endpoint);
      if (!initialized) { initialized = true; select(profiles[0] || null); }
      else options();
      await refreshSession();
    } catch (e) { status(errors[e.message] || `本地配置读取失败：${e.message}`); }
  }
  async function save(event) {
    event.preventDefault(); busy = true; controls();
    try {
      const value = { name: el("name").value, adapter: el("adapter").value, base_url: el("url").value,
        timeout_seconds: Number(el("timeout").value), credential: el("credential").value };
      const result = await request(current ? `${endpoint}/${current.id}` : endpoint, {method: current ? "PUT" : "POST",
        headers: {"Content-Type":"application/json"}, body: JSON.stringify(value)});
      profiles = profiles.filter(p => p.id !== result.id); profiles.push(result);
      select(result);
    } catch (e) { status(errors[e.message] || `配置保存失败：${e.message}`); }
    finally { el("credential").value = ""; busy = false; controls(); }
  }
  function render(value) {
    const {items, agent} = value.preview;
    previewItems = items;
    el("summary").replaceChildren(node("span", `${items.length} 道平台题目`));
    if (agent?.rating !== undefined) el("summary").append(node("span", `当前 rating ${agent.rating}`));
    if (!items.length) {
      el("preview").replaceChildren(node("p", current.adapter === "arena" ? "连接成功，当前没有进行中的题目。本次未调用领取接口。" : "连接成功，当前任务的题目列表为空。"));
      return;
    }
    el("preview").replaceChildren(...items.map(item => {
      const card = document.createElement("article"); card.className = "competition-question";
      card.append(node("h3", item.display_name), node("p", `平台状态：${item.state}`, "competition-note"), node("p", item.description || "平台未提供题面。", "competition-description"));
      if (item.category) card.append(node("p", `类型：${item.category}`));
      if (item.flag_count !== undefined) card.append(node("p", `Flag 进度：${item.correct_flag_count} / ${item.flag_count}${item.is_completed ? " · 平台已确认整题完成" : ""}`));
      if (item.remaining_seconds !== null) card.append(node("p", `平台本次返回剩余：${item.remaining_seconds} 秒（非本地运行预算）`));
      if (item.wrong_count !== undefined && item.wrong_count !== null) card.append(node("p", `错误次数：${item.wrong_count} / ${item.max_wrong_count ?? "未提供上限"}`));
      card.append(node("pre", item.addresses.length ? item.addresses.join("\n") : "平台暂未提供可用目标地址。", "competition-addresses"));
      if (item.attachment) {
        card.append(node("p", `附件：${item.attachment.name || "未命名"}（请自行下载后在任务页上传）`));
        if (item.attachment.url) card.append(node("pre", item.attachment.url, "competition-addresses"));
      }
      const form=document.createElement("form");form.className="competition-import";
      const label=node("label","目标入口（需要明确协议）","field");
      const address=document.createElement("select"); address.setAttribute("aria-label","目标入口");
      address.append(new Option(item.addresses.length ? "请选择入口" : "附件题：在任务页上传文件", ""));
      for (const raw of item.addresses) {
        if (/^https?:\/\//i.test(raw)) address.append(new Option(raw,raw));
        else if (!raw.includes("://")) for (const scheme of ["http","https"]) address.append(new Option(`${scheme}://${raw}（请确认协议）`,`${scheme}://${raw}`));
      }
      if (item.addresses.length) address.required=true;
      label.append(address); form.append(label);
      const button=node("button","导入并打开任务","secondary-button");button.type="submit";
      button.disabled=Boolean(item.is_completed || current.adapter==="arena" && item.state!=="active");
      form.append(button);
      form.addEventListener("submit",async event=>{
        event.preventDefault();button.disabled=true;busy=true;controls();
        try {
          const task=await request(`${endpoint}/${current.id}/import`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({remote_ref:item.remote_ref,endpoint:address.value})});
          await loadTasks(); selectTask(task.id,false); showView("detail");
        } catch(e) {status(errors[e.message] || `导入失败：${e.message}`);}
        finally {button.disabled=false;busy=false;controls();}
      });
      card.append(form);
      return card;
    }));
  }
  async function check() {
    busy = true; controls(); resetPreview(); status("正在读取平台，只执行认证 GET…");
    try {
      const value = await request(`${endpoint}/${current.id}/check`, {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
      render(value); status("连接成功：认证读取已完成。尚未启动 Agent 或自动赛程。");
    } catch (e) { status(errors[e.message] || `连接检查失败：${e.message}`); }
    finally { busy = false; controls(); }
  }
  async function clear() {
    busy = true; controls();
    try {
      const result = await request(`${endpoint}/${current.id}/credential`, {method:"DELETE"});
      profiles = profiles.map(p => p.id === result.id ? result : p); select(result);
    } catch (e) { status(errors[e.message] || `凭据清除失败：${e.message}`); }
    finally { busy = false; controls(); }
  }
  function sessionControls() {
    const active = session && session.state.status !== "finished";
    el("schedule-fields").disabled = sessionBusy || Boolean(active);
    el("schedule-start").disabled = sessionBusy || Boolean(active) || !current || dirty;
    for (const id of ["pause","resume","stop-after","extend-overall"]) el(id).disabled = sessionBusy || !active;
    el("extend-question").disabled = sessionBusy || !active || !session?.state.challenge_id;
    el("open-task").disabled = !session?.state.challenge_id;
    if (active) el("stop-after").checked = session.state.stop_after_current;
    if (pendingExtension && active) {
      el("extend-question").disabled = sessionBusy || pendingExtension.action !== "extend_question";
      el("extend-overall").disabled = sessionBusy || pendingExtension.action !== "extend_overall";
      el("extend-minutes").disabled = true;
    } else el("extend-minutes").disabled = sessionBusy;
  }
  async function refreshSession() {
    if (sessionBusy) return;
    try {
      const rows = await request("/api/competition/sessions");
      session = rows.find(row => row.state.status !== "finished") || rows[0] || null;
      if (session) {
        if (pendingExtension && (pendingExtension.session_id !== session.id || session.state.status === "finished")) {pendingExtension = null;sessionStorage.removeItem("competition-extension");}
        const state = session.state;
        el("schedule-status").textContent = `${state.status} · ${errors[state.reason] || state.reason || "按串行队列执行"}${state.pending_control ? " · 加时待应用" : ""}`;
        const lines = [`平台：${session.config.profile.adapter}`, `队列位置：${state.index + 1}`, `当前题目：${state.current_ref ?? "待领取"}`, `整体截止：${new Date(state.deadline_at * 1000).toLocaleString()}`];
        if (!session.runtime && state.current_ref !== null) lines.push(`本题预算：${(state.question_seconds || session.config.question_seconds)/60} 分钟（TaskRun 登记后计时）`);
        if (session.runtime?.deadline_at) lines.push(`本题有效截止：${new Date(session.runtime.deadline_at*1000).toLocaleString()}`);
        if (session.runtime?.unknown) lines.push("存在待核验操作，禁止自动重放");
        if (state.platform?.flag_count !== undefined) lines.push(`Flag：${state.platform.correct_flag_count} / ${state.platform.flag_count}`);
        el("schedule-progress").replaceChildren(...lines.map(text => node("span",text)));
      }
    } catch(e) { el("schedule-status").textContent = errors[e.message] || e.message; }
    finally {sessionControls();}
  }
  async function scheduleCommand(value) {
    if (!session || sessionBusy) return;
    sessionBusy = true; el("schedule-error").textContent = ""; sessionControls();
    try {
      const {session_id,...command} = value;
      session = await request(`/api/competition/sessions/${session.id}/control`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(command)});
      if (value.operation_id && !session.state.pending_control) pendingExtension = null;
    } catch(e) {
      el("schedule-error").textContent = errors[e.message] || `赛程控制失败：${e.message}`;
      if (["operator_extension_invalid","operator_extension_exceeds_ceiling","competition_extension_exceeds_platform","operator_task_expired_or_terminal","competition_overall_deadline","operator_operation_conflict"].includes(e.message)) pendingExtension = null;
      return;
    } finally {sessionBusy = false; sessionStorage.setItem("competition-extension",JSON.stringify(pendingExtension)); sessionControls();}
    await refreshSession();
  }
  async function startSession(event) {
    event.preventDefault(); if (!current || sessionBusy) return;
    sessionBusy = true; sessionControls();
    try {
      const value = {profile_id:current.id,default_scheme:el("scheme").value,question_minutes:Number(el("question-minutes").value),
        overall_minutes:Number(el("overall-minutes").value),queue:el("queue").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean),
        http_methods:el("methods").value.split(",").map(x=>x.trim().toUpperCase()).filter(Boolean),mode:"real"};
      if (el("platform-end").value) value.platform_deadline_at = new Date(el("platform-end").value).getTime()/1000;
      session = await request("/api/competition/sessions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
    } catch(e) {el("schedule-error").textContent = errors[e.message] || `创建失败：${e.message}`;return;}
    finally {sessionBusy = false;sessionControls();}
    await refreshSession();
  }
  function extend(action) {
    const seconds = Number(el("extend-minutes").value)*60;
    if (!Number.isSafeInteger(seconds) || seconds <= 0) return;
    pendingExtension ||= {action,seconds,operation_id:crypto.randomUUID(),session_id:session.id};
    sessionStorage.setItem("competition-extension",JSON.stringify(pendingExtension));
    scheduleCommand(pendingExtension);
  }
  document.addEventListener("DOMContentLoaded", () => {
    el("schedule-form").addEventListener("submit",startSession);
    el("fill-queue").addEventListener("click",()=> {el("queue").value = current?.adapter === "benchmark" ? previewItems.filter(x=>!x.is_completed).map(x=>x.remote_ref).join("\n") : "";});
    el("pause").addEventListener("click",()=>scheduleCommand({action:"pause"}));
    el("resume").addEventListener("click",()=>scheduleCommand({action:"resume"}));
    el("stop-after").addEventListener("change",()=>scheduleCommand({action:"stop_after_current",enabled:el("stop-after").checked}));
    el("extend-question").addEventListener("click",()=>extend("extend_question"));
    el("extend-overall").addEventListener("click",()=>extend("extend_overall"));
    el("open-task").addEventListener("click",async()=> {if(session?.state.challenge_id){await loadTasks();selectTask(session.state.challenge_id,false);showView("detail");}});
    setInterval(()=> {if (!document.getElementById("competition-view").hidden) refreshSession();},2000);
    sessionControls();
    el("form").addEventListener("submit", save);
    el("profile").addEventListener("change", () => select(profiles.find(p => p.id === el("profile").value) || null));
    el("new").addEventListener("click", () => select(null));
    el("form").addEventListener("input", () => { dirty = true; resetPreview(); credentialLabel(); controls(); status("配置尚未保存；保存后再测试连接。"); });
    el("adapter").addEventListener("change", () => { el("url").value = el("adapter").value === "arena" ? "https://www.nssctf.cn" : ""; credentialLabel(); });
    el("check").addEventListener("click", check); el("clear").addEventListener("click", clear);
  });
  return {load};
})();
