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
  let refreshing = false, revision = 0, selectedRemote = "", records = [], dashboardOwner = null, queueSignature = null;
  try {pendingExtension = JSON.parse(sessionStorage.getItem("competition-extension") || "null");} catch {}
  let profiles = [], current = null, initialized = false, busy = false, dirty = true;
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; };
  const status = (text) => { el("status").textContent = text; };
  function controls() {
    el("fields").disabled = busy;
    const locked = session && session.state.status !== "finished" && session.config.profile.id === current?.id;
    el("adapter").disabled = el("url").disabled = busy || Boolean(locked);
    sessionControls();
    for (const id of ["profile", "new", "save"]) el(id).disabled = busy;
    el("check").disabled = busy || dirty || !current;
    el("clear").disabled = busy || dirty || !current?.credential_configured;
  }
  function resetPreview() { previewItems = []; selectedRemote = ""; el("question-picker").replaceChildren(new Option("尚未读取题目", "")); el("preview").replaceChildren(node("p", "尚未读取当前配置的题目信息。")); el("summary").replaceChildren(); renderQueue(); }
  function credentialLabel() { el("credential-label").textContent = el("adapter").value === "arena" ? "Agent Token（不含 Bearer 前缀）" : "BENCHMARK_TOKEN（UUID）"; }
  function options() {
    el("profile").replaceChildren(new Option("新建配置", ""), ...profiles.map(p => new Option(p.name, p.id)));
    el("profile").value = current?.id || "";
  }
  function select(profile) {
    revision += 1; current = profile;
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
      const firstLoad=!initialized;
      profiles = await request(endpoint);
      if (!initialized) { initialized = true; select(profiles[0] || null); }
      else {if(!dirty && current) current=profiles.find(profile=>profile.id===current.id) || current;options();controls();}
      await refreshSession();
      if(firstLoad && session) {const bound=profiles.find(profile=>profile.id===session.config.profile.id);if(bound && bound.id!==current?.id) {select(bound);renderSession();renderQueue();}}
    } catch (e) { status(errors[e.message] || `本地配置读取失败：${e.message}`); }
  }
  async function save(event) {
    event.preventDefault(); revision += 1; busy = true; controls();
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
    const {items:allItems, agent} = value.preview;
    previewItems = allItems;
    if (!allItems.some(item => String(item.remote_ref) === selectedRemote)) selectedRemote = String(allItems[0]?.remote_ref ?? "");
    el("question-picker").replaceChildren(...allItems.map(item => new Option(item.display_name,String(item.remote_ref))));
    el("question-picker").value = selectedRemote;
    const items = allItems.filter(item => String(item.remote_ref) === selectedRemote);
    renderQueue();
    el("summary").replaceChildren(node("span", `${allItems.length} 道平台题目`));
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
      if (current.adapter === "benchmark") {
        const add=node("button",draftQueue().includes(String(item.remote_ref)) ? "已在队列" : "加入待答队列","quiet-button"); add.type="button";
        add.disabled=Boolean(item.is_completed || session && session.state.status!=="finished");
        add.addEventListener("click",()=> {const refs=draftQueue();if (!refs.includes(String(item.remote_ref))) refs.push(String(item.remote_ref));el("queue").value=refs.join("\n");render({preview:{items:previewItems,agent}});});
        card.append(add);
      }
      return card;
    }));
  }
  async function check() {
    busy = true; controls(); resetPreview(); status("正在读取平台，只执行认证 GET…");
    try {
      const value = await request(`${endpoint}/${current.id}/check`, {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
      render(value); status("连接成功：认证读取已完成，本次检查未领取、启动或提交。");
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
    el("queue-list").inert=sessionBusy || busy;
    el("preview").inert=sessionBusy;
    el("schedule-start").disabled = sessionBusy || busy || Boolean(active) || !current?.credential_configured || dirty;
    for (const id of ["pause","resume","stop-after","extend-overall"]) el(id).disabled = sessionBusy || !active;
    el("pause").disabled = sessionBusy || !active || session.state.status !== "running";
    el("resume").disabled = sessionBusy || !active || !["paused","needs_attention"].includes(session.state.status);
    el("queue").disabled = el("fill-queue").disabled = sessionBusy || Boolean(active) || current?.adapter!=="benchmark";
    el("adapter").disabled = el("url").disabled = busy || Boolean(active && session.config.profile.id===current?.id);
    el("extend-question").disabled = sessionBusy || !active || !session?.state.challenge_id;
    el("open-task").disabled = !session?.state.challenge_id;
    if (active) el("stop-after").checked = session.state.stop_after_current;
    el("extend-question").textContent=pendingExtension?.action==="extend_question" ? "确认本题加时" : "本题 +";
    el("extend-overall").textContent=pendingExtension?.action==="extend_overall" ? "确认整体加时" : "整体 +";
    if (pendingExtension && active) {
      el("extend-minutes").value=pendingExtension.seconds/60;
      el("extend-question").disabled = sessionBusy || pendingExtension.action !== "extend_question";
      el("extend-overall").disabled = sessionBusy || pendingExtension.action !== "extend_overall";
      el("extend-minutes").disabled = true;
    } else el("extend-minutes").disabled = sessionBusy;
  }
  async function refreshSession() {
    if (sessionBusy || refreshing) return;
    refreshing = true; const captured = revision;
    try {
      const rows = await request("/api/competition/sessions");
      if (captured !== revision) return;
      session = rows.find(row => row.state.status !== "finished") || rows[0] || null;
      if (session && pendingExtension && (pendingExtension.session_id !== session.id || session.state.status === "finished")) {pendingExtension = null;sessionStorage.removeItem("competition-extension");}
      renderSession(); renderQueue();
      await refreshDashboard(captured);
      if (captured===revision) el("freshness").textContent=`更新于 ${new Date().toLocaleTimeString("zh-CN",{hour12:false})}`;
    } catch(e) { if(captured===revision) {el("freshness").textContent="读取失败 · 显示上次结果";el("schedule-error").textContent=errors[e.message] || e.message;} }
    finally {refreshing=false;sessionControls();}
  }
  const sessionLabels={running:"赛程运行中",pausing:"正在暂停，等待在途调用结束或核验",paused:"已暂停，保留进展",needs_attention:"需要处理，未自动恢复",finished:"赛程已结束"};
  const statusLabels={running:"进行中",suspended:"已挂起",succeeded:"已通过",failed:"失败",inconclusive:"未定",completed:"已完成",achieved:"平台已确认整题完成",awaiting_external_feedback:"等待平台反馈",deadline_exceeded:"本题时限已到",paused:"已暂停",starting:"正在登记 TaskRun",recovery_required:"待核验"};
  const date=value=> typeof value === "number" ? new Date(value*1000).toLocaleString("zh-CN",{hour12:false}) : "未知 / 未提供";
  const short=value=>String(value || "—").slice(0,8);
  function clock(value) {
    if (typeof value!=="number") return "—";
    const seconds=Math.max(0,Math.ceil(value-Date.now()/1000));
    return `${String(Math.floor(seconds/3600)).padStart(2,"0")}:${String(Math.floor(seconds/60)%60).padStart(2,"0")}:${String(seconds%60).padStart(2,"0")}`;
  }
  function renderSession() {
    const state=session?.state, runtime=session?.runtime, control=runtime?.control;
    const active=state && state.status!=="finished";
    el("schedule-form").hidden=Boolean(active);el("frozen-settings").hidden=!active;
    el("run-settings").querySelector("summary").textContent=active ? "赛程参数 · 已冻结" : "新赛程参数";
    if(active) el("frozen-settings").replaceChildren(...[["无协议入口",session.config.default_scheme?.toUpperCase() || "未提供"],["初始单题预算",`${session.config.question_seconds/60} 分钟`],["允许的 HTTP 方法",session.config.http_methods?.join(", ") || "未提供"]].map(([label,value])=> {const row=node("div");row.append(node("dt",label),node("dd",value));return row;}));
    el("schedule-status").closest(".c-command").dataset.status=state?.status || "idle";
    el("schedule-status").textContent=state ? `${sessionLabels[state.status] || state.status}${state.reason ? ` · ${errors[state.reason] || state.reason}` : ""}${state.pending_control ? " · 加时待应用" : ""}` : "尚未创建赛程";
    el("bound-source").textContent=session ? `${session.config.profile.adapter.toUpperCase()} · ${session.config.profile.base_url}` : "尚未绑定赛程";
    el("overall-clock").textContent=clock(state?.deadline_at);
    el("question-clock").textContent=runtime ? clock(runtime.deadline_at) : state?.current_ref!==null && state?.current_ref!==undefined ? "尚未计时" : "—";
    const platform=state?.platform;
    el("flag-score").textContent=platform?.flag_count!==undefined ? `${platform.correct_flag_count} / ${platform.flag_count}` : "—";
    const limits=[["本地整体截止",state?.deadline_at],["本题有效截止",runtime?.deadline_at],["本地单题截止",control?.question_deadline_at],["平台题目截止",control?.platform_deadline_at],["平台整体截止",session?.config.platform_deadline_at]];
    el("deadlines").replaceChildren(...limits.map(([label,value])=> {const row=node("div");row.append(node("dt",label),node("dd",date(value)));return row;}));
    if (platform?.wrong_count!==null && platform?.wrong_count!==undefined) {const row=node("div");row.append(node("dt","平台错误次数"),node("dd",`${platform.wrong_count} / ${platform.max_wrong_count ?? "上限未提供"}`));el("deadlines").append(row);}
    const reason=runtime?.unknown ? "存在未核验操作，不能自动重放。" : session?.runtime_error ? "本题状态读取失败；当前无法确认执行状态。" : state?.status==="pausing" ? "新操作已停止准入；在途调用尚未确认全部结束。" : state && state.deadline_at<=Date.now()/1000 && state.status!=="finished" ? "截止已到，等待可信端核验与结算。" : "本地加时不改变平台期限；有效截止取各层最小值。";
    el("schedule-progress").textContent=reason;
    const identity=session?.id || null;
    if (dashboardOwner!==identity) {dashboardOwner=identity;el("preview-details").open=!state || state.status==="finished";}
  }
  function draftQueue() {return el("queue").value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);}
  function renderQueue() {
    const active=session && session.state.status!=="finished", arena=active ? session.config.profile.adapter==="arena" : current?.adapter==="arena";
    const refs=active ? session.config.queue : draftQueue();
    const signature=JSON.stringify([active,arena,refs,active ? session.state.index : null,active ? session.state.current_ref : null,current?.id]);
    if(signature===queueSignature) return;
    queueSignature=signature;
    el("queue-count").textContent=arena ? "自动领取" : `${refs.length} 题`;
    el("queue-note").textContent=active ? "赛中只读 · 已处理不代表成功，请核对平台回执。" : "赛前选择与排序；开始后顺序冻结。";
    el("queue-list").replaceChildren();
    if (arena || !refs.length) {el("queue-list").append(node("li",arena ? `Arena ${session?.state.current_ref ? `当前 #${session.state.current_ref}` : "等待当前 / 下一题"}` : "从题目预览加入待答题目。","c-empty"));return;}
    for (const [index,ref] of refs.entries()) {
      const li=node("li"), isCurrent=active && String(session.state.current_ref)===ref;
      li.dataset.current=String(Boolean(isCurrent));
      const title=node("button",`${String(index+1).padStart(2,"0")}  ${ref}`,"c-queue-title");title.type="button";
      title.addEventListener("click",()=> {if((!active || current?.id===session.config.profile.id) && previewItems.some(item=>String(item.remote_ref)===ref)){selectedRemote=ref;render({preview:{items:previewItems}});el("preview-details").open=true;}});
      li.append(title,node("span",active ? isCurrent ? "当前题" : index<session.state.index ? "已处理" : "待答" : "待开始","c-queue-state"));
      if (!active) {
        const actions=node("div",undefined,"c-queue-actions");
        for (const [label,offset] of [["上移",-1],["下移",1],["移除",0]]) {
          const button=node("button",label);button.type="button";button.setAttribute("aria-label",`${label} ${ref}`);
          button.disabled=offset!==0 && (index+offset<0 || index+offset>=refs.length);
          button.addEventListener("click",()=> {const next=draftQueue();if(offset===0)next.splice(index,1);else [next[index],next[index+offset]]=[next[index+offset],next[index]];el("queue").value=next.join("\n");renderQueue();});actions.append(button);
        }
        li.append(actions);
      }
      el("queue-list").append(li);
    }
  }
  function category(value) {
    const text=String(value || "");
    if (/unknown|fail|error|recover|reject|missing|invalid|conflict|timeout|denied/.test(text)) return "recovery";
    if (/candidate|feedback|submission|competition/.test(text)) return "platform";
    if (/model|review|planning|summary|refin/.test(text)) return "model";
    if (/fact|evidence|conclusion|proof|import/.test(text)) return "evidence";
    if (/tool|execution|mcp|action/.test(text)) return "tool";
    return "control";
  }
  function renderRecords() {
    const filter=el("record-filter").value, labels={control:"控制",model:"模型/审核",tool:"工具",evidence:"证据",platform:"平台",recovery:"恢复"};
    const selected=records.filter(item=>filter==="all" || item.category===filter);
    el("records").replaceChildren(...selected.map(item=> {const li=node("li");li.append(node("time",item.at ? new Date(typeof item.at==="number" ? item.at*1000 : item.at).toLocaleTimeString("zh-CN",{hour12:false}) : "—"),node("span",`${labels[item.category]} · ${item.label}`));return li;}));
    if (!selected.length) el("records").append(node("li","此分类暂无已提交记录。","c-empty"));
  }
  async function refreshDashboard(captured) {
    const challenge=session?.state.challenge_id, owner=session?.id;
    if (!challenge) {
      el("current-question").replaceChildren(node("p",session ? "等待领取 / 准备当前题目。" : "配置来源并读取题目后开始。","c-empty"));
      el("agent-state").textContent="尚未运行";el("agent-metrics").replaceChildren();el("agent-frontier").replaceChildren();el("recovery").textContent="";el("feedback").textContent="平台提交回执尚未产生。";el("candidates").textContent="尚无受保护候选。";records=[];renderRecords();return;
    }
    const [task,data]=await Promise.all([request(`/api/challenges/${encodeURIComponent(challenge)}`),request(`/api/challenges/${encodeURIComponent(challenge)}/progress`)]);
    if(captured!==revision || session?.id!==owner || session?.state.challenge_id!==challenge) return;
    const progress=data.progress, intents=(progress?.turns || []).flatMap(turn=>turn.intents || []);
    el("current-question").replaceChildren(node("h3",task.title),node("p",task.description || "平台未提供题面。"),node("pre",task.endpoint || "附件任务 · 无网络入口"));
    for (const file of task.attachments || []) el("current-question").append(node("p",`附件：${file.name}`));
    el("agent-state").textContent=session.state.status==="pausing" ? "暂停中 · 核验在途调用" : session.state.status==="paused" ? "已暂停 · 进展保留" : session.state.status==="needs_attention" ? "等待处理" : statusLabels[progress?.task_run?.status || data.run?.status] || (progress ? "已读取持久状态" : "等待 TaskRun 登记");
    const values=[["Turn",progress?.turns?.length],["Intent",progress ? intents.length : undefined],[progress?.budget?.category_counts?.tool_dispatch!==undefined ? "工具派发" : "执行记录",progress?.budget?.category_counts?.tool_dispatch ?? progress?.executions?.length],["verified Fact",progress?.verified_facts?.length]];
    el("agent-metrics").replaceChildren(...values.map(([label,value])=> {const box=node("div");box.append(node("span",label),node("strong",value===undefined ? "—" : String(value)));return box;}));
    const current=intents.filter(item=>["running","suspended"].includes(item.status));
    el("agent-frontier").replaceChildren(...current.map(item=>node("p",`${statusLabels[item.status] || item.status} · ${item.description} · ${short(item.intent_id)}`)));
    if (!current.length) el("agent-frontier").append(node("p",progress ? "当前没有执行中的 Intent；以持久状态为准。" : "暂无已提交的 Agent 进展。","competition-note"));
    const result=data.run?.result || {}, feedback=result.competition_submission;
    const feedbackLabels={accepted:"平台确认整题完成",partial:"部分 Flag 正确，继续原任务",rejected:"候选被判错，继续原任务",blocked:"提交被平台拒绝，待处理",unknown:"提交结果未知，未自动重放"};
    const candidates=progress?.answer_candidates;
    el("candidates").textContent=Array.isArray(candidates) ? candidates.length ? `受保护候选 ${candidates.length} 条 · ${candidates.slice(-3).map(item=>`${short(item.candidate_id)} ${feedbackLabels[item.validation_state] || ({pending:"待验证",conflict:"反馈冲突"})[item.validation_state] || item.validation_state}`).join("；")}` : "当前尚无受保护候选。" : "候选状态尚未提供；请以完整任务证据为准。";
    el("feedback").textContent=feedback ? `上次平台回执：${feedbackLabels[feedback.state] || feedback.state}${feedback.correct_flag_count!==undefined ? ` · ${feedback.correct_flag_count} / ${feedback.total_flag_count}` : ""}` : "尚无可显示的平台提交回执；Agent 自述完成不代表整题成功。";
    const recovery=progress?.recovery || result.recovery;
    el("recovery").textContent=recovery?.required ? `待核验：${errors[recovery.reason] || recovery.reason || "技术状态尚未确认"}` : "";
    records=(data.events || []).slice(-80).reverse().map(event=> {const op=event.details?.operation || event.type;return {category:category(op),at:event.timestamp,label:`#${event.sequence} ${op} · TaskRun ${statusLabels[event.status] || event.status || "已提交"}`};});
    for (const key of ["business_diagnostics","model_format_diagnostics","http_diagnostics","review_call_diagnostics","errors"]) for (const item of progress?.[key] || []) {
      const code=item.code || item.reason_code; if (typeof code==="string") records.unshift({category:category(code),label:code});
    }
    if(feedback) records.unshift({category:"platform",label:feedbackLabels[feedback.state] || feedback.state});
    records=records.slice(0,100);renderRecords();
  }
  async function scheduleCommand(value) {
    if (!session || sessionBusy) return;
    revision += 1; sessionBusy = true; el("schedule-error").textContent = ""; sessionControls();
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
    revision += 1; sessionBusy = true; sessionControls();
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
    el("question-picker").addEventListener("change",()=> {selectedRemote=el("question-picker").value;render({preview:{items:previewItems}});});
    el("queue").addEventListener("input",renderQueue);
    el("record-filter").addEventListener("change",renderRecords);
    el("schedule-form").addEventListener("submit",startSession);
    el("fill-queue").addEventListener("click",()=> {el("queue").value = current?.adapter === "benchmark" ? previewItems.filter(x=>!x.is_completed).map(x=>x.remote_ref).join("\n") : "";renderQueue();});
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
