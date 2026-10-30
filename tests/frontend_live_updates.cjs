"use strict";
// Optional browser check: QINYUAN_PLAYWRIGHT points to an existing installation.
// All API and asset requests are fulfilled locally; no user service is contacted.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");

(async () => {
  if (process.env.QINYUAN_EDGE) { edgeActivityAcceptance(); return; }
  const { chromium } = require(process.env.QINYUAN_PLAYWRIGHT || "playwright");
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const taskId = "11111111-1111-4111-8111-111111111111";
    const challengeId = "22222222-2222-4222-8222-222222222222";
    const run = { run_id: "33333333-3333-4333-8333-333333333333", status: "running", agent_task_run_id: taskId };
    let progress = {
      protocol_version: 3, contract_version: 22, mode: "real", sandbox_backend: "docker", task_run_id: taskId,
      goal_validation: { mode: "human" }, task_run: { goal: "Task-level final objective", status: "running", deadline_at: Date.now() / 1000 + 3600 },
      turns: [{ turn_id: "turn-one", planning_state: "active", intent: { description: "Find a useful result", completion_criterion: "Check supported result", support_refs: [] },
        roads: [{ road_id: "road-one", description: "First independent approach", status: "running" }], fact_refs: [] }],
      tool_operations: [{ operation_id: "tool-one", road_id: "road-one", status: "completed" }],
      verified_facts: [{ fact_id: "fact-one", ref: "verified_fact:fact-one:v1", turn_id: "turn-one", content: "A stable verified observation.", road_id: "road-one", source_refs: [], status: "verified" }],
      user_hints: [], retrospective: [], checkpoint: { sequence: 1 }, goal_evaluation: "not_evaluated", cleanup: {},
    };
    const challenge = { id: challengeId, title: "Offline UI acceptance", category: "web", endpoint: "", description: "controlled fixture",
      status: "running", deadline_minutes: 60, step_timeout_seconds: 60, attachments: [], run };
    let failHintResponse = true;
    const controls = [];
    const applied = new Map();
    const securityActions = [];
    let security = { supported: true, status: "unknown", reason: "not_checked", busy: false, verified: false, checked_at: null,
      data_dir: "C:\\Example Data\\workbench_data", pattern: "C:\\Example Data\\workbench_data\\runs\\*\\agent-state\\*\\stage-evidence", script_path: "C:\\Example App\\agent_adapters\\windows_defender_setup.ps1" };
    let securityPending = 0;
    let startedRuns = 0;
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.origin !== "http://127.0.0.1:44174") throw new Error("Unexpected network request: " + url.origin);
      if (url.pathname.startsWith("/api/")) {
        let body = {};
        if (url.pathname === "/api/health") body = { status: "ready", capabilities: { protocol_version: 3, execution_profile: "d8_v22", d7_mode: "real", real_sandbox_backend: "docker" } };
        else if (url.pathname === "/api/windows-security") {
          if (route.request().method() === "POST") {
            const action = route.request().postDataJSON().action;
            securityActions.push(action); securityPending = 2;
            security = { ...security, busy: true, verified: false, status: "unknown", action, reason: "awaiting_administrator_or_check" };
          } else if (security.busy && --securityPending === 0) {
            const excluded = security.action === "Apply";
            security = { ...security, busy: false, verified: true, status: excluded ? "excluded" : "not_excluded", reason: null,
              exact_rule_present: excluded, real_time_protection_enabled: true, tamper_protected: true, checked_at: "2026-10-29T12:00:00Z" };
          }
          body = security;
        }
        else if (url.pathname === "/api/challenges") body = [{ ...challenge, run: { ...run, result: progress } }];
        else if (url.pathname.endsWith("/run") && route.request().method() === "POST") { startedRuns++; body = { ...run, result: progress }; }
        else if (url.pathname.endsWith("/progress")) body = { run: { ...run, result: progress }, progress, events: [] };
        else if (url.pathname.endsWith("/operator-control")) {
          const command = route.request().postDataJSON(); controls.push(command);
          if (!applied.has(command.operation_id)) {
            applied.set(command.operation_id, command);
            if (command.action === "user_hint") progress.user_hints.push({ operation_id: command.operation_id, content: command.payload.content, created_at: Date.now() / 1000 });
            else progress.task_run.deadline_at += command.payload.seconds;
          }
          if (command.action === "user_hint" && failHintResponse) { failHintResponse = false; await route.abort("failed"); return; }
          body = { operation_id: command.operation_id, operator_control: { question_deadline_at: progress.task_run.deadline_at } };
        }
        await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) }); return;
      }
      const name = path.basename(url.pathname) || "index.html";
      const allowed = ["index.html", "app.js", "run-view.js", "styles.css", "run.css", "competition.js"];
      if (!allowed.includes(name)) { await route.fulfill({ status: 404, body: "" }); return; }
      let body = fs.readFileSync(path.join(root, "frontend", name), "utf8");
      if (name === "index.html") body = body.replace(/<script[^>]*src=["'][^"']*competition\.js[^"']*["'][^>]*><\/script>/g, "");
      await route.fulfill({ contentType: name.endsWith("js") ? "text/javascript" : name.endsWith("css") ? "text/css" : "text/html", body });
    });
    await page.goto("http://127.0.0.1:44174/#task/" + challengeId);
    await page.waitForSelector(".graph-node.graph-intent");
    await page.click("#run-tab-evidence");
    for (let index = 0; index < 5; index++) await page.click("#graph-zoom-in");
    await page.evaluate(() => {
      document.getElementById("run-graph").scrollLeft = 60;
      window.savedPan = document.getElementById("run-graph").scrollLeft;
      window.savedZoom = document.getElementById("graph-zoom-level").textContent;
    });
    assert.ok(await page.evaluate(() => savedPan > 0));
    await page.evaluate(() => {
      window.savedNodes = { road: document.querySelector(".graph-intent"), fact: document.querySelector(".run-fact"),
        event: document.querySelector("#run-timeline details"), card: document.querySelector(".task-card") };
      savedNodes.event.open = true;
      const selection = window.getSelection(); const range = document.createRange();
      range.selectNodeContents(savedNodes.fact.querySelector(".fact-content")); selection.removeAllRanges(); selection.addRange(range);
      document.getElementById("user-hint-content").value = "Draft survives polling";
    });
    assert.equal(await page.evaluate(() => String(window.getSelection())), "A stable verified observation.");
    await page.evaluate(() => refreshRun(state.selected));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    assert.deepEqual(await page.evaluate(() => [document.getElementById("run-graph").scrollLeft,
      document.getElementById("graph-zoom-level").textContent]), await page.evaluate(() => [savedPan, savedZoom]));
    assert.ok(await page.evaluate(() => {
      const edge = document.querySelector(".graph-edge:not(.sequence)");
      const end = edge.getPointAtLength(edge.getTotalLength());
      const point = new DOMPoint(end.x, end.y).matrixTransform(edge.getScreenCTM());
      const target = document.querySelector(".graph-turn").getBoundingClientRect();
      return Math.abs(point.x - target.left) < 2 && Math.abs(point.y - target.top - target.height / 2) < 2;
    }));
    assert.deepEqual(await page.evaluate(() => ({
      road: savedNodes.road === document.querySelector(".graph-intent"),
      fact: savedNodes.fact === document.querySelector(".run-fact"),
      event: savedNodes.event === document.querySelector("#run-timeline details"),
      card: savedNodes.card === document.querySelector(".task-card"),
      open: savedNodes.event.open, selection: String(window.getSelection()), draft: document.getElementById("user-hint-content").value,
    })), { road: true, fact: true, event: true, card: true, open: true, selection: "A stable verified observation.", draft: "Draft survives polling" });
    progress.turns[0].roads[0].status = "succeeded";
    progress.turns[0].roads.push({ road_id: "road-two", description: "Another distinct approach", status: "pending" });
    progress.verified_facts.push({ fact_id: "fact-two", ref: "verified_fact:fact-two:v1", turn_id: "turn-one", content: "New independent observation", road_id: "road-one", status: "verified" });
    await page.evaluate(() => refreshRun(state.selected));
    assert.equal(await page.locator(".graph-intent").count(), 2);
    assert.equal(await page.evaluate(() => savedNodes.road === document.querySelector(".graph-intent")), true);
    assert.match(await page.locator(".task-pipe").innerText(), /2\s*Road/);
    assert.match(await page.locator(".graph-intent").first().innerText(), /已通过/);
    assert.equal(await page.evaluate(() => savedNodes.fact === document.querySelector("#run-facts .run-fact")), true);
    await page.click("#run-tab-evidence");
    await page.fill("#user-hint-content", "Check dependency version first.");
    await page.click("#user-hint-submit");
    await page.waitForFunction(() => document.getElementById("user-hint-submit").textContent === "确认原提交");
    await page.click("#user-hint-submit");
    await page.waitForFunction(() => document.getElementById("user-hint-content").value === "");
    assert.equal(controls.filter(c => c.action === "user_hint").length, 2);
    assert.equal(controls[0].operation_id, controls[1].operation_id);
    assert.equal(progress.user_hints.length, 1);
    assert.match(await page.locator("#run-user-hints").innerText(), /Check dependency version first/);
    const before = progress.task_run.deadline_at;
    await page.fill("#extend-run-minutes", "30"); await page.click("#extend-run");
    await page.waitForFunction(() => !document.getElementById("extend-run-minutes").disabled);
    assert.equal(progress.task_run.deadline_at, before + 1800);
    assert.equal(controls.filter(c => c.action === "extend_question").length, 1);

    // Differentiate curated outcomes, provenance and references across a real
    // multi-stage shape; scope changes are presentation only, never API writes.
    const ancestor = progress.verified_facts[0].ref;
    Object.assign(progress.turns[0], { planning_state: "waiting_child", fact_refs: [ancestor],
      achievement: { completion: { reason: "Root deliverable is recorded.", support_refs: [ancestor] } } });
    progress.turns.push({ turn_id: "turn-two", parent_turn_id: "turn-one", planning_state: "active",
      intent: { description: "Child deliverable goal", completion_criterion: "Child completion criterion", support_refs: [ancestor] },
      fact_refs: [ancestor, "verified_fact:fact-three:v1", "verified_fact:fact-three:v0"],
      roads: [{ road_id: "child-a", description: "Method A independent hypothesis", status: "running", support_refs: [ancestor, "progress:raw-observation:v1"] },
        { road_id: "child-b", description: "Method B alternative hypothesis", status: "failed", support_refs: [] },
        { road_id: "child-c", description: "Method C not yet executed", status: "pending", support_refs: [] }] });
    progress.verified_facts.push(
      { fact_id: "fact-three", ref: "verified_fact:fact-three:v1", turn_id: "turn-two", road_id: "child-a", content: "Selected child result", current_status: "effective", source_status: "available", source_refs: ["progress:tool-child-a:v1"] },
      { fact_id: "fact-four", ref: "verified_fact:fact-four:v1", turn_id: "turn-two", road_id: "child-a", content: "Unselected child observation", current_status: "effective", source_status: "available" },
      { fact_id: "fact-five", ref: "verified_fact:fact-five:v1", turn_id: "turn-two", road_id: "child-b", content: "Withdrawn result from method B", current_status: "withdrawn", source_status: "available" });
    progress.tool_operations.push({ operation_id: "tool-child-a", road_id: "child-a", status: "completed" });
    progress.retrospective = [
      { operation_id: "root-review", turn_id: "turn-one", road_id: null, phase: "review", status: "completed" },
      { operation_id: "child-turn-review", turn_id: "turn-two", road_id: null, phase: "review", status: "completed" },
      { operation_id: "child-a-review", turn_id: "turn-two", road_id: "child-a", phase: "fact_review", status: "completed" },
      { operation_id: "child-b-review", turn_id: "turn-two", road_id: "child-b", phase: "review", status: "completed" },
      { operation_id: "legacy-review", phase: "review", status: "completed" }];
    await page.evaluate(async () => { await refreshRun(state.selected); clearTimeout(runPoll); });
    await page.locator('.graph-turn[data-key="turn-turn-two"]').click();
    assert.match(await page.locator("#run-detail").innerText(), /Turn.Intent · 阶段目标[\s\S]*Child deliverable goal/i);
    assert.deepEqual(await page.locator("#run-detail .run-fact[data-fact-id]").evaluateAll(items => items.map(el => el.dataset.factId)), ["fact-one", "fact-three"]);
    assert.equal(await page.locator('#run-detail .run-fact[data-ref="verified_fact:fact-three:v0"] .fact-content').innerText(), "引用版本未在当前快照中提供，暂不可解析正文。");
    assert.match(await page.locator('#run-detail .run-fact[data-fact-id="fact-one"]').innerText(), /来源：Turn 1 \/ Road 1/);
    await page.click("#run-tab-evidence");
    assert.deepEqual(await page.locator("#run-facts .run-fact[data-fact-id]").evaluateAll(items => items.map(el => el.dataset.factId)), ["fact-one", "fact-three"]);
    await page.click("#run-tab-log");
    assert.deepEqual(await page.locator("#run-timeline details").evaluateAll(items => items.map(el => el.dataset.key)),
      ["tool-tool-child-a", "review-child-turn-review", "review-child-a-review", "review-child-b-review"]);

    await page.locator('.graph-intent[data-key="intent-child-a"]').click();
    assert.match(await page.locator("#run-detail").innerText(), /Road · 解题思路[\s\S]*Method A independent hypothesis/i);
    assert.doesNotMatch(await page.locator("#run-detail").innerText(), /Method B alternative hypothesis/);
    assert.deepEqual(await page.locator("#run-detail .fact-group").first().locator(".run-fact").evaluateAll(items => items.map(el => el.dataset.factId)), ["fact-three", "fact-four"]);
    assert.deepEqual(await page.locator("#run-detail .fact-group").nth(1).locator(".run-fact").evaluateAll(items => items.map(el => el.dataset.factId)), ["fact-one"]);
    assert.doesNotMatch(await page.locator('.graph-intent[data-key="intent-child-a"]').getAttribute("title"), /短 Plan/);
    if (process.env.QINYUAN_UI_PREVIEW) {
      await page.click("#graph-fit");
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
      await page.screenshot({ path: process.env.QINYUAN_UI_PREVIEW });
    }
    await page.click("#run-tab-log");
    assert.deepEqual(await page.locator("#run-timeline details").evaluateAll(items => items.map(el => el.dataset.key)), ["tool-tool-child-a", "review-child-a-review"]);
    assert.match(await page.locator("#run-timeline").innerText(), /部分审核记录未提供归属/);
    await page.click("#run-tab-evidence");
    await page.locator('#run-facts .run-fact[data-fact-id="fact-one"] .fact-origin').click();
    assert.equal(await page.locator("#run-scope-label").innerText(), "Turn 1 / Road 1");
    assert.deepEqual(await page.locator("#run-detail .fact-group").first().locator(".run-fact").evaluateAll(items => items.map(el => el.dataset.factId)), ["fact-one", "fact-two"]);

    await page.locator('.graph-intent[data-key="intent-child-b"]').click();
    assert.match(await page.locator("#run-detail .run-fact").innerText(), /已撤回[\s\S]*Withdrawn result from method B/);
    await page.locator('.graph-intent[data-key="intent-child-c"]').click();
    assert.equal(await page.locator("#run-detail .run-fact").count(), 0);
    assert.match(await page.locator("#run-detail").innerText(), /Method C not yet executed/);
    await page.locator(".graph-task").click();
    assert.match(await page.locator("#run-detail").innerText(), /TaskRun · 任务总览[\s\S]*Task-level final objective/);
    assert.match(await page.locator('.graph-turn[data-key="turn-turn-one"]').innerText(), /等待后继[\s\S]*阶段已达成/);
    await page.click("#run-tab-evidence");
    assert.equal(await page.locator("#run-facts .run-fact").count(), 5);
    await page.locator('.graph-intent[data-key="intent-child-b"]').click();
    progress.turns[1].roads = progress.turns[1].roads.filter(r => r.road_id !== "child-b");
    await page.evaluate(async () => { await refreshRun(state.selected); clearTimeout(runPoll); });
    assert.match(await page.locator("#run-scope-label").innerText(), /所选节点未在当前快照/);
    assert.equal(await page.locator("#run-detail .run-fact").count(), 0);
    await page.click("#run-tab-evidence"); await page.click("#run-scope-all");
    assert.equal(await page.locator("#run-tab-evidence").getAttribute("aria-selected"), "true");
    assert.equal(await page.locator("#run-facts .run-fact").count(), 5);
    await page.click("#run-tab-log");
    assert.equal(await page.locator('[data-key="review-legacy-review"]').count(), 1);
    await page.waitForFunction(() => state.windowsSecurity?.supported);
    assert.deepEqual(securityActions, []); // Opening a page must never request elevation.
    assert.equal(await page.locator("#windows-security-hint").isVisible(), true);
    await page.click('#windows-security-hint a');
    assert.equal(await page.locator("#security-view").isVisible(), true);
    assert.match(await page.locator("#security-status").innerText(), /无法确认/);
    assert.equal(await page.locator("#security-pattern").innerText(), security.pattern);
    security = { ...security, reason: "script_execution_blocked", error_type: "PSSecurityException" };
    await page.evaluate(() => refreshWindowsSecurity());
    assert.match(await page.locator("#security-status").innerText(), /脚本策略阻止.*尚未执行 Defender/);
    assert.match(await page.locator("#security-reason").innerText(), /管理员权限不会自动解除/);
    assert.match(await page.locator("#security-diagnostic").innerText(), /script_execution_blocked.*PSSecurityException/);
    security = { ...security, reason: "helper_result_read_failed", error_type: "PermissionError", errno: 13 };
    await page.evaluate(() => refreshWindowsSecurity());
    assert.match(await page.locator("#security-reason").innerText(), /无法读取检查回执/);
    assert.match(await page.locator("#security-diagnostic").innerText(), /helper_result_read_failed.*PermissionError.*errno=13/);
    await page.click("#security-apply");
    assert.equal(await page.locator("#security-apply").isDisabled(), true);
    await page.waitForFunction(() => document.getElementById("security-status").textContent.includes("排除已生效"));
    assert.deepEqual(securityActions, ["Apply"]);
    assert.match(await page.locator("#security-rule").innerText(), /^存在$/);
    await page.click("#security-remove");
    await page.waitForFunction(() => document.getElementById("security-status").textContent.includes("未被排除"));
    assert.deepEqual(securityActions, ["Apply", "Remove"]);
    assert.match(await page.locator("#security-reason").innerText(), /已撤销/);
    await page.evaluate(() => { showView("detail"); renderRun({ status: "finished", run_id: "offline-finished" }); });
    assert.equal(await page.locator("#windows-security-hint").isVisible(), true);
    await page.click("#start-run");
    assert.equal(startedRuns, 1); // Missing exclusion is advisory, never a run gate.
    security = { ...security, supported: false, status: "not_applicable", verified: false, reason: "windows_only" };
    await page.click('#windows-security-hint a');
    await page.waitForFunction(() => document.getElementById("security-check").disabled);
    assert.match(await page.locator("#security-status").innerText(), /不在 Windows/);
    assert.deepEqual(errors, []);
    console.log("PASS incremental DOM and controls; Turn/Road/Fact scopes; Windows security page, explicit operations, live completion, nonblocking start");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

