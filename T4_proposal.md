# T4 单 Intent 执行契约

日期：2026-09-24。本文汇总用户逐项确认的 T4 限制；替代早期草案中仍标为“待确认”的对应建议。实现已接通，最终验证记录见 `TODO.md`。真实模型、真实 Docker、本地 CLI 和完整 TaskRun 调度不在本轮验收范围。

## 最小链路与状态所有权

```text
可信附件导入 → Intent.running → 持久目标绑定
→ Fake Model 单步骤 Plan → 持久 Plan 声明 → Controller 注册/启动
→ 模型动作 → 持久动作声明 → ToolGateway → Policy → 本机 File MCP
→ 同一 Fake Docker Sandbox → 持久 Execution → Controller 完成 Plan
→ 文件类型候选 → 不可覆盖证明 → Controller 提交 verified Fact
→ 只读 Intent 评估 → Controller 提交 Intent.succeeded → 返回上层
```

成功只表示当前 Intent 取得了有证据支持的工具识别结果。TaskRun 目标为“找到 flag”时，`ASCII text` 不证明该任务完成；T4 不调用完成确认、不完成 Turn、不生成 TaskResult。取消和 Deadline 仍由 Controller 执行既有终态提交及清理。流程图中的 direct、工具、证据、验证和评估路径用于此切片，其上层 TaskRun 目标判断、Stage、Procedure、RAG、CLI 及自动多 Intent 调度留后续。

Fake Model 只提出候选，核心规则真实执行。真实本机 File MCP 负责命令生成，Fake Docker 输出来自夹具，只能验证程序接受受支持候选、拒绝不受支持候选，不能证明真实文件类型、Docker 隔离或真实进程终止。

## 可信来源、导入与目标

- `TaskRunController.start(..., source_root: str | None = None)` 兼容旧调用。可信入口先以 `Path.resolve(strict=True)` 取得现存规范绝对目录，Controller 复核后与启动事件及 Checkpoint 原子保存。旧 TaskRun 的 NULL 不补填，不能导入本地附件。
- `AttachmentImportRequest(import_id, task_run_id, sandbox_id, attachment_index, source_root, sandbox_path)` 由可信上层提供。原始相对 `local_path` 仅从持久 TaskRequest 的该索引读取；URL 不支持。来源解析必须在根目录内，拒绝符号链接、越界和非普通文件；目标为规范 `/workspace/input/...`，不可覆盖。文件同名不构成映射。
- `import_attachment(request, cancellation_token)` 和 `recover_import(import_id, cancellation_token)` 均为异步。单文件最多 16 MiB，来源读取、写入及读回共用最多 60 秒且不超过 TaskRun Deadline。
- 独立导入端口在一次 Sandbox 准入锁内新建、写入、读回字节数和 SHA-256；显式恢复只读回。导入不来自模型，不经过 ToolGateway；本轮仅提供 Fake Sandbox 验收，真实 Docker 导入适配器留后续。
- 接触 Sandbox 前，保存不可覆盖且带校验和的声明；记录 TaskRun/Sandbox、附件索引、原始 local_path、根目录、精确目标路径、字节数、SHA-256、声明时间。声明起独占附件索引及目标路径；未知结果也阻止新 ID。
- 读回与原始字节数/摘要一致才签发成功收据，增加完成时间，不保存文件内容。旧 ID 成功重放只核对调用参数并返回原收据，不再读取可能变化的源文件、不访问 Sandbox。同 ID 参数不同或跨 ID 占用冲突均拒绝。
- 声明存在但无成功收据时返回 `AttachmentImportRecoveryRequired`，不自动写入。显式恢复只在摘要一致时补签，其他情况保持未知。声明后取消、超时或写入/读回异常均属于未知。
- 前置失败使用 `AttachmentImportRejected(code)`：`task_run_mismatch`、`sandbox_mismatch`、`task_run_not_running`、`source_root_unavailable`、`attachment_not_local`、`source_path_rejected`、`source_unavailable`、`attachment_too_large`、`deadline_or_cancelled`、`import_not_found`。冲突与证据存储异常分别保留独立异常。

