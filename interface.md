# Qinyuan_Agent 接口声明

## 说明

本文件描述 Python 层的端口边界。接口文件只声明签名，不实现业务函数；T2 Sandbox 与 T3 文件工具适配器的已确认实现另见后续章节，T5 已增加 CLI 装配，历史真实 Docker 文件识别效果复核通过；本次完整独立复验受权限阻塞，详情见 TODO。已有类型优先复用 `agent_core` 中的定义。

## 已冻结的声明

代码位置：[`agent_core/interfaces.py`](agent_core/interfaces.py)。

| 接口 | 已声明内容 |
| --- | --- |
| `TaskRunLifecyclePort` | `start`、`attach`、`status`、`advance_time`、`commit_turn_registration`、`commit_turn_operation`、`mark_candidate_found`、`cancel`、`conclude`、`recover_task_run`、`reload_state`、`cleanup_sandbox`、`close` |
| T1 工作副本提交 | `TurnWorkingCopy` 返回不可变 `TurnOperation`；`TaskRunController.commit_turn_operation(operation, mutation_id)` 返回 `CommittedOperation` |
| 提交前候选复核 | `ControlStateRepository.validate_candidate(state, mutation) -> None`；验证整个变更后的候选及外部证据，失败抛出 `StorageError` 且不写库 |
| 提交后重载异常 | `CommittedStateReloadError` 是 `StorageError` 子类，`committed=True`；T1 入口的 `commit` 携带已提交结果，缓存失效不代表数据库回滚 |
| `TurnRegistrationPort` | 从 `ControlStateSnapshot` 和有序 `IntentCandidate` 准备 pending Turn/Intent 注册，不提交存储 |
| `ModelReasonerPort` | 使用 `ModelRequest`、`CancellationToken` 返回 `ModelResponse` 或 `ModelError` |
| `ContextBuilderPort` | 查询有界 `ModelContextSnapshot` |
| `ControlStatePort` | 读取 `TaskRunSnapshot` 和 `ControlStateSnapshot` |
| `ClockPort` | 提供 UTC Unix 秒 |
| `AtomicCommitterPort` | 使用 `ControlMutation` 返回 `ControlCommitResult` |
| `SandboxPort` | 复用现有 `create / interrupt / destroy` 生命周期端口 |
| `TaskRunSandboxPort` | T2 可选扩展：`bind(task_run_id, sandbox_id, *, terminal)` 精确绑定；`quiesce(sandbox_id)` 关闭执行准入；旧内存 Fake 不要求实现 |
| `ModelAdapter` | 复用现有供应商无关 `invoke(request, timeout_seconds, cancellation_token)` |
| `ControlStore` | 复用现有原子提交、幂等查询和状态读取端口；T2 增加 `load_terminal_commit(task_run_id)`，供重复取消读取首次终态结果，不写新 mutation |
| `ExecutionEvidencePort` | 复用 command/output/evidence 只读查询 |
| `PlanValidationEvidencePort` | 复用 Plan 校验证据只读查询 |
| `ModelErrorEvidencePort` | 复用模型错误证据归属查询 |

### 已复用的核心类型

`TaskRequest`、`Target`、`ModelBackend`、`Turn`、`Intent`、`Plan`、`PlanStep`、`Execution`、`Fact`、`FailureSummary`、`PlanValidationRecord`、`ModelRequest`、`ModelResponse`、`ModelError`、`RuntimeEvent`、`ControlMutation`、`ControlCommitResult`、`ControlStateSnapshot` 和 `TaskRunStatus` 已存在于 `agent_core`，本次不复制定义。

## 已确认模块的边界声明

`agent_core/interfaces.py` 同时声明以下 Protocol 的模块边界：

`AgentApplicationPort`、`RunSupervisorPort`、`StagePlannerPort`、`ProcedureSelectorPort`、`ProcedureRunnerPort`、`ToolGatewayPort`、`PolicyPort`、`MCPManagerPort`、`SandboxRuntimePort`、`ExecutionRecorderPort`、`FactVerifierPort`、`GoalEvaluatorPort` 和 `EventPort`。

