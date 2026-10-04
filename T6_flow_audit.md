# T6 execution-flow audit (2026-09-25)

## Expected and observed paths

`Architecture.md`, `interface.md`, `protocol.md`, `restrict.md`, `TODO.md`, and
`Qinyuan_Agent_confirmed_flow.svg` describe one TaskRun and one Sandbox per
challenge, model-generated ordered Intents, sequential depth-first execution,
Policy-gated tools, recorded Execution, verified Fact, TaskRun goal evaluation,
and either a new Intent round or a final TaskResult. T6 acceptance requires a
real CLI-driven single-challenge run through that chain. T5 `run-intent` is a
separate, already frozen file-identification slice.

The workbench currently calls `Runner.start -> python -m agent run-intent --mode
real`. Its request contains one local attachment, one caller-provided Intent
copied from the challenge description, and only `file.inspect_file` permission.
`AgentApplication.execute` registers exactly that Intent and concludes with
`inconclusive / single_intent_slice_finished`. `DeepSeekModelAdapter` only
supports Plan generation and file-type actions. It rejects Intent generation.
The Agent's existing Controller, ToolGateway, Policy, MCP, evidence, and
file-type verifier are still in this path. The deviation is at the TaskRun
supervision and goal boundaries, not an alternate command executor in the UI.

## Confirmed gaps

1. **No complete T6 run:** no model-generated Intent list, depth-first
   multi-Intent supervisor, general Fact verifier, task GoalEvaluator, pending
   flag ledger, or final TaskResult is connected to the workbench or a T6 CLI.
   A successful T5 file-type Fact remains `overall_goal=not_evaluated`.
2. **Front-end run range is narrower than stored drafts:** the workbench saves
   0-16 attachments and an optional remote endpoint, but its current runner
   rejects every case except one local attachment and no endpoint. Upload has
   no application-level size limit, while T5 import rejects a file over 16 MiB.
3. **The displayed step limit does not govern tool execution:** the workbench
   passes it as `QINYUAN_RUNTIME_MODEL_TIMEOUT`; the T5 `ToolRequest` still
   uses its own fixed file-tool timeout. The displayed 60-second default is
   not yet an end-to-end per-step bound.
4. **A runnable Pwn binary needs a new isolation profile:** the current T2
   Sandbox creates `/workspace` as `noexec` tmpfs. The user confirmed running
   only a checksum-verified working copy of an attachment. Input must stay
   read-only to model-controlled processes; `/workspace/work` must be the
   only executable writable area. The current Sandbox also has
   `network_mode=none`, so it cannot access the supplied target. No target-only
   network isolation has been exercised. A Docker probe found that
   `put_archive` cannot seed even tmpfs in a `read_only=True` container;
   Docker returned `container rootfs is marked read-only`. A second probe,
   approved by the user, used a fixed trusted UID 0 import into root-owned
   tmpfs while ordinary commands ran as UID 10001. It proved that ordinary
   commands could read but neither chmod nor delete the input, could execute
   a copied `/bin/true` from `/workspace/work`, and could not execute one
   from the `noexec` parent. `T6TaskRunSandbox` now provides this separate
   filesystem profile. The real lifecycle probe created and destroyed that
   profile with exact-ID cleanup confirmed. This is not yet the production
   attachment importer or arbitrary bash execution path.
5. **T5 provider contract is broadened by the workbench bridge:**
   `agent_adapters.cli` now calls `DeepSeekSettings.from_runtime_env`, and a
   process with `QINYUAN_RUNTIME_PROVIDER=openai_compatible` can use a custom
   HTTPS endpoint even for `run-intent`. The frozen T5 v1 contract allows only
   the official DeepSeek endpoint. The workbench passes its API key through a
   child-process environment, so the wording "service-process memory only"
   needs to be reconciled with this implementation. There is no evidence of
   key persistence in SQLite or the request file from this audit.
6. **Existing workbench run-state errors:** before the local fixes, `start`
   left the run row at `starting` while the challenge was `running`, so
   immediate cancel failed; a second start during cancellation spawned an
   extra child before rejection. Exit codes 4 and 5 became archiveable
   `failed` rows, though Agent recovery or cleanup was still needed. A
   restarted workbench acknowledged cancel without an owned process or an
   Agent cancel request. The fixes now expose `recovery_required` and prevent
   these false transitions; the workbench still needs an explicit recovery
   operation before such runs can be resolved.

## Decisions and next slices

- Confirmed this turn: an Intent succeeds only after a predeclared checkable
  postcondition and matching Execution evidence are verified and committed.
  An unverified inference or candidate flag never becomes a verified Fact.
- Confirmed this turn: after all current and ancestor alternatives are
  exhausted, save classified failures and generate another round until the
  TaskRun deadline or cancellation. Model errors remain explicit; unknown
  external outcomes require explicit recovery before new dispatch.
- Confirmed this turn: add an explicitly authorized `sandbox.command` tool
  carrying argv, stdin, working directory, timeout, and output bound. The
  latest user decision is to support arbitrary bash after the Sandbox
  filesystem, network, and process-isolation boundary is actually verified.
  Policy checks each outer dispatch, not the internal commands in a script.
- Confirmed this turn: the selected Pwn path may execute a checksum-verified
  working copy of an attachment, while the original Sandbox input remains
  root-owned and read-only to all model-controlled commands. The user
  approved an import-only UID 0 fixed program with capabilities dropped;
  all model-controlled commands remain UID 10001.
- Confirmed this turn: the endpoint hostname may be re-resolved during the
  TaskRun. Each new target connection resolves the original hostname again;
  private, loopback, link-local and reserved resolved addresses are rejected.
  Exact literal-IP authorization is required for private labs. The Sandbox
  uses target.qinyuan at the authorized port. Real target isolation is still
  awaiting acceptance.