`FileTypeIntentGoal(task_run_id, intent_id, path)` 表示获取该路径有证据支持的类型识别结果，不预设类型，不从自然语言推导。首期仅 `goal_type=file_type`。上层先选中并持久化 Intent.running，首次 Plan 生成前调用：

```python
controller.bind_intent_goal(goal, import_id, mutation_id, authorization)
```

Controller 核对 TaskRun/Intent、运行状态、成功导入收据及原声明、附件索引/local_path/source_root/Sandbox/目标路径，以及当前授权包含 `file.inspect_file` 和精确 path；一次事务提交目标、事件和 Checkpoint。可选只读导入端口未配置时拒绝新绑定。目标表不存授权，首次事件保留原 import_id。

绑定结果为 `IntentGoalBindingResult` 的 `created/replayed/existing`。mutation 作用域是 TaskRun 内；指纹仅含操作及 `task_run_id/intent_id/goal_type/path`。同 ID 同指纹重放首次结果及原 import_id，不读取新收据、不重新授权；同 ID 异指纹冲突。新 ID 同目标必须重新核对收据与当前授权，成功返回 existing，不写事件/Checkpoint；Intent 已结束时新 ID 一律拒绝。目标随 TaskRun 保留，Intent 结束不删除。

稳定错误码：`intent_not_running`、`task_run_mismatch`、`goal_path_not_in_task_scope`、`goal_path_not_authorized`、`goal_conflict`、`mutation_conflict`。存储异常不伪装成这些错误。每次新工具派发仍由 Gateway 重验当前授权；历史请求收据中的授权快照不赋予后续权限。

## Plan、模型动作、预算与上下文

公开入口为 `async IntentToolCallingLoop.run(intent_id, authorization, cancellation_token) -> IntentLoopResult`。Controller、模型协调器、Gateway、验证器、评估器、证据、台账及有界上下文读取在构造时注入；协调器与循环必须使用同一模型调用台账。TaskRun/Plan/目标都来自已提交状态，调用方须先完成导入、Intent.running 和目标绑定。

一个 Intent 只有一个 Plan，T4 只接受一个步骤。循环固定工具名 `file.inspect_file`、步骤 0、Plan ID、可信 ID 及既有默认 30 秒工具超时；实际超时由 Gateway 按 Deadline 收缩。成功 Execution 持久化后，Controller 显式完成 Plan，随后模型才能提出 Fact 候选。动作台账允许读取 running 或 completed Plan；completed Plan 不允许发起新的检查。

动作以严格 `kind` 对象归一化，不接受缺字段、额外字段或未知 kind：

```json
{"kind":"inspect_file","path":"/workspace/input/sample.txt"}
{"kind":"file_type_candidate","path":"/workspace/input/sample.txt","observed_type":"ASCII text","execution_id":"..."}
{"kind":"stop","short_reason":"..."}
```

模型不提供命令、授权、状态、证明 ID、Fact ID 或 mutation ID。`IntentActionContext` 包含 Intent/Plan/步骤 0/goal_path 及最近已提交 Execution 的状态和观察。stdout/stderr 共用 4096 字符（先 stdout 再 stderr），同时保留原双流截断标记及 context_clipped；观察不是 verified Fact。重试后的最近执行按持久动作声明中的请求顺序选择，不能按 UUID 排序推断。Fact 上下文最多 64 条/16384 字，策略失败最多 32 条，沿用既有读取接口。

每 Intent 的 Plan 调用最多 2 次、动作调用最多 8 次，均包含结构纠正且按开始登记计数；TaskRun 不另加次数限制，仍受 Deadline。一次模型响应至多纠正一次结构错误。一次成功文件检查的限制也按 Intent 统计，不妨碍其他 Intent 使用同一 Sandbox。

schema 5 的模型调用台账记录开始/结束、Plan 关联、纠正关联、结果类别及经过结构校验的 Plan/动作摘要；类别为 `plan/intent_action/invalid_structure/model_error`。开始与完成分别只写一次，同 ID 同内容只读重放、不同内容冲突；不记录原始提示、原始响应或凭据，不推进控制面 Checkpoint。已有开始但没有完成结果时停止自动调用，不以重启清零。

