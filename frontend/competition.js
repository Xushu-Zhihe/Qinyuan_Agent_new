"use strict";

const CompetitionPage = (() => {
  const el = (id) => document.getElementById(`competition-${id}`);
  const endpoint = "/api/competition/profiles";
  const errors = {
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
  let profiles = [], current = null, initialized = false, busy = false, dirty = true;
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; };
  const status = (text) => { el("status").textContent = text; };
  function controls() {
    el("fields").disabled = busy;
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
      if (item.attachment) card.append(node("p", `附件：${item.attachment.name || "未命名"}（仅预览，未下载）`));
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
  document.addEventListener("DOMContentLoaded", () => {
    el("form").addEventListener("submit", save);
    el("profile").addEventListener("change", () => select(profiles.find(p => p.id === el("profile").value) || null));
    el("new").addEventListener("click", () => select(null));
    el("form").addEventListener("input", () => { dirty = true; resetPreview(); credentialLabel(); controls(); status("配置尚未保存；保存后再测试连接。"); });
    el("adapter").addEventListener("change", () => { el("url").value = el("adapter").value === "arena" ? "https://www.nssctf.cn" : ""; credentialLabel(); });
    el("check").addEventListener("click", check); el("clear").addEventListener("click", clear);
  });
  return {load};
})();