T3 已补齐工具、Policy、MCP、Sandbox 执行和 ExecutionRecorder，T4 增加专用文件类型验证、Intent 评估和循环入口，见后文。其余 Protocol 只表达职责边界，公共参数、返回值与错误结构尚未冻结，暂不填入 `Any`、万能字典或猜测字段。

## T5 公共入口与后续转换器边界（2026-09-24）

T5 面向独立的自动化渗透测试 Agent 提供 CLI、事件和真实单 Intent 文件类型识别。第一阶段以模拟链路验证公共入口，第二阶段接入真实模型、真实 Docker 和实际附件；历史真实效果复核通过，本次完整独立复验受权限阻塞。第一阶段通过不能替代第二阶段，历史通过不能替代本次复验。CLI 和 Python API 的应用操作仍归属 `AgentApplicationPort`；CLI 的外部序列化已冻结为 T5 v1；稳定公共 Python API 仍未冻结。T5 不声明或实现 qinyuan-bat 专用适配接口。

真实接入复用 `ModelAdapter`、`AttachmentImportSandboxPort`、TaskRun Sandbox 生命周期及 `IntentToolCallingLoopPort` 等既有内部边界；正式模型入口已确认使用项目 `.env` 的 DeepSeek API，Codex CLI 本轮不接入。具体请求/响应、超时和取消已确认并实现，见 protocol.md 的 T5 v1 节。输出必须区分当前 Intent 结果与整体 TaskRun 状态，不能将真实文件识别表述为自主解题或找到 flag。多 Intent 调度、任务目标判断和最终 TaskResult 属于 T6。

CLI 外形已确认为同步 `qinyuan-agent-new run-intent --request <JSON 文件|->`，最终 stdout 是单个版本化 JSON，可选 `--events` JSONL 导出持久化控制事件。普通切片结束且无未知副作用时 TaskRun 收尾为 `inconclusive`、整体目标为 `not_evaluated`，清理结果独立报告；请求/响应字段、错误和退出码、事件读回入口与 `resume/cancel` 已确认并实现；`AgentApplicationPort` 仍不承诺稳定公共 Python 签名。

后续独立转换器负责多题目获取、调用 `qinyuan-agent-new` 和平台 flag 提交。它使用哪种公共调用入口、如何传递题目/附件、接收结果和提交回执，均需另行确认；本次不新增转换器 Protocol、字段或占位签名。当前 TaskRun、Intent 和控制面类型继续复用，转换器不直接修改其内部状态。

2026-09-25 已确认前端在 T6 闭环稳定后独立交付。页面操作和 Agent 调用方式尚未冻结，不据此新增 HTTP/WebSocket、事件订阅或前端专用 Protocol；实施前逐项确认，继续复用确认后的应用边界。

## 待确认后才能补齐的声明

1. `AgentApplicationPort`：T5 CLI 已实现；仍待确认公共 Python API、通用事件订阅和 T6 完整结果。
2. `RunSupervisorPort`：循环驱动、动态 Intent、路径耗尽、Budget 和终止返回值。
3. `StagePlannerPort`：StageContext、前置/后置条件、时间片和计划返回值。
4. `ProcedureSelectorPort` / `ProcedureRunnerPort`：ProcedureFact 字段、版本、匹配排序、校验失败和执行失败路径。
5. `ToolGatewayPort` / `PolicyPort`：T3 File Tool 已确认；T6 通用脚本新增 `T6CommandGateway`，其权限粒度与 Scope/ROE 仅限本轮已确认的 Sandbox 边界。
6. `MCPManagerPort` / `SandboxRuntimePort`：专用 MCP 仍使用 T3 方法；通用脚本直接进入 T6 Sandbox，其他 Tool、远程 MCP 和真实 Docker 验收仍需确认。
7. `ExecutionRecorderPort`：T3 执行记录、保留、截断、Hash 与脱敏已确认；一般 Artifact 的扩展字段和清理 API 仍待后续确认。
8. 通用 `FactVerifierPort` / TaskRun `GoalEvaluatorPort`：任意事实与整体任务判断仍待确认；T4 文件类型验证器和只读 IntentGoalEvaluatorPort 已实现，不以此扩大到通用目标判断。
9. `EventPort`：事件类型、顺序、持久化、重放、订阅和背压。