`invoke_async` 只把模型适配器调用放在工作线程；SQLite 台账仍在调用线程写入。取消/Deadline 后忽略迟到结果，未完成记录保持未知，不能触发工具或 Fact。Python 不能强制终止该线程，适配器仍须遵守超时与取消令牌；真实供应商适配器需要后续验收。

## 文件类型验证与 Intent 成功

模型候选仅为 `FileTypeFactCandidate(path, observed_type, execution_id)`。验证器复核目标、导入映射、工具请求/授权快照、实际保存命令、Execution 及输出。必须成功退出、停止已确认、未超时/取消、双流未截断、stderr 为空。

stdout 只能是一条非空白文本行，允许末尾无换行或恰好一个 LF/CRLF，仅去掉该末尾换行。正文禁止 Unicode Cc、U+2028/U+2029，最多 200 字；其他空格和大小写保留。候选必须逐字匹配，不将 ASCII text 归纳成 Python 源码。Fact 文本由可信代码生成“工具文件类型识别结果：<observed_type>”；完整路径留在结构化证据。

证明为不可覆盖、带校验和的 `FileTypeVerificationProof(proof_id, task_run_id, intent_id, execution_id, import_id, path, local_path, observed_type, verified_at)`，proof_id 由可信代码生成 UUID4。导入声明、成功、执行开始/结束、证明时间均须在 TaskRun 时间范围，且顺序正确、无未来时间；不另加分钟级过期。

`lookup_file_type_proof(execution_id, proof_id)` 每次重新核对原始证据后才返回 local_path 目标引用。旧 FileToolRecorder.lookup 仍返回 target_ref=None，旧 Fact 不补造证明。

`commit_verified_file_type_fact(intent_id, candidate, proof_id, fact_id, mutation_id)` 提交前复核证明、Execution、目标和状态，一次事务插入 verified Fact、事件与 Checkpoint。指纹含操作、TaskRun/Intent、候选三字段、proof_id、fact_id；旧 ID 精确重放不复核。证明先写而事务失败留下的孤立证明，可由同 ID 重试完整复核后使用；候选失败不创建 Fact、不改控制面。旧 create_fact → verify_fact(turn_target_satisfied=...) 语义不变。

评估器只读返回 `IntentGoalEvaluation(task_run_id, intent_id, outcome, verified_fact_ids, short_reason)`：

| outcome | short_reason | 含义 |
| --- | --- | --- |
| satisfied | verified_file_type_fact | 当前目标至少一个已提交 verified Fact 的证明重新核验通过；只列真实支持的 Fact ID，按提交顺序排列。 |
| not_satisfied | no_supporting_verified_fact | 没有支持事实且没有本 Intent 未决/未知 Execution，不等于目标不可能完成。 |
| inconclusive | execution_outcome_unknown | 本 Intent 有 pending/running Execution 或其 ID 对应 OUTCOME_UNKNOWN；支持事实优先于未知执行。 |

缺失/跨任务目标为 state_conflict；已提交证明损坏、缺失或存储读取异常直接抛异常。`complete_satisfied_intent(intent_id, evaluation, mutation_id)` 重新只读评估并要求支持 Fact ID 完全一致，才原子提交 Intent.succeeded；旧 mutation 精确重放只返回首次结果。Turn、TaskRun 和完成确认由上层负责。

## 停止与错误

`IntentLoopResult(task_run_id, intent_id, evaluation, stop_reason, plan_id, execution_ids, verified_fact_ids)` 的评估与停止原因分开；ID 仅来自当前 Intent 的已提交对象。停止原因固定为 `intent_satisfied/model_stopped/candidate_rejected/budget_exhausted/tool_failed/recovery_required/model_error/interrupted`。