- First T6 tool slice: preserve T5 wire behavior; verify a separate T6 Sandbox
  profile for immutable input, executable work copies, target-only egress,
  cancellation, and cleanup. Only then add bash dispatch with Policy,
  execution record, bounded output, and deterministic unknown handling.
  Test against a controlled local target before a real challenge endpoint.
- Subsequent slices: durable Intent generation and DFS supervisor, general
  Fact/goal/TaskResult semantics, T6 CLI/recovery, then real Sandbox and
  selected-challenge acceptance. The chosen remote Pwn challenge cannot be
  used as proof until network and executable-workspace decisions are
  confirmed and tested. Platform flag submission remains outside this Agent.

## Direct-command slice update

`T6CommandGateway -> Policy -> T6TaskRunSandbox` now bypasses the generic Bash
MCP preparation path. Focused Controller/SQLite tests cover normal zero and
nonzero exits, byte evidence, exact replay, permission rejection, one confirmed
timeout retry, budget exhaustion before dispatch, environment failure, unknown
results, cancellation and interrupted evidence writes. An initial focused run
passed 33 tests. Later focused command/postcondition regression passed 29 tests.
The trusted postcondition evaluator fixes output-literal and file-SHA-256
checks before execution and returns separate satisfied, not_satisfied and
inconclusive results. The new independent T6 observation proof is rechecked
before one atomic Fact/Intent commit. It proves only the declared observation,
not candidate-answer correctness or TaskRun achievement. The workbench and
full multi-Intent runner do not yet call this path.

T6-2 verification on 2026-09-26: 38 focused checks passed, including confirmed
timeout retry, immutable proof replay, SQLite Fact/Intent rollback, corrupt or
missing evidence, cancellation/deadline rejection, and unknown digest process
creation blocking further admission. Full regression returned 558 passed,
1 skipped and 1 failed in the existing real local File MCP integration; the
single failing test passed in isolation, so the full run is not accepted as
all-green. Logs: `.test/t6-final/focused.log`, `regression.log`, `mcp.log` and
`build.log`. Wheel and sdist builds passed; real T6 solving remains unverified.

A real Docker local, network-disabled probe verified arbitrary Bash input and
binary output, nonzero exits including 124 and 137, timeout process cleanup,
reaping of background children, workspace reuse, bounded dual streams, trusted
file SHA-256, symlink rejection and exact-ID container removal. Evidence is at
`.test/t6-direct/docker-report.json`. The workbench still invokes T5
`run-intent`; neither remote target networking nor a T6 TaskResult is verified.

T6-3 internal update on 2026-09-26: Intent generation is now a separately
audited Turn-level model call. Its ordered candidates, fixed registration
mutation and context digest survive a restart; a completed result can register
the same Turn without calling the model again. An incomplete call requires
explicit recovery. The workbench still invokes T5 `run-intent`, so the
multi-Intent scheduling and end-to-end deviation identified above remains.
The offline full regression passed 566 tests with one skipped case; focused
protocol and schema-upgrade checks added afterward passed 21 tests. Final
wheel/sdist and focused Ruff/mypy checks passed. Real model Intent generation
and full challenge solving are still unverified.

T6-4 internal update on 2026-09-26: `RunSupervisor` now owns the committed
depth-first scheduling slice. It selects one pending Intent by Turn sequence
and priority, commits `running` before calling the narrow `IntentRunnerPort`,
and classifies only structured runner results. Complete target mismatches
continue sibling candidates; when all candidates fail, an explicit generator
may register a new independent Turn using committed Fact and failure
summaries. Unfinished executions or incomplete receipts return
`recovery_required` and leave the running Intent/Turn untouched; only
completed executions whose postconditions cannot be decided become
`inconclusive` and stop dispatch. Model errors preserve a bounded reason in
the audit/failure summary, leave Intent/Turn/TaskRun unchanged, and stop
dispatch without trying siblings. Re-entering an inconclusive Turn remains
paused and cannot generate a new Turn. The audited generation adapter reuses
stored candidate results and model errors without recalling the provider.
Focused scheduler, generator, state, and store tests passed 157 cases in a
writable Windows test directory; the two new replay regressions passed in
their respective 13- and 12-case suites. Full regression finished with 583
passed, one skipped, and one failed: an existing T5 subprocess cancellation
test did not start its model call within 15 seconds; its isolated rerun passed.
Scoped Ruff, mypy, compileall, diff check, and sdist/wheel build passed. The current
workbench still invokes the T5 single-Intent process; no real Bash/Pwn runner,
GoalEvaluator, TaskResult, or end-to-end T6 run is accepted by this slice.

## Verification boundary

The audit probe at `.test/t6-flow-audit/evidence-*/report.json` used fake
child processes and no model, Docker, or target access. It reproduced the four
workbench run-state faults above. After the fixes, the focused workbench suite
passed `8` tests in the elevated Windows test environment. The same existing
three workbench tests failed in the restricted environment on temporary
directory creation/cleanup with `WinError 5`; this is an environment
limitation, not evidence that the real Agent chain works. No T6 solving run or
network isolation has been accepted.

The real local filesystem probe report is at
`.test/t6-flow-audit/docker-isolation-report.json`. Its final run exercised
`T6TaskRunSandbox.create/destroy`, trusted fixed-code import, ordinary non-root
read/write/execute checks, `network_mode=none`, `CapDrop=ALL`, and confirmed
container removal. It did not import actual challenge attachments, enable
remote networking, run arbitrary bash, or prove a T6 TaskResult.