## T2 外围适配入口

`Sandbox.task_run_sandbox.TaskRunSandbox.connect(settings=None)` 连接已有 Docker Engine，不准备网络、不拉镜像；显式 `SandboxSettings` 的镜像、控制请求超时和资源限制沿用既有类型。`TaskRunSandbox(runtime)` 接受现有 DockerRuntime 或测试 Fake。它实现上述 T2 生命周期端口，内部 `execute(sandbox_id, CommandRequest) -> CommandResult` 只用于本轮固定脚本模拟验收；T3 之前不暴露到模型/CLI，不作为权限校验接口。

`TaskRunController.cleanup_sandbox() -> None` 仅对已成功重载的终态执行清理，不生成业务 mutation。无已提交 TaskRun 的创建错误可使用 `TaskRunSandbox.cleanup_uncommitted(task_run_id, sandbox_id=None)`：有 ID 时精确查询，无 ID 时只按本次所属标签定位唯一资源；查询为空仍不能证明未完成的 create 不会稍后生效，因此报告未知。归属不符拒绝；仍运行的未知资源不停止。该操作不属于紧急强制清理。

`TaskRunSandboxError` 提供有界 `phase / outcome / task_run_id / sandbox_ids`，并分开保留 `original_error / cleanup_error` 供调用者诊断；异常默认文本不输出原始 SDK 错误。它不新增 TaskRun 终态或外部 wire protocol。`close()` 关闭执行准入和 SDK 连接，不自动删除容器。

`TaskRunController.reload_state() -> ControlStateSnapshot` 是缓存失效后的显式恢复入口，要求当前 Controller 仍持有同一 Registry 所有权且未关闭。它通过 Repository 重读并复核完整控制面和外部证据，确认 TaskRun/Checkpoint 归属、Sandbox ID 不变和 Checkpoint 不回退，全部成功后一次性替换缓存并清除失效标记；失败抛出 `StorageError` 并保持缓存失效，不释放 Registry。该入口不提交 mutation、不推进时间、不调用 Sandbox，也不自动清理。终态提交后自动重载失败时，正确路径是恢复读取/证据 -> `reload_state()` -> `cleanup_sandbox()` -> `close()`；随后新 Controller 可使用同一个 Registry 重新绑定。

## T3 已确认接口（2026-09-10）

本节替代上文工具、Policy、MCP、Sandbox 执行及 ExecutionRecorder 的待确认项；其他待确认模块继续保持空边界。类型位于 `agent_core/tool_contracts.py`，`interfaces.py` 的对应公开端口继承这些具体签名。