- stop 立即返回 model_stopped；确定的 candidate_rejected 立即返回，不新增内容修正，不改变 Intent 终态，不产生 target_mismatch。
- 模型认证/供应商/拒答错误、已返回的超时及一次结构纠正后仍非法，返回 model_error；不调用旧模型失败路径宣布 TaskRun.failed。
- ToolGateway 的 Policy/参数拒绝或其已提交的失败返回 tool_failed，保留 Gateway/Controller 的既有状态提交。
- 明确环境故障且 Gateway 给出 RETRY_EXECUTION 或 RETRY_COMMAND_GENERATION 时，用声明中唯一重试 ID 显式重试一次，不再让模型决定。执行重试复用原命令和步骤；第二次仍失败返回 tool_failed，不增加第三次请求。新派发仍重验授权、取消及 Deadline；不增加模型调用次数。
- 工具结果未知、未完成收据或停止未确认返回 recovery_required，不重派。取消/Deadline 优先通过 Controller 既有生命周期处理；停止已确认则 interrupted，已启动工具但停止未确认则 recovery_required。
- candidate_rejected 是内容/路径/完整输出不支持；evidence_unavailable 是证据缺失/损坏；state_conflict 是归属/状态冲突。后两类、存储、提交及重载异常直接抛出，不交模型重试。

以下是 T4 限制，使用独立 `IntentActionRejected(code)`，不是通用 Agent 错误策略：

| code | 行为 |
| --- | --- |
| path_out_of_scope | 循环先由 Policy 比对 Intent 精确目标，拒绝越界；Gateway 自身授权检查仍保留。 |
| unsupported_plan_steps | 通用响应允许步骤数组，但 T4 只支持一步；多步不拆分、不新增调整调用。 |
| repeated_inspection | 只有当前 Intent 已有成功检查后的新动作才拒绝；同一个已执行动作仍只读复用原结果。 |

拒绝结束本次 run，不改领域状态；Plan/Intent 等可能仍 running，调用方必须将异常视为需要外层处理的停止信号，不能自动空转重调。持久动作/Plan 摘要及固定身份使同一拒绝可恢复重现，不重新调用模型。取消/Deadline 在消费动作前优先处理。跨 TaskRun/Intent 的 Execution 引用仍是 state_conflict。

## schema 6 与安全恢复

旧 schema 1–5 顺序原子迁移至 6，保留 TaskRun、事件、Checkpoint 和模型台账。schema 2 是 intent_goals，3 是 source_root，4 是 verification_ref，5 是 model_invocations；schema 6 新增：

- `intent_plan_applications`：以已完成 Plan 调用 ID 唯一关联经校验单步骤、可信 plan_id、注册/启动 mutation ID。在控制面提交前保存，不推进 Checkpoint。
- `intent_action_applications`：以已完成动作调用 ID 唯一关联动作摘要及副作用 ID。inspect 保存首次/重试 ToolRequest ID 和完成 Plan mutation ID；candidate 保存 proof/fact/提交 Fact/完成 Intent ID；stop 不分配副作用 ID。

声明不可覆盖；保存时核对已审计结果与归属，同内容重放，异内容冲突。新的模型调用结果在当前 run 可首次声明；历史已完成但没有声明的有效结果返回 recovery_required，不能猜测旧进程是否已产生副作用。多步骤等确定拒绝可由持久的非法 Plan 摘要重现。

恢复按声明复用原 ID：Plan 尚未注册/启动时从对应安全位置提交；工具已完成时核对收据、命令及控制面后消费，已开始但未完成则停止；候选的证明/Fact/完成 Intent 提交按原 ID 复核或重放。Controller 缓存失效须先显式 reload_state；恢复读取本身不自动修复证据或重派工具。迁移不补造历史声明。

## 验证位置

- `tests/test_intent_loop.py`：真实本机 MCP/Fake Docker 成功链；候选、拒绝、重放、明确重试、未知结果、提交/重载中断、孤立证明、数据库重开、多 Intent、取消/Deadline 和迟到模型结果。
- `tests/test_attachment_import*`、`test_file_type*`、`test_modeling.py`、`test_control_store.py`：导入、映射、完整证据、不可变证明、台账、迁移与预算规则。
- 临时文件统一在 `.test/`，保留 `.venv/`；最终实际测试、类型、格式、编译和构建结果见 TODO，未运行的真实环境不升级为验收通过。
