# Qinyuan_Agent 接口声明

## 说明

本文件描述 Python 层的端口边界。接口文件只声明签名，不实现业务函数；T2 Sandbox 与 T3 文件工具适配器的已确认实现另见后续章节，CLI 装配和真实 Docker 验收仍属后续交付。已有类型优先复用 `agent_core` 中的定义。

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

T3 已补齐工具、Policy、MCP、Sandbox 执行和 ExecutionRecorder 的方法及类型，见末节。其余 Protocol 只表达职责边界，公共参数、返回值与错误结构尚未冻结，暂不填入 `Any`、万能字典或猜测字段。

## 待确认后才能补齐的声明

1. `AgentApplicationPort`：CLI/Python API 的输入、同步/异步、结果格式、事件订阅、退出码和 `resume` 标识。
2. `RunSupervisorPort`：循环驱动、动态 Intent、路径耗尽、Budget 和终止返回值。
3. `StagePlannerPort`：StageContext、前置/后置条件、时间片和计划返回值。
4. `ProcedureSelectorPort` / `ProcedureRunnerPort`：ProcedureFact 字段、版本、匹配排序、校验失败和执行失败路径。
5. `ToolGatewayPort` / `PolicyPort`：T3 File Tool 已确认，其他 Tool 的权限粒度与 Scope/ROE 仍需确认。
6. `MCPManagerPort` / `SandboxRuntimePort`：T3 方法已确认，其他 Tool、远程 MCP 和真实 Docker 验收仍需确认。
7. `ExecutionRecorderPort`：T3 执行记录、保留、截断、Hash 与脱敏已确认；一般 Artifact 的扩展字段和清理 API 仍待后续确认。
8. `FactVerifierPort` / `GoalEvaluatorPort`：验证器输入、目标完成规则、ResultFact/TaskResult 字段及误判处理。
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

`FileToolRecorder(directory, sensitive_values=())` 显式接收证据目录与敏感值。`EvidenceStorageError` 表示证据写入/完整性/请求指纹冲突；`ToolRecoveryRequired` 表示已有不完整请求、历史执行未确认停止等情况，禁止借重试该请求重新派发命令。Controller 的提交后重载错误仍原样保留，不包装成工具失败。

本轮只确认上述 Python 内部类型及本地证据文件，不冻结 CLI、外部 TaskResult 或通用远程 wire protocol。T3 不提供删除运行中证据的 API；调用方应在不再需要审计或控制面恢复后显式清理整份证据目录。

## T4 已确认边界，具体签名待确认（2026-09-13）

单 Intent direct 循环返回当前 Intent 目标已满足/未满足/无法确定的判断。Fact 验证通过不等于 Intent 目标满足，Intent 目标满足也不等于 TaskRun 目标满足；完成确认、TaskRun 终态和最终 TaskResult 由上层按已有契约处理。T4 不因文件类型 Fact 成立就调用任务完成确认入口。

文件类型候选采用专用结构，表达被检查对象、工具识别结果和来源 Execution 引用，不让验证器理解任意自然语言候选。具体字段、输出规范化、评估类型和签名尚未冻结。

当前 Intent 只有 description，没有独立的结构化目标；旧 `verify_fact` 接口的 `turn_target_satisfied` 明确表示 Turn 目标判断，不能暗中改作 Intent 满足或候选内容有效。新增目标契约、验证入口的兼容方式及评估与持久化状态的关系，见 `T4_proposal.md` 的待确认建议；确认前不补齐 Python Protocol。