| 类型/接口 | 已确认字段或方法 |
| --- | --- |
| `ToolRequest` | UUID4 request_id、task_run_id、plan_id、step_index、tool_name、path、timeout_seconds（默认 30，1–60 秒） |
| `ToolAuthorization` | authorization_id、task_run_id、allowed_paths、allowed_tools；由可信调用方提供，不属于模型请求 |
| `PolicyDecision` | allowed、short_reason；`authorize(request, authorization)` 与 `validate_command(request, command)` |
| `MCPCommand` | tool、arguments 不可变元组、timeout_seconds；仅允许固定 python3 包装脚本 |
| `SandboxExecutionRequest` | sandbox_id、command |
| `SandboxExecutionResult` | sandbox_id、exit_code（可空）、stdout/stderr、timed_out、termination_confirmed、双流截断标记 |
| `ExecutionRecord` | request/task_run/sandbox/plan/step/authorization/execution 标识，command_ref/output_ref，started_at/finished_at，exit_code，failure_type、short_reason，双流内容及截断标记、timed_out/cancelled/termination_confirmed，以及实际派发的 effective_timeout_seconds（1–60 秒，未派发为空） |
| `ToolResult` | record、可空的核心 FailureAction；只报告调用结果，不提交 Fact |
| `ToolGatewayPort.call` | async `(ToolRequest, ToolAuthorization, CancellationToken) -> ToolResult` |
| `MCPManagerPort.prepare` | async `(ToolRequest, timeout_seconds: float) -> MCPCommand` |
| `SandboxRuntimePort` | async `execute(SandboxExecutionRequest) -> SandboxExecutionResult`；`quiesce(sandbox_id)` 关闭新执行准入 |
| `ExecutionRecorderPort` | begin、save/load_command、save/read_record、finish，以及既有 ExecutionEvidencePort 的查询 |

### T6 直接脚本切片（2026-09-25）

`T6CommandGateway.call(SandboxCommandRequest, SandboxCommandAuthorization, CancellationToken)` 在可信侧完成 Policy 校验后直接调用 `T6CommandSandboxPort.execute(AuthorizedCommand, CancellationToken)`；它不启动 MCP。`AuthorizedCommand` 只允许结构化的 `("bash", "-c", script)`，工作目录固定在 `/workspace/work` 及其真实子目录。脚本最多 64 KiB，stdin 最多 1 MiB，stdout/stderr 各保存最多 64 KiB，单步有效时限取请求、60 秒步骤预算和 TaskRun 剩余时限的最小值，且不超过 300 秒。

Sandbox 只接受经摘要校验的工作副本执行；原始输入保持只读且不可执行。直接执行的完整退出（包括非零退出码）记录为 `completed`，由后置条件验证器判断其内容；只有可信侧时限和已确认的进程清理才能产生 `step_timeout`。派发后无法确认结果的记录为 `outcome_unknown`，阻止新的派发并等待显式恢复。已确认超时允许同一 Plan 步最多一次改命令重试。

`IntentPostconditions` 由模型在执行前提出，可信侧核对当前 TaskRun/Intent/Plan 与步骤索引后不可覆盖地保存。首版 `OutputContains(step_index, stream, literal)` 与 `FileSha256(step_index, path, expected_sha256)` 全部满足才返回 `satisfied`；任一证据不完整、文件摘要观察未知或 Execution 未完成返回 `inconclusive`，完整证据确认不满足才返回 `not_satisfied`。文件条件表示整个 Plan 结束时的 SHA-256，输出条件仍绑定指定步骤。`T6FileDigest` 以普通工具 UID 执行固定只读程序，拒绝工作区路径穿越、符号链接及非普通文件；首次摘要观察不可覆盖，未知观察不自动重读。

`T6ObservationProofVerifier.issue(intent_id, proof_id, plan_id=None)` 依据该 Plan 已固定的条件、Execution 和已保存的文件摘要观察签发独立不可覆盖证明；`lookup(execution_id, proof_id)` 只读复核，绝不重新执行 Bash 或重新读取文件。`TaskRunController.commit_verified_t6_observation(intent_id, proof_id, fact_id, mutation_id, complete_intent=True)` 经可信侧与 `ControlStateRepository` 二次复核，原子提交 verified 观察 Fact、事件和 Checkpoint；`complete_intent=False` 时 Intent 保持 running，默认值保留 T6-2 的 Fact/Intent 同事务完成行为。证明引用使用 `t6:<UUID>` 与文件类型专用证明区分。此入口不评估候选 flag 正确性，也不改变 TaskRun 状态；v2 Runner 和工作台真实模式均已接入，工作台只做控制和只读投影。