function edgeActivityAcceptance() {
  // Native browser fallback for the read-only view; no packages or API requests.
  const { spawnSync } = require("node:child_process");
  const { pathToFileURL } = require("node:url");
  const directory = path.resolve(process.env.QINYUAN_UI_TEST_DIR);
  fs.mkdirSync(directory, { recursive: true });
  function checkActivity() {
    const check = (condition, message) => { if (!condition) throw new Error(message); };
    const get = id => document.getElementById(id);
    const keyed = (id, key) => Array.from(get(id).querySelectorAll("[data-key]")).find(el => el.dataset.key === key);
    const run = { run_id: "offline-run", status: "running" };
    const progress = {
      protocol_version: 3, contract_version: 22, mode: "simulated", task_run_id: "offline-task",
      goal_validation: { mode: "human" }, task_run: { goal: "Offline goal", status: "running" },
      turns: [{ turn_id: "parent", planning_state: "waiting_child", review_state: "accepted", intent: { description: "Parent goal", completion_criterion: "Parent evidence" }, roads: [], fact_refs: [] },
        { turn_id: "child", parent_turn_id: "parent", planning_state: "active", review_state: "pending", intent: { description: "Child goal", completion_criterion: "Child evidence" },
          roads: [{ road_id: "road", description: "Current approach", status: "running", review_state: "pending" }], fact_refs: [] }],
      verified_facts: [{ fact_id: "formal", ref: "verified_fact:formal:1", turn_id: "child", road_id: "road", content: "Registered observation", current_status: "effective" }],
      review_queue: [], branches: [], inner_activity: [], retrospective: [], user_hints: [],
    };
    const target = { turn_id: "child", road_id: "road" };
    const review = (id, state, extra = {}) => ({ proposal_operation_id: id, state, kind: "fact_candidates", targets: [target], author: { scope: "road_execution", owner_id: "road" }, attempt_ids: [], candidates: [], ...extra });
    progress.review_queue = [
      review("intent", "pending", { kind: "turn_intent", targets: [{ turn_id: "child", road_id: null }], author: { scope: "turn_planning", owner_id: "parent" }, attempt_ids: ["active"] }),
      review("saved", "pending", { attempt_ids: ["saved-review"] }),
      review("partial", "settled", { decision: "accept", review_operation_id: "batch-review", candidates: [
        { index: 0, content: "Registered observation", decision: "accept", fact_id: "formal", support_refs: ["progress:tool:1"] },
        { index: 1, content: "<img src=x onerror=alert(1)>", decision: "reject", reason: "unsupported", support_refs: [] },
        { index: 2, content: "Reviewed hypothesis", inferred: true, decision: "accept", support_refs: [] },
      ] }), review("blocked", "blocked"), review("obsolete", "superseded", { review_operation_id: "upstream-rejection" }),
    ];
    progress.retrospective = [{ operation_id: "active", status: "unsettled", stage: "stage_review" }, { operation_id: "saved-review", status: "completed", stage: "stage_review" }];
    progress.inner_activity = [{ ...target, operation_id: "cancelled", stage: "stage_action", disposition: "discarded", disposition_ref: "batch-review" }];
    progress.branches = [{ ...target, turn_id: "parent", road_id: null, branch_id: "root-prefix", kind: "continuation", current: true, proposal_operation_id: "intent" },
      { ...target, branch_id: "road-prefix", parent_branch_id: "root-prefix", kind: "inheritance", current: false, proposal_operation_id: "roads" },
      { ...target, branch_id: "correction", parent_branch_id: "road-prefix", kind: "correction", current: true, proposal_operation_id: "partial", review_operation_id: "batch-review" }];
    get("run-panel").hidden = false;
    document.body.classList.add("detail-open");
    document.querySelectorAll(".view").forEach(el => { el.hidden = !el.contains(get("run-panel")); });
    for (let el = get("run-panel").parentElement; el; el = el.parentElement) el.hidden = false;
    RunView.render({ run, progress });
    get("run-tab-activity").click();
    check(!get("run-pane-activity").hidden, "activity tab unavailable");
    check(document.querySelectorAll(".review-badge").length >= 3, "missing pending/accepted plan badges");
    check(document.querySelector(".graph-intent").textContent.includes("运行中") && document.querySelector(".graph-intent").textContent.includes("待审"), "execution and approval conflated");
    check(keyed("run-review-queue", "review-proposal/intent").textContent.includes("未结算"), "unsettled review hidden");
    check(keyed("run-review-queue", "review-proposal/saved").textContent.includes("结果已保存 · 待应用"), "saved result incorrectly applied");
    const partial = keyed("run-review-queue", "review-proposal/partial"); partial.open = true;
    check(partial.textContent.includes("部分接受") && partial.textContent.includes("仍不是正式 Fact"), "partial/inferred verdict missing");
    const decisions = Array.from(partial.querySelectorAll(".review-candidate > strong"), el => el.textContent);
    check(decisions[0].includes("已接受") && decisions[1].includes("已拒绝") && decisions[2].includes("已接受"), "per-item decisions mislabeled");
    check(!partial.querySelector("img"), "candidate content interpreted as HTML");
    check(!get("run-facts").textContent.includes("Reviewed hypothesis") && !get("run-facts").textContent.includes("<img"), "pending claims became official Facts");
    check(keyed("run-review-queue", "review-proposal/obsolete").textContent.includes("使提案失效的审核"), "supersession confused with own verdict");
    check(get("run-discarded").textContent.includes("生成未采用"), "discarded generation missing");
    const text = partial.querySelector(".review-candidate p").firstChild;
    const range = document.createRange(); range.selectNodeContents(text);
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    const selected = selection.toString();
    progress.checkpoint = { sequence: 2 };
    RunView.render({ run, progress: structuredClone(progress) });
    check(keyed("run-review-queue", "review-proposal/partial") === partial && partial.open, "poll rebuilt/opened node lost");
    check(selection.toString() === selected, "poll lost text selection");
    document.querySelector(".graph-intent").click(); get("run-tab-activity").click();
    check(keyed("run-review-queue", "review-proposal/intent"), "successor Intent filtered by author instead of subject");
    check(!keyed("run-branches", "branch/root-prefix"), "Road scope leaks parent branch");
    keyed("run-branches", "branch/road-prefix").querySelector("button").click();
    check(keyed("run-branches", "branch/root-prefix").open && !get("run-pane-activity").hidden, "parent navigation failed");
    check(!keyed("run-review-queue", "review-proposal/intent"), "parent scope shows child target");
    document.querySelector(".graph-task").click(); get("run-tab-activity").click();
    progress.review_queue.push({ proposal_operation_id: "old-snapshot", state: "pending" });
    RunView.render({ run, progress });
    check(keyed("run-review-queue", "review-proposal/old-snapshot").textContent.includes("归属未提供"), "older metadata not handled explicitly");
    progress.goal_validation = { mode: "human", candidate_id: "answer", confirmation_deadline: Date.now() / 1000 + 300, timeout_action: "assume" };
    progress.answer_candidates = [{ candidate_id: "answer", evidence_version: "version", source_ref: "mcp-result/source", validation_state: "pending" }];
    progress.task_run.status = "awaiting_confirmation";
    RunView.render({ run, progress });
    const confirmation = get("run-confirmation");
    check(!confirmation.hidden && get("run-pane-evidence").hidden, "human controls hidden behind a tab");
    check(confirmation.textContent.includes("默认确定") && confirmation.textContent.includes("candidate-read"), "timeout rule or candidate reference missing");
    const buttons = Array.from(confirmation.querySelectorAll("button"));
    check(buttons.length === 2 && buttons[0].textContent === "确定" && buttons[1].textContent === "错误", "human controls must be determine/error");
    const commands = [];
    window.addEventListener("stage-human-decision", event => commands.push(event.detail.selection));
    buttons[1].click();
    RunView.render({ run, progress: structuredClone(progress) });
    check(confirmation.querySelectorAll("button")[1] === buttons[1], "confirmation poll rebuilt button");
    buttons[1].disabled = false; buttons[1].click();
    check(commands.length === 2 && commands[0].action === "continue" && commands[0].candidate_id === "answer" && commands[0].operation_id === commands[1].operation_id, "wrong/retry decision identity changed");
    buttons[0].click(); check(commands.at(-1).action === "accept", "determine not routed to original accept control");
    progress.goal_validation.mode = "external";
    RunView.render({ run, progress }); check(confirmation.hidden, "external validation exposed human controls");
    progress.goal_validation.mode = "human"; progress.goal_validation.timeout_action = "end_unverified";
    RunView.render({ run, progress }); check(confirmation.textContent.includes("原规则"), "historical timeout falsely shown as assume");
    progress.task_run.status = "assumed_achieved"; progress.task_run.reason = "human_confirmation_timeout_assumed";
    RunView.render({ run, progress });
    check(!confirmation.hidden && !confirmation.querySelector("button") && confirmation.textContent.includes("未伪造"), "default determination lost its provenance");
    get("run-dashboard").scrollIntoView({ block: "start" });
    return "PASS native Edge: review/branch scopes, DOM preservation; prominent human determine/error controls, stable retry IDs, external isolation, timeout provenance";
  }
  let html = fs.readFileSync(path.join(root, "frontend/index.html"), "utf8")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, "")
    .replace("<head>", `<head><base href="${pathToFileURL(path.join(root, "frontend") + path.sep).href}"><style>* { animation: none !important; transition: none !important; scroll-behavior: auto !important; }</style>`);
  const source = fs.readFileSync(path.join(root, "frontend/run-view.js"), "utf8");
  html = html.replace("</body>", `<script>${source}</script><script>
    document.addEventListener('DOMContentLoaded', () => {
      let result;
      try { result = { ok: true, message: (${checkActivity.toString()})() }; }
      catch (error) { result = { ok: false, message: error.stack }; }
      const output = document.createElement('pre'); output.id = 'a4-result';
      output.textContent = encodeURIComponent(JSON.stringify(result)); document.body.append(output);
    });</script></body>`);
  const fixture = path.join(directory, "activity.html"); fs.writeFileSync(fixture, html);
  const output = spawnSync(process.env.QINYUAN_EDGE, ["--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-background-networking", "--disable-component-update", `--user-data-dir=${path.join(directory, "profile")}`, "--dump-dom", "--virtual-time-budget=5000", "--window-size=1600,1100", ...(process.env.QINYUAN_UI_PREVIEW ? [`--screenshot=${process.env.QINYUAN_UI_PREVIEW}`] : []), pathToFileURL(fixture).href], { encoding: "utf8", windowsHide: true, timeout: 45000, maxBuffer: 16 * 1024 * 1024 });
  const match = output.stdout?.match(/<pre id="a4-result">([^<]+)<\/pre>/);
  assert.ok(match, `Edge did not return acceptance result (${output.status}): ${output.error?.message || output.stderr?.slice(-1500)}`);
  const result = JSON.parse(decodeURIComponent(match[1])); assert.ok(result.ok, result.message);
  console.log(result.message);
}
