"use strict";
const $ = (id) => document.getElementById(id);
let settings, templates = [], profiles = [], adapter = "arena", profileId = "", downloadPoll, securityPoll, closePoll;
let busy = false, connected = false;
const labels = {passed:"通过",failed:"失败",unknown:"待核验",not_applicable:"不适用"};
const messages = {
  "settings.configuration_changed":"配置文件已被其他程序修改，请重新打开启动器后再保存。",
  "settings.role_capacity_mismatch":"两个角色需要使用相同的已验证容量策略。",
  "settings.credential_protection_failed":"Windows 用户凭据加密或解密失败。",
  "settings.timeout_invalid":"超时必须是大于零的有限数值。",
  "download.environment_in_use":"请先停止工作台，再同步项目依赖。",
  "download.operation_in_progress":"有准备任务尚未完成或取消结果尚未确认。",
  "launcher.runs_still_active":"运行尚未停止，请等待收尾后再退出。",
  "launcher.schedules_still_active":"赛程尚未暂停，请等待控制回执。",
  "launcher.service_not_started":"工作台尚未启动。",
  "launcher.service_unreachable":"已有工作台暂时无法连接，请确认后台进程状态。",
  "launcher.instance_identity_mismatch":"工作台实例与当前项目或数据目录不匹配。",
  "environment.python_missing":"尚未准备项目 Python 环境，请前往下载模块准备依赖。",
  "d8_model_capacity_configuration_unsupported":"当前模型与地址组合尚未通过能力、容量验证。",
  "settings.background_format_invalid":"请选择 PNG、JPEG 或 WebP 格式的图片。",
  "settings.background_too_large":"背景图片超过 16 MiB，请选择较小的图片。",
  "settings.background_opacity_invalid":"背景图片不透明度须为 0–100%。",
};
function notice(text) { $("notice").textContent=text; $("notice").hidden=false; clearTimeout(notice.timer); notice.timer=setTimeout(()=>$("notice").hidden=true,6500); }
async function rpc(action, value) {
  if (!window.pywebview?.api) throw new Error("请通过 start-launcher.cmd 打开桌面启动器。");
  const result = await window.pywebview.api.invoke(action,value ?? null);
  if (!result.ok) throw new Error(`${messages[result.error] || "操作未完成"} [${result.category}/${result.error}]`);
  return result.data;
}
async function perform(task) {
  if (busy) return;
  busy=true;
  document.body.setAttribute("aria-busy","true");
  try { await task(); } catch(e) { notice(e.message); }
  finally { busy=false; document.body.removeAttribute("aria-busy"); }
}
function node(tag, text, className) { const n=document.createElement(tag); n.textContent=text; if(className)n.className=className; return n; }
const panes={start:["launch"],settings:["llm","agent","appearance","security"],checks:["basic","images","files"],downloads:["dependencies","build"]};
function navigate(tab, pane=panes[tab][0]) {
  document.querySelectorAll("[data-tab]").forEach(b=>{b.classList.toggle("active",b.dataset.tab===tab);b.setAttribute("aria-current",b.dataset.tab===tab?"page":"false");});
  for(const name of Object.keys(panes)) $("side-"+name).hidden=name!==tab;
  document.querySelectorAll("[data-page]").forEach(p=>p.hidden=p.dataset.page!==pane);
  document.querySelectorAll("[data-pane]").forEach(b=>{b.classList.toggle("selected",b.dataset.pane===pane);b.setAttribute("aria-current",b.dataset.pane===pane?"page":"false");});
  $("side-title").textContent={start:"启动准备",settings:"偏好设置",checks:"环境检测",downloads:"下载与构建"}[tab];
  $("download-console").hidden=tab!=="downloads";
  if(tab==="checks") perform(()=>runCheck(pane));
  if(pane==="security") refreshSecurity();
  if(tab==="downloads") pollDownload();
}
function applySettings(value) {
  settings=value;
  for(const field of document.querySelectorAll("[data-key]")) field.value=value.values[field.dataset.key] ?? "";
  $("shared-inner").checked=!value.values.QINYUAN_INNER_MODEL;
  toggleInner();
  for(const role of ["outer","inner"]) $(role+"-key-state").textContent=value.key_configured[role]?"已加密保存":"未配置";
  $("migration-card").hidden=!value.plaintext_key_present;
  previewOpacity();
  $("capacity-details").replaceChildren(...Object.entries(value.capacity||{}).flatMap(([role,p])=>[
    node("dt",role==="outer"?"Outer 容量":"Inner 容量"),node("dd",p?`上下文 ${p.total_units.toLocaleString()} · 输出预留 ${p.output_reserve_units.toLocaleString()}（估算 token 单位）`:"模型/端点容量尚未验证")
  ]));
}
function previewOpacity() {
  const opacity=Number($("background-opacity").value);
  $("background-opacity-value").value=opacity+"%";
  $("launcher-background").style.opacity=opacity/100;
}
async function showBackground(image) {
  const layer=$("launcher-background");
  if(image) {
    const preview=new Image();
    preview.src=image;
    try {await preview.decode();}
    catch (_) {
      layer.style.backgroundImage="none";document.body.classList.remove("has-background");
      $("clear-background").disabled=false;$("background-state").textContent="图片无法解码，请重新选择或移除。";
      return false;
    }
    layer.style.backgroundImage=`url("${image}")`;
  } else layer.style.backgroundImage="none";
  document.body.classList.toggle("has-background",Boolean(image));
  $("clear-background").disabled=!image;
  $("background-state").textContent=image?"已设置本地背景图片":"尚未设置背景图片";
  return true;
}
function toggleInner() { const shared=$("shared-inner").checked; $("inner-fields").hidden=shared; $("shared-description").hidden=!shared; $("inner-fields").querySelectorAll("[data-key]").forEach(field=>field.required=!shared); }
async function saveSettings() {
  const values={...settings.values};
  for(const field of document.querySelectorAll("[data-key]")) {
    if(field.closest("#inner-fields") && $("shared-inner").checked) continue;
    if(!field.reportValidity()) return false;
    values[field.dataset.key]=field.value.trim();
  }
  if($("shared-inner").checked) { values.QINYUAN_INNER_MODEL="";values.QINYUAN_INNER_BASE_URL=""; }
  values.QINYUAN_LAUNCH_PROFILE=profileId;
  const keys={outer:$("outer-key").value,inner:$("shared-inner").checked?"":$("inner-key").value};
  try { applySettings(await rpc("settings",{values,keys,revision:settings.revision})); notice("配置已保存；新运行使用新配置。"); return true; }
  finally { $("outer-key").value="";$("inner-key").value=""; }
}
function renderTemplates() {
  $("template-cards").replaceChildren(...templates.map(t=>{
    const b=node("button","","template-card"+(t.id===adapter?" active":""));b.type="button";b.setAttribute("aria-pressed",String(t.id===adapter));
    const icon=node("span",t.id==="arena"?"N":"T","template-icon"),text=node("span","");
    text.append(node("strong",t.name),node("small",t.id==="arena"?"平台领取 · 单题推进":"题目队列 · 1–3 槽并发"));
    b.append(icon,text,node("span",t.id===adapter?"✓":"","selection"));
    b.addEventListener("click",()=>selectProfile(null,t.id));return b;
  }));
  const t=templates.find(t=>t.id===adapter);
  $("template-note").textContent=t?.note||"";$("credential-label").textContent=t?.credential_label||"平台凭据";
  $("launch-concurrency").disabled=adapter==="arena";
  if(adapter==="arena")$("launch-concurrency").value="1";
}
function selectProfile(profile, type=profile?.adapter || adapter) {
  profileId=profile?.id||"";adapter=type;
  const list=profiles.filter(p=>p.adapter===adapter);
  $("profiles").replaceChildren(new Option("新建配置",""),...list.map(p=>new Option(p.name,p.id)));
  $("profiles").value=profileId;
  $("profile-name").value=profile?.name||"";$("profile-url").value=profile?.base_url||templates.find(t=>t.id===adapter)?.base_url||"";
  $("profile-timeout").value=profile?.timeout_seconds||10;$("profile-key").value="";
  $("profile-state").textContent=profile?.credential_configured?"凭据已加密保存":"尚未配置凭据";
  $("check-profile").disabled=$("clear-profile").disabled=!profileId;$("preview-summary").textContent="";
  renderTemplates();
}
async function loadProfiles() {
  profiles=await rpc("profiles");
  selectProfile(profiles.find(p=>p.id===profileId)||profiles.find(p=>p.id===settings.values.QINYUAN_LAUNCH_PROFILE)||profiles[0]||null);
  $("service-badge").textContent=connected?"工作台已连接":"工作台未启动";$("local-status").textContent=connected?"本地服务就绪":"配置准备就绪";
}
async function saveProfile() {
  if(!$("profile-form").reportValidity()) return;
  const value={id:profileId,name:$("profile-name").value.trim(),adapter,base_url:$("profile-url").value.trim(),timeout_seconds:Number($("profile-timeout").value)};
  if($("profile-key").value)value.credential=$("profile-key").value;
  try { const p=await rpc("profile_save",value);profileId=p.id;await loadProfiles();notice("连接配置已保存。"); }
  finally {$("profile-key").value="";}
}
async function runCheck(group) {
  const container=$("checks-"+group);container.replaceChildren(node("p","正在检测…","note"));
  const items=await rpc("check",group);
  container.replaceChildren(...items.map(v=>{
    const card=node("article","","check-card "+v.status),body=node("div","");
    body.append(node("strong",v.name),node("p",v.detail));
    card.append(node("span",{passed:"✓",failed:"×",unknown:"!"}[v.status]||"—","check-icon"),body,node("small",labels[v.status]||v.status));return card;
  }));
}
async function refreshSecurity() {
  clearTimeout(securityPoll);
  try {
    const s=await rpc("security");
    $("security-state").textContent=s.busy?"检查/操作中":s.verified?"已核验":"待核验";
    $("security-reason").textContent=s.status==="excluded"?"精确目录排除已核验生效。":s.status==="not_applicable"?"仅适用于 Windows。":`状态：${s.status} · ${s.reason}`;
    const values={"数据目录":s.data_dir,"精确规则":(s.patterns||[]).join("\n"),"证据访问":s.evidence_access?.status||"未检查","最近核验":s.checked_at||"尚未核验"};
    $("security-details").replaceChildren(...Object.entries(values).flatMap(([k,v])=>[node("dt",k),node("dd",v)]));
    document.querySelectorAll("[data-security]").forEach(b=>b.disabled=s.busy||!s.supported);
    if(s.busy)securityPoll=setTimeout(refreshSecurity,1500);
  } catch(e) {notice(e.message);}
}
async function pollDownload() {
  clearTimeout(downloadPoll);
  try {
    const s=await rpc("progress");
    const names={idle:"暂无准备任务",running:"正在准备",completed:"已完成并核验",needs_attention:"准备完成，核验待处理",failed:"准备失败",cancel_requested:"正在停止准备",cancelled_unverified:"已请求取消，服务端状态待核验"};
    $("download-state").textContent=(names[s.status]||s.status)+(s.error?` · ${s.error}`:"");
    $("download-progress").hidden=s.status!=="running";$("cancel-download").disabled=s.status!=="running";
    const output=[...(s.logs||[])];
    for(const c of s.checks||[])output.push(`[environment] ${c.name}: ${labels[c.status]||c.status} · ${c.detail}`);
    const filter=$("download-filter").value;
    $("download-logs").textContent=output.filter(line=>filter==="all"||line.startsWith(`[${filter}]`)).join("\n")||"此分类暂无记录。";
    document.querySelectorAll("[data-prepare]").forEach(b=>b.disabled=["running","cancel_requested","cancelled_unverified"].includes(s.status));
    if(["running","cancel_requested"].includes(s.status))downloadPoll=setTimeout(pollDownload,1000);
  } catch(e) {notice(e.message);}
}
window.launcherClose=()=>perform(async()=>{
  const s=await rpc("service_status");
  if(s.active || ["running","cancel_requested","cancelled_unverified"].includes((await rpc("progress")).status)) {
    $("close-dialog").showModal();return;
  }
  await rpc("stop_service");await rpc("close","background");
});
async function finishClose() {
  try { const s=await rpc("service_status");
    if(!s.active) {await rpc("stop_service");await rpc("close","background");return;}
    $("close-state").textContent="正在等待原运行控制确认停止。未知结果请在工作台核验；不会强制退出。";
    closePoll=setTimeout(finishClose,1500);
  } catch(e) {$("close-state").textContent=e.message;}
}
async function initialize() {
  try {
    const snapshot=await rpc("snapshot");templates=snapshot.templates;connected=snapshot.service;
    $("timeout-fields").replaceChildren(...Object.entries(snapshot.settings.timeouts).map(([key,v])=>{
      const label=node("label","","field"),span=node("span",v.label+` · ${v.unit}`),input=document.createElement("input");
      input.type="number";input.min="0.001";input.step="any";input.required=true;input.dataset.key=key;
      span.append(node("small",`默认 ${v.default}`));label.append(span,input);return label;
    }));
    applySettings(snapshot.settings);await showBackground(snapshot.background);selectProfile(null);await rpc("security","Probe");refreshSecurity();
    runCheck("basic").catch(e=>notice(e.message));
    try {await loadProfiles();$("launch-concurrency").value=adapter==="benchmark"?settings.values.QINYUAN_LAUNCH_CONCURRENCY:"1";}catch(e){notice(e.message);}
  } catch(e) {notice(e.message);}
}
document.addEventListener("click",e=>{
  const b=e.target.closest("button");if(!b)return;
  if(b.dataset.tab)navigate(b.dataset.tab);
  else if(b.dataset.pane)navigate(Object.keys(panes).find(t=>panes[t].includes(b.dataset.pane)),b.dataset.pane);
  else if(b.classList.contains("save-settings"))perform(saveSettings);
  else if(b.dataset.check)perform(()=>runCheck(b.dataset.check));
  else if(b.dataset.link)perform(()=>rpc("link",b.dataset.link));
  else if(b.dataset.security)perform(async()=>{await rpc("security",b.dataset.security);await refreshSecurity();});
  else if(b.dataset.prepare)perform(async()=>{await rpc("prepare",b.dataset.prepare);await pollDownload();});
  else if(b.classList.contains("clear-key"))perform(async()=>{applySettings(await rpc("clear_key",b.dataset.role));notice("密钥已清除。");});
});
$("shared-inner").addEventListener("change",toggleInner);
$("choose-background").addEventListener("click",()=>perform(async()=>{const selected=await rpc("choose_background");if(selected&&await showBackground(selected.image))notice("背景图片已保存。");}));
$("clear-background").addEventListener("click",()=>perform(async()=>{await rpc("clear_background");await showBackground(null);}));
$("background-opacity").addEventListener("input",previewOpacity);
let opacitySave=Promise.resolve();
$("background-opacity").addEventListener("change",()=>{
  const opacity=Number($("background-opacity").value);
  opacitySave=opacitySave.then(async()=>{
    const result=await rpc("background_opacity",{opacity,revision:settings.revision});
    settings.revision=result.revision;settings.values.QINYUAN_BACKGROUND_OPACITY=String(opacity);
  }).catch(e=>notice(e.message));
});
$("profiles").addEventListener("change",()=>selectProfile(profiles.find(p=>p.id===$("profiles").value)||null));
$("new-profile").addEventListener("click",()=>selectProfile(null));
$("profile-form").addEventListener("submit",e=>{e.preventDefault();perform(saveProfile);});
$("check-profile").addEventListener("click",()=>perform(async()=>{const v=await rpc("profile_check",profileId);$("preview-summary").textContent=`连接成功 · 已读取 ${v.preview?.items?.length??0} 道题目；未领取或启动题目。`;}));
$("clear-profile").addEventListener("click",()=>perform(async()=>{await rpc("profile_clear",profileId);await loadProfiles();}));
$("migrate-key").addEventListener("click",()=>perform(async()=>{applySettings(await rpc("migrate",settings.revision));notice("密钥已加密保存并移除明文键。");}));
$("launch-button").addEventListener("click",()=>perform(async()=>{if(await saveSettings())await rpc("open");}));
$("stop-service").addEventListener("click",()=>perform(async()=>{await rpc("stop_service");$("service-badge").textContent="工作台已停止";notice("空闲工作台已请求停止。");}));
$("cancel-download").addEventListener("click",()=>perform(async()=>{await rpc("cancel_prepare");await pollDownload();}));
$("download-filter").addEventListener("change",pollDownload);
$("copy-logs").addEventListener("click",()=>perform(async()=>{await navigator.clipboard.writeText($("download-logs").textContent);notice("已复制当前分类的诊断记录。");}));
$("keep-background").addEventListener("click",()=>perform(()=>rpc("close","background")));
$("pause-exit").addEventListener("click",()=>perform(async()=>{await rpc("close","pause");finishClose();}));
$("cancel-close").addEventListener("click",()=>{clearTimeout(closePoll);$("close-dialog").close();});
if(window.pywebview?.api)initialize();else window.addEventListener("pywebviewready",initialize,{once:true});