T6-5 的内层动作解析入口为 `parse_t6_action(raw)`，只接受 `probe`、`complete_intent`、`abandon_intent`、`candidate_flag` 四种严格字段形状；它只解析提议，不执行副作用。`PendingCandidateRecorder.record(state, intent_id, action, candidate_id, created_at)` 复核当前 Intent 的已完成 Execution 及命令收据后，保存不可覆盖的 pending 候选；`grouped_pending_candidates` 仅供结果展示按原文合并来源。`TaskRunController.complete_verified_t6_intent(intent_id, fact_ids, mutation_id)` 只在引用的已验证 Fact 归属当前 Intent，且包含最新可验证短 Plan 的证明时原子完成 Intent。通用旧入口拒绝使用 T6 观察 Fact 绕过该检查。schema 10 的 `T6ActionJournal` 在模型调用前经 `SqliteControlStore.begin_t6_action` 固定当前 Checkpoint、上下文摘要和资源 ID，结构化动作写入 `T6ActionEvidenceStore` 后仅将摘要提交到数据库；`resume_unapplied` 只读复用完整证据，证据已写而完成标记未写时补记同一结果，只有开始记录时要求显式恢复。该审计切片已由 `IncrementalIntentRunner` 与 `run-task` 组装。

`recent_t6_observations(state, intent_id, commands)` 只读取当前 Intent 最近最多 8 条已结束 Execution 的完成收据，返回按先后顺序排列的有界观察；结构化 JSON 合计不超过 16 KiB。非法 UTF-8 字节转义为 `\xNN`，保留原始截断标记并单独标记上下文裁剪。任何仍运行的 Execution 或缺失、不匹配的完成收据都会阻止生成下一次模型上下文。Runner 已将其接入每次动作请求。

`build_action_model_request` 从已提交的运行中 Intent、已完成短 Plan、可信模型上下文以及有界 Execution/候选收据构造 `T6ActionModelRequest`。`AuditedT6ActionModel.propose` 在模型调用前登记 schema 10 资源 ID，严格解析动作并保存不可覆盖结果；未知模型调用不重发，结构错误最多纠正一次。已保存但未应用的动作返回原资源 ID，供 Runner 核对控制面副作用。Runner 已接入此入口；Bash 仍只能经 Gateway 派发。

`recent_pending_candidate_context(state, intent_id, store, commands)` 返回当前 Intent 最近最多 8 条 pending 候选摘要，JSON 总量不超过 16 KiB。完整原文按最新优先纳入；放不下的条目以 `value=None`、SHA-256 和来源 ID 表示，并标识省略；每条重新核对已完成命令收据。候选上下文不改变候选台账或整题状态。

### T6-3 有序 Intent 生成（2026-09-26）

`OrderedIntentGenerator.generate(task_run_id, turn_target, registration_mutation_id, cancellation_token)` 为内部异步入口；`resume_unapplied(task_run_id, cancellation_token)` 从台账查找同一未注册轮，不猜测新的请求 ID。`turn_target` 与 UUID4 mutation 来自可信调度调用方；服务从 Repository 读取已提交、有界的 Fact 和目标失败摘要，构造 `intent_generation` 请求。`ModelInvocationCoordinator` 在每次调用前经同一 `SqliteControlStore` 登记轮级审计；单次结构纠正也独立登记。schema 8 的 `turn_model_invocations` 保存唯一模型调用、Checkpoint、上下文 SHA-256、注册 mutation、确定性 Turn ID、完成结果或错误，不保存原始提示/响应。候选非空，按模型顺序持久化；不额外限制每轮数量。结果完成与固定注册标识在同一审计事务内；Turn/Intent、事件和 Checkpoint 仍由 Controller 另一次原子提交。未知调用、先前结果未应用、上下文/状态变化均阻止新派发或旧结果应用。该入口不执行 Intent，也不改变 TaskRun 目标状态。

`FileToolRecorder(directory, sensitive_values=())` 显式接收证据目录与敏感值。`EvidenceStorageError` 表示证据写入/完整性/请求指纹冲突；`ToolRecoveryRequired` 表示已有不完整请求、历史执行未确认停止等情况，禁止借重试该请求重新派发命令。Controller 的提交后重载错误仍原样保留，不包装成工具失败。

### T6-4 深度优先调度（2026-09-26）

`RunSupervisor` 只通过 `TaskRunController` 和 `TurnWorkingCopy` 推进控制面。它从最小 sequence 的 pending/running Turn 选择 priority 最小的 pending Intent，先原子提交 `Intent.running`，再调用窄小的 `IntentRunnerPort.run(intent_id) -> IntentRunResult`。Runner 必须返回带 TaskRun/Intent 归属的结构化结果；调度器不从模型文本或退出码推断领域结论。

`IntentRunResult` 的结果为 `succeeded`、`target_mismatch`、`inconclusive`、`recovery_required` 和 `model_error`。`succeeded` 要求 Runner 已提交当前 Intent 的 succeeded，调度器随后完成 Turn；`target_mismatch` 要求已完成 Plan 与 Execution 证据，提交分类失败并继续同一 Turn 的兄弟候选；兄弟候选全部完整失败时，通过显式 `TurnGeneratorPort` 生成下一独立 Turn，生成器从已提交 Fact 与目标失败摘要构造上下文，不需要 `parent_turn_id`。`inconclusive` 只用于 Execution 已结束但后置条件无法判断，原子标记 Intent/Turn 并停止派发；`recovery_required` 用于仍有 running Execution 或未完成工具收据，保留原始 running 状态并禁止继续派发，等待显式恢复；`model_error` 将错误原因写入审计/失败摘要，Intent/Turn/TaskRun 均保持原状态，停止调度且不尝试兄弟候选。

已提交的 `model_error` 失败摘要是该 Intent 的停止标记；恢复时调度器只读返回相同原因，不再次调用 Runner/模型，也不选择兄弟候选。

最新 Turn 已为 `inconclusive` 时，再次调用调度器只读返回 `paused_inconclusive`，不生成下一 Turn。`AuditedTurnGenerator` 先复用未应用的轮级模型台账；已完成的错误只读回放，已保存的候选用原注册 ID 提交，调用未完成则要求显式恢复。

Runner 仅接收本次调度新提交为 running 的 Intent；若调度器读到先前已 running 的 Intent，则只读返回 `recovery_required`。这一约束避免未知执行因调度器重启而再次派发。

调度器不执行 Bash、MCP 或答案校验，也不把 verified Fact 升级为 TaskRun achieved。控制库的 mutation 重放仍沿用现有幂等和 Checkpoint 规则；v2 CLI、临时 TaskResult、工作台只读投影及 T6-6 单一公网目标网络适配器已接入。独立答案校验器和真实环境验收仍待后续。

本轮只确认上述 Python 内部类型及本地证据文件，不冻结 CLI、外部 TaskResult 或通用远程 wire protocol。T3 不提供删除运行中证据的 API；调用方应在不再需要审计或控制面恢复后显式清理整份证据目录。

## T4 已确认边界（2026-09-23）

单 Intent direct 循环返回当前 Intent 目标已满足/未满足/无法确定的判断。Fact 验证通过不等于 Intent 目标满足，Intent 目标满足也不等于 TaskRun 目标满足；完成确认、TaskRun 终态和最终 TaskResult 由上层按已有契约处理。T4 不因文件类型 Fact 成立就调用任务完成确认入口。

文件类型候选为 `FileTypeFactCandidate(path, observed_type, execution_id)`。只接受成功、停止已确认、双流未截断、stderr 为空的单行工具输出；最多移除一个末尾换行，正文不得含控制字符或 Unicode 行分隔符，类型文本最多 200 字且逐字匹配。可信验证器签发独立证明；`commit_verified_file_type_fact(intent_id, candidate, proof_id, fact_id, mutation_id)` 在重新核对证明后原子提交 verified Fact、事件和 Checkpoint。旧 `verify_fact` 的 Turn 层语义保持不变。

只读 `IntentGoalEvaluatorPort.evaluate(task_run_id, intent_id)` 返回 `IntentGoalEvaluation(task_run_id, intent_id, outcome, verified_fact_ids, short_reason)`。`satisfied/verified_file_type_fact` 只列出本 Intent 当前目标的可复核已提交 verified Fact；没有支持事实时返回 `not_satisfied/no_supporting_verified_fact`，本 Intent 有 pending/running 或已记录结果未知的 Execution 时返回 `inconclusive/execution_outcome_unknown`。已提交证明缺失或损坏抛存储异常，缺失目标或跨任务归属抛 `state_conflict`，不伪装为三态结果。`complete_satisfied_intent(intent_id, evaluation, mutation_id)` 由 Controller 重新评估并核对支持 Fact ID 后，仅原子提交 Intent.succeeded；同 ID 精确重放不重新评估，Turn 和 TaskRun 状态不随之完成。

`intent_action` 的归一化内部响应使用 `kind` 判别对象，且恰为 `inspect_file(path)`、`file_type_candidate(path, observed_type, execution_id)` 或 `stop(short_reason)` 之一；多字段、缺字段和未知 kind 拒绝。请求携带可信的 Intent/Plan/步骤 0/目标路径及最近已提交 Execution 观察，stdout/stderr 共用 4096 字符上下文预算，保留原始双流截断和上下文裁剪标记；观察不是 verified Fact。T4 模型调用台账按 TaskRun/Intent 保存已开始和经校验结果，不保存原始提示/响应；每 Intent 的 Plan 生成至多 2 次，动作调用至多 8 次，首期最多一次成功工具派发。循环结果使用 `IntentLoopResult(task_run_id, intent_id, evaluation, stop_reason, plan_id, execution_ids, verified_fact_ids)`，不包含 TaskResult。

Intent 的 description 不用于推导目标；可信调用方独立提供并持久保留 `FileTypeIntentGoal`，Intent 结束不删除；上层先提交 `Intent.running`，Controller 在首次 Plan 生成前核对归属、运行状态、任务范围和当前授权，并将目标、事件及 Checkpoint 原子绑定；未绑定时 T4 拒绝启动。重试和恢复核对同一目标。目标保存在独立一对一控制面表，schema 2 引入该表，旧库按版本原子迁移，旧 Intent 不自动补目标。旧 `verify_fact` 接口的 `turn_target_satisfied` 明确表示 Turn 目标判断，不能暗中改作 Intent 满足或候选内容有效。绑定签名为 `bind_intent_goal(goal, import_id, mutation_id, authorization) -> IntentGoalBindingResult`，返回 `created`/`replayed`/`existing`，首次提交事件保存原 import_id，目标表不保存授权或收据副本。

已确认：来源 `local_path` 与 Sandbox 路径必须通过可信附件导入记录精确映射，URL 本轮不支持；绑定业务指纹只比较操作类型和四个目标字段，授权不入指纹；专用 `IntentGoalBindingResult` 区分 `created`/`replayed`/`existing`；六个稳定错误码、重复绑定及新 mutation 授权重验规则按 `T4_proposal.md` 执行，首期只支持 `file_type`。目标表不存授权快照或凭据，恢复后新工具派发重验当前授权。附件导入声明、成功收据及绑定入口已经实现。

本地附件来源根目录已确认并实现：持久化 `TaskRunController.start()` 增加仅限持久化模式的可选关键字参数 `source_root: str | None = None`；提供时须是可信入口预先解析的现存规范绝对目录，由 Controller 校验后随 TaskRun 启动原子保存。schema 3 使旧记录的该字段为 NULL；NULL TaskRun 不能执行 T4 本地导入。后续状态提交不可修改根目录，恢复时核对同一规范路径。

附件导入的公开入口为 `AttachmentImporter.import_attachment(AttachmentImportRequest, cancellation_token)` 与 `recover_import(import_id, cancellation_token)`，均为异步；请求固定含 import_id、TaskRun/Sandbox ID、附件索引、来源根目录和 Sandbox 目标路径，`local_path` 只从持久 TaskRequest 读取。独立 `AttachmentImportSandboxPort` 提供一次准入锁内的 `create_and_readback` 与只读 `readback`，返回字节数和 SHA-256。前置拒绝使用 `AttachmentImportRejected.code`，参数/占用冲突、证据损坏、声明后未知结果分别使用独立异常；`import_not_found` 表示显式恢复的 ID 从未声明。真实 Docker 导入适配器未纳入 T4 验收。

## T4 执行入口及恢复声明（2026-09-24）

`IntentToolCallingLoop.run(intent_id: str, authorization: ToolAuthorization, cancellation_token: CancellationToken) -> IntentLoopResult` 为异步入口。构造时注入 Controller、ModelInvocationCoordinator、Gateway、FileTypeVerifier、IntentGoalEvaluatorPort、只读工具证据、共享调用/动作台账及有界上下文读取。模型协调器须使用同一台账；可信上层先导入、选中 Intent.running 并绑定目标。公共类型见 `agent_core/intent_loop.py`；`IntentToolCallingLoopPort` 声明此入口。

`ModelInvocationCoordinator.invoke_async` 保留同步 invoke 的兼容性，仅适配器调用进入工作线程，SQLite 留在原线程；取消/Deadline 后不消费迟到结果，未完成调用保持未知。Plan 完成后仍可请求候选或 stop，但不允许新检查。

`IntentPlanApplication` 固定 Plan 摘要、plan_id、注册/启动 mutation；`IntentActionApplication` 固定动作及工具首次/重试 ID、完成 Plan ID，或证明/Fact/提交/完成 Intent ID。`IntentApplicationStorePort` 提供 save/load_plan_application 与 save/load_action_application，SQLite schema 6 实现不可覆盖声明并复核模型调用归属。

`IntentActionRejected.code` 为 path_out_of_scope、unsupported_plan_steps、repeated_inspection。同动作重放复用结果或重现拒绝；只有当前 Intent 成功检查后的新检查动作报 repeated_inspection。异常停止本次调用，不改变领域状态，上层不能自动空转重调。其余停止/失败/恢复规则以 `T4_proposal.md` 为准。


## T5 已实现的边界入口（2026-09-25）

- CLI：`agent_adapters.cli.main`，同步 run-intent、status、replay、resume、cancel；wire contract 见 protocol.md T5 v1。
- 应用装配：`agent_adapters.application.AgentApplication`；内部 Python API，不承诺外部长期兼容。
- 模型：`DeepSeekModelAdapter.invoke` 实现原 ModelAdapter，取消 HTTP 子进程，不输出供应商原始错误。
- 附件：`ImportableTaskRunSandbox` 实现既有 AttachmentImportSandboxPort；16 MiB、共享截止预算、精确路径、排他创建、读回验证。
- 控制面：`TaskRunController.start` 增加可选调用方预声明 `task_run_id`，仍校验 UUID4 并拒绝已存在 ID；`conclude` 增加仅限 inconclusive 的 `reason="single_intent_slice_finished"`，不改写 Intent 结果。
- 查询：`SqliteControlStore.open_readonly` 不迁移、不创建库；只读证据记录器不创建目录。事件导出仅读取已提交 Checkpoint 水位以内序号。
- 兼容：`FileToolPolicy.validate_recorded_command` 仅为历史证据接受旧 `--error` 固定程序；新 dispatch 的 validate_command 只接受 `-E` 版本。
