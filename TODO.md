# Qinyuan_Agent TODO

> 本文件依据 `Qinyuan_Agent_confirmed_flow.svg`、当前仓库和用户已确认的决策维护。
> 未确认的功能、字段、状态、算法、错误语义和外部授权不得自行推断。

## 1. 项目目标

构建一个独立的自动化渗透测试 Agent（Qinyuan_Agent_new），由本仓库拥有 TaskRun 内的调度权。它接收一次题目任务，创建一个 TaskRun 和一个专属 Sandbox，在同一 Sandbox 内按 Intent 的可能性顺序逐个进行深度优先尝试；首期由 direct 模型生成方案，经 ToolGateway、Policy、MCP Manager 和 Sandbox 执行，记录可追溯证据，验证后提交 Fact，直到目标完成或输出有边界的结果。

多题目的自动获取、调用 `qinyuan-agent-new` 和向题目平台提交 flag，由后续独立转换器负责。本项目当前聚焦 Agent 能力与公共调用入口，T5 不再包含 qinyuan-bat 专用转接。转换器的接口和运行规则需另行确认。

目标链路：

```text
CLI -> TaskRun -> 一个 Sandbox -> Intent 深度优先
    -> direct Model -> ToolGateway -> Policy -> MCP -> Sandbox
    -> ExecutionRecord/Artifact -> ResultFact -> GoalEvaluator
    -> TaskResult 或下一轮 Intent
```

完成目标必须同时满足：接口边界清晰；核心不依赖外部 SDK；原始输出不能直接成为事实；控制面变更可原子提交、恢复和审计；主路径及关键失败路径有实际验证证据。

## 2. 本次交付范围

- [x] 将项目目标写入本 TODO。
- [x] 根据已确认流程图整理 `Architecture.md`，说明控制链、模块职责、数据所有权和依赖方向。
- [x] 新增 `agent_core/interfaces.py`，声明已确认的生命周期、控制面、模型、证据和边界 Protocol；只声明签名，不实现业务函数。
- [x] 新增 `interface.md`，说明 Python 接口、复用类型以及尚未冻结的接口字段。
- [x] 新增 `protocol.md`，记录 CLI/Python API、模型适配器、MCP、Sandbox、持久化、事件、版本、错误和取消语义。
- [x] 新增 `restrict.md`，记录全局约束和技术决策。
- [x] 架构声明阶段未实现 Stage、Procedure、RAG、真实 CLI、真实模型或外部执行链路；T3 后续按用户确认接入真实 MCP，Sandbox 仍只做模拟验收。

## 3. 已确认架构与全局约束

1. CLI 和 Python API 统一进入 `AgentApplication`；首期实际入口只确认 CLI，具体 CLI wire contract 仍待确认。
2. 本仓库是 Agent 在 TaskRun 内的唯一调度所有者，负责内部调度、重试、状态机和业务判断。后续独立转换器负责多题目获取、调用 Agent 和提交 flag；批次调度、并发及重试规则待确认，不复制 Agent 内部控制逻辑。
3. `TaskRun` 是一道题目的一次完整解题实例。接收后创建全局唯一 Sandbox；所有 Intent 轮复用它，完成、取消或 Deadline 后销毁；重新运行必须创建新的 TaskRun 和 Sandbox。
4. 同一轮的 Intent 按生成模型给出的可能性顺序逐个尝试，采用深度优先回溯；首期不并行操作同一 Sandbox。 `intent_type` 可作为元数据，首期不改变调度。
5. 首期只走 direct 方案。RAG、retrieve_first、匹配阈值、候选仲裁、双 LLM 并行和 RAG 强制路径均延期。
6. 工具路径固定为 `ToolGateway -> Policy -> MCP Manager -> Sandbox`。Policy 必须在副作用前完成权限和目标范围检查。
7. 环境故障（本机、MCP、Sandbox、Docker）与目标任务失败必须按记录区分。环境故障保留重新申请/调用路径，不能直接加入策略失败集合。
8. 原始 stdout/stderr 不能直接成为 `ResultFact`。Fact 必须经过后置条件、证据、目标范围和时效验证，再由原子提交边界写入。
9. SQLite 是已冻结控制面的唯一事实来源；状态、一个 RuntimeEvent 和一个 Checkpoint 在同一事务提交。内存只缓存已提交快照。
10. Deadline 或取消时只能使用已验证 Fact 形成 `partial` / `inconclusive` 结果；两者的完整外部语义仍待确认。
11. 核心领域只依赖类型和 Protocol，不依赖 CLI、SQLite、Docker、MCP SDK 或模型 SDK；系统组装集中在边界入口。
12. 密钥不得进入源码、日志、Artifact、模型上下文或版本库；输出和错误必须有界并按需要脱敏。

## 4. 已验证历史与证据

### 第 1 轮 A：基础请求与 TaskRun 生命周期

- 已实现 `TaskRequest` 的 `goal / target / deadline_seconds / model_backend`，以及 HTTP、HTTPS、TCP Endpoint 和 `local_path / url` Attachment 校验。
- 已实现全局 UUID TaskRun、总 Deadline、180 秒完成确认期、终态迁移和 Deadline 优先的 `interrupt -> destroy`。
- 已实现纯 `SandboxPort` 与 Fake Sandbox，未连接真实 Docker。
- 证据：2026-08-31，`tests/test_core_round1.py` 为 `16 passed`。

### 第 1 轮 B：核心领域契约

- 已实现 `Turn -> Intent -> Plan -> Execution -> Fact`、固定状态枚举、非法迁移拒绝、priority 顺序和深度优先回溯。
- 已实现一次环境故障的命令生成重试、一次实际 command 执行重试，以及 `policy_rejected`、`parameter_error`、`outcome_unknown`、`model_error`、`target_mismatch` 分流。
- 已实现 `PlanValidationRecord`、独立只读证据端口、TaskRun/Plan/步骤/时间/attempt 校验和执行证据边界。
- 证据：`tests` 为 `43 passed`，`agent_core tests` compileall 通过。

### 第 2 轮：模型契约与 Fake 适配器

- 已实现供应商无关的 `ModelRequest`、Intent/Plan 候选、`ModelResponse`、`ModelError`、取消和截止时间端口。
- 已实现结构校验和同一适配器最多一次纠正；Fake Codex、Fake Claude Code、Fake DeepSeek 仅用于协议测试。
- 未实现真实 CLI、DeepSeek 网络调用、凭据读取、Tool Calling 和模型装配。
- 证据：2026-09-02，模型测试 `22 passed`，全套 `65 passed`。

### 第 3 轮：控制面持久化与原子提交

- 已实现 SQLite schema/migration、`ControlStore`、`ControlMutation`、幂等提交、陈旧写入拒绝、事务回滚、受控 Event 和 Checkpoint。
- 已实现 TaskRun/Turn 注册、Controller/Registry 的 start/attach/close、提交失败补偿、提交后重载、缓存失效保护、显式 `advance_time`、候选确认、终态持久化和 Sandbox 清理。
- 已实现 Plan 校验证据两阶段提交、执行/Plan 证据分层、上下文有界读取和运行中 Execution 的崩溃恢复；T1 已完成 TurnManager 工作副本到 Controller 原子提交的接入，并完成提交前候选状态与外部证据复核。
- 控制面保存的对象为 `TaskRun / Turn / Intent / Plan / PlanStep / Execution / Fact / FailureSummary / PlanValidationRecord / RuntimeEvent / Checkpoint`；不保存实际 command、原始 stdout/stderr、附件内容、Token 或模型原始响应。
- 历史证据：本轮早期为 `115 passed`；T1 完成后的全套测试为 `196 passed`，复核阶段新增回归测试 43 项。

## 5. 模块边界与接口任务

### 已声明并可复用的接口

代码位置：[`agent_core/interfaces.py`](agent_core/interfaces.py)。

- `TaskRunLifecyclePort`：start、attach、status、advance_time、Turn 注册提交、候选确认、cancel、conclude、recover、close。
- `TurnRegistrationPort`：从已提交快照和有序 Intent 候选准备 pending Turn，不执行存储提交。
- `ModelReasonerPort`、`ContextBuilderPort`、`ClockPort`：分别负责已确认的模型调用、有限 Fact/失败上下文查询和 UTC 时间。
- `ControlStatePort`、`AtomicCommitterPort`：读取已提交控制面和提交 typed `ControlMutation`。
- 复用现有 `ModelAdapter`、`ControlStore`、`SandboxPort`、`ExecutionEvidencePort`、`PlanValidationEvidencePort`、`ModelErrorEvidencePort`。
- T3 已补齐 `ToolGatewayPort`、`PolicyPort`、`MCPManagerPort`、`SandboxRuntimePort`、`ExecutionRecorderPort`；具体类型与异步方法见 `agent_core/tool_contracts.py` 和 `interface.md`。

### 仅声明职责、等待字段确认的模块

仅保留职责、尚未补齐方法的边界 Protocol：`AgentApplicationPort`、`RunSupervisorPort`、`StagePlannerPort`、`ProcedureSelectorPort`、`ProcedureRunnerPort`、`FactVerifierPort`、`GoalEvaluatorPort`、`EventPort`。

这些模块暂不使用 `Any` 或万能字典补齐签名。`StageContext`、`TaskResult` 等后续类型仍待确认；T3 工具请求、授权、命令和执行记录类型已经确认并实现。

## 6. 后续实施任务

### T1：完成控制面工作副本接入

状态：已完成。新增 `TurnWorkingCopy`：从已提交 `ControlStateSnapshot` 恢复可丢弃领域副本，Plan、Execution、Fact 和失败推进只生成 typed `TurnOperation`，由 `TaskRunController.commit_turn_operation` 统一原子提交并重新加载。保留旧内存接口；持久化 Controller 拒绝直接走可变 TurnManager。已恢复 PlanValidation 消费、环境故障重试次数和同一 Sandbox 的 Execution 串行约束，防止重启后重复消费或重复重试。

T1 完成记录（2026-09-08，用户确认）：Controller 在提交前复核整组候选状态和外部证据；SQLite 继续只负责结构、归属、状态、幂等、Checkpoint 和事务原子性。提交前复核发现证据失效或查询异常时，不产生新的数据库记录、缓存推进或 Sandbox 副作用。幂等重放不重复查询证据或执行副作用。系统已禁止跨 TaskRun 写入和已有对象更换父级，禁止 `Plan.completed + Execution.running`，并保留受限的崩溃恢复例外。提交后重载失败仍通过 `CommittedStateReloadError` 区别于事务失败，缓存失效不代表回滚。具体保证与证据并发失效边界见 `protocol.md`。

### T2：TaskRun 与真实 Sandbox 生命周期

状态：第二轮模拟验收通过（2026-09-09，用户确认）；真实 Docker 仍未验收，待单独授权和运行环境确认。

第二轮复核指出“终态已提交但自动重载失败后，缓存失效堵住清理和 Registry 释放”的 P2 缺口；现已补充 `reload_state()`。当前所有者显式复核状态/证据并恢复缓存后，再调用 `cleanup_sandbox()`、`close()`，可由新 Controller 使用同一个 Registry 绑定。重载和业务重放均不访问 Docker、不写入新记录。修复验证见末节。

本轮已接入 `TaskRunSandbox` 生命周期适配器，并由持久化 Controller 绑定活动 TaskRun 的精确 Sandbox ID、TaskRun 归属、运行状态和配置。单个 TaskRun 只能尝试创建一次 Sandbox；活动绑定拒绝缺失、非运行、归属不符或配置不符的容器；终态绑定只允许清理，禁止执行。适配器固定使用 `network_mode=none`、无宿主目录/Docker socket/端口发布、只读根文件系统和 `/workspace`/`/tmp` tmpfs；T2 测试代码只发出固定的 `python3` 脚本请求，执行入口不校验脚本内容，不能代替 T3 Policy。

取消/Deadline 的清理顺序为：提交并成功重载终态 -> 停止接收新执行 -> 关闭输出流 -> 最多一次 `stop(timeout=2)` -> 查询确认已停止 -> 删除；查询仍运行或 Docker 查询/删除失败报告 `outcome_unknown`，不发出重叠 stop。业务 mutation 重放只返回首次提交结果，不访问 Docker；失败清理通过显式 `cleanup_sandbox()` 重试。创建返回超时或结果不明时按 TaskRun 标签定位，确认单一且配置匹配的运行容器后才允许绑定，不创建第二个容器。

模拟验收：`tests/test_task_run_sandbox.py` 验证创建一次、同 Sandbox ID 的两次固定脚本调用、跨 TaskRun 归属隔离、精确绑定、终态执行拒绝和 `stop -> remove` 顺序；真实容器的文件复用、网络隔离、资源限制和进程终止尚未宣称通过。

### T3：ToolGateway -> Policy -> MCP -> Sandbox

状态：**T3 切片完成，验收通过**（2026-09-11，用户确认；依据已有实现及 2026-09-10 的 **323 passed** 记录）。完成范围为 `ToolGateway -> Policy -> MCP -> Sandbox`，不代表完整 Agent 设计目标已经完成。

用户逐项确认接入 `File_Mcp.inspect_file`，采用真实本机 stdio MCP + Fake Docker 验收；明确授权本 TaskRun 的 `/workspace/input/` 普通文件，固定测试文件为 `/workspace/input/sample.txt`。不联网、不提供写入工具，测试夹具预置文件；拒绝相对路径、穿越、各级符号链接及非普通文件。已有记录显示全套 **323 passed**，静态、类型、格式、编译和构建通过。

**不计入 T3 完成范围**：真实 Docker、完整 Agent 链路、其他 MCP、Fact 验证、GoalEvaluator、CLI。完整的 `CLI -> Model -> Intent 调度 -> Tool -> ResultFact -> GoalEvaluator -> TaskResult` 尚未完成端到端验收；已有核心 Fact 规则测试不等同完整 Fact 验证链路已完成。后续能力仍按各自切片确认与验收。

T3 已实现异步 `ToolGateway`、输入及实际命令双重 Policy、独立 File MCP 命令生成适配器、受控文件描述符检查脚本、T2 Sandbox 执行适配器和独立持久化证据。执行前经 Controller 原子保存 PlanValidation 和 pending/running Execution；成功及失败结果经同一 Controller 提交。SQLite 不保存实际命令和原始输出，未自动创建 Fact。

已确认规则：MCP 启动/调用各最多 10 秒；命令默认 30 秒、最高 60 秒，受总 Deadline 限制；只允许调用方显式发起一次确定环境故障重试，不自动重试、重启、重建或重放未知命令。相同 request_id 返回首次完成结果，参数/授权冲突拒绝；残留开始记录或提交中断要求显式恢复。取消/Deadline 先提交并成功重载终态再清理，独立审计随后保存，终态控制面不再补写输出。

证据使用调用方指定目录，stdout/stderr 各最多 64 KiB，配置敏感值脱敏、摘要最长 256 字，保留至显式清理。协议和错误分类见 `protocol.md` 的 T3 节；真实 Docker 文件安全与进程终止仍不在本轮验收范围。

用户最后确认的 `ExecutionRecord.effective_timeout_seconds` 已加入：记录经剩余 Deadline 下调后的实际派发上限；未派发为空，执行结果未知或执行链中断时仍保留该值。原 MCPCommand 的请求上限单独保存，幂等重放及重新读取证据不推算或修改实际值。

### T4：单 Intent direct Tool-Calling Loop

状态：**T4 已确认切片完成，独立复验通过**（2026-09-24，用户确认）。独立复验全套 **434 passed、1 skipped，43.86 秒**；真实本机 File MCP 端到端测试通过，仅完成当前 Intent。真实模型、真实 Docker、CLI 和完整 TaskRun 链路仍不计入此次验收，详见本文末尾的独立复验记录。

用户确认本轮完成可重复验证的单 Intent direct 链路，使用 Fake Model、真实本机 File MCP、Fake Docker 和真实持久化。首条业务事实描述“获授权文件的工具识别结果”，采用专用结构化候选和确定性验证；Fake Docker 输出来自夹具，不证明真实文件确实具有该类型。

Intent 目标与 TaskRun 目标必须分开。例如 TaskRun 目标为“找到 flag”，识别附件类型只满足当前 Intent。T4 验证并提交文件类型 Fact，返回当前 Intent 已满足/未满足/无法确定的判断；不能据此触发 TaskRun 完成确认或宣告任务成功。完成确认、TaskRun 终态和最终 TaskResult 由上层按已有契约处理；取消/Deadline 仍走 Controller 的已确认生命周期路径。

当前 Intent 目标绑定随 TaskRun 保留，Intent 结束后不删除；可信调用方在首次执行前提供并持久化 `FileTypeIntentGoal`，模型不能生成或修改。上层先持久化 `Intent.running`，再在首次生成 Plan 前调用 Controller 绑定；Controller 核对归属、运行状态、任务范围和当前授权，并将目标、事件和 Checkpoint 原子提交。未绑定目标时 T4 拒绝启动，旧 Intent 也不自动补目标。重试和恢复必须核对同一目标；Controller 拥有目标写入和 Intent 完成状态的最终提交权，评估器只读，循环只提出完成请求。目标使用独立的一对一 `intent_goals` 控制面表，schema 2 引入该表，旧库按版本原子迁移，保留既有数据（2026-09-23 已确认）。绑定入口、三态返回、不可变目标及六个错误码已实现。

七项绑定规则已确认（2026-09-23）：`local_path` 来源引用不自动映射 Sandbox 路径，首期需可信附件导入记录明确映射，URL 不作为 `file_type` 目标；绑定指纹只含操作类型和四个目标字段，授权为实时准入条件；同 mutation 同目标重放首次结果，不同目标报 `mutation_conflict`；新 mutation 同目标重验当前授权，成功返回已有绑定，不写新事件/Checkpoint；Intent 结束后新 mutation 一律拒绝；返回专用 `IntentGoalBindingResult`，结果标记为 `created`/`replayed`/`existing`；使用列出的六个稳定错误码，首期仅 `file_type`。绑定仍须核对已确认的成功导入收据；Fake Docker 夹具路径本身不是可信映射。

最小本地附件导入链路已纳入 T4：可信上层执行导入并持久化不可覆盖的映射收据，绑定只读取该收据；Fake Docker 夹具模拟容器内文件，不作为真实 Docker 导入验收。独立 `AttachmentImportSandboxPort` 负责独占新建及读回，不走模型或 T3 ToolGateway；正常导入的写入与读回共用同一 Sandbox 准入锁。单文件最多 16 MiB，来源读取、写入和读回共用最多 60 秒且受 TaskRun 剩余 Deadline 限制。

导入路径规则已确认：可信调用方显式提供 TaskRun ID、已绑定 Sandbox ID、启动时记录的绝对来源根目录、附件索引及规范化的 `/workspace/input/...` 目标路径。导入器只接受该索引对应的本地附件，拒绝来源越界、符号链接、非普通文件及目标覆盖；同名不自动形成映射。schema 3 的 `task_runs.source_root` 和持久化启动参数已实现：提供时须是现存规范绝对目录并与启动事件、Checkpoint 同事务保存；旧记录为 NULL，不可执行本地导入。

导入收据和重放规则已确认：独立不可覆盖、带校验和的本机证据收据记录 `import_id`、TaskRun/Sandbox、附件索引、原始 `local_path`、来源根目录、目标路径、字节数、SHA-256 与完成时间，不保存内容；写入 Sandbox 后须读回校验再签发。接触 Sandbox 前先保存含原始摘要的请求声明；声明落盘起独占附件索引和 Sandbox 路径，未知结果也阻止新 ID。同 ID 同请求成功重放只读返回，不重新读取可能已修改或删除的源文件；同 ID 参数冲突拒绝。声明存在而收据缺失时停止自动重派，仅显式恢复读回匹配原摘要才补签，否则保持结果未知。声明后取消/超时按未知结果处理。导入服务、证据适配器和 T4 循环的本地验证已通过；真实 Docker 导入仍未验收。

授权保存与恢复已确认：`intent_goals` 不保存授权快照或凭据；首次绑定与新 mutation 同目标调用核对当前授权覆盖精确路径和 `file.inspect_file`；同 mutation 精确重放只读首次结果。恢复后每次新工具派发重验当前授权，授权不足停止继续且保留原目标。T3 的单次工具请求收据不延长授权有效期。

首条候选固定为 `FileTypeFactCandidate(path, observed_type, execution_id)`。验证要求成功且停止已确认、未超时/取消、双流未截断、stderr 为空、stdout 单行非空白；最多去掉一个末尾换行，正文禁止控制字符和 Unicode 行分隔符，类型文本最多 200 字且逐字匹配。短 Fact 文本为“工具文件类型识别结果：<observed_type>”，完整路径留在独立证明和目标记录。证明文件不可覆盖；schema 4 的 `verification_ref` 对旧 Fact 为 NULL，对 T4 新 verified Fact 必填。专用 Controller 操作在复核证明、Execution 和归属后原子写入 Fact、事件和 Checkpoint，同 mutation 同指纹仅返回首次提交。单 Intent 只接受单步骤 Plan，模型工具请求固定绑定步骤 0，成功 Execution 后显式完成 Plan。历史 `target_ref=None` 不改写。已确认每 Intent 最多 2 次 Plan 生成调用、8 次动作调用、首期最多一次成功工具派发；schema 5 独立台账在模型调用前/后原子记录，未知调用不自动重调。执行循环现通过 schema 6 声明关联固定副作用 ID；完整冻结契约见 `T4_proposal.md`，验收范围及实际结果见本文末尾。

### T5：独立 Agent 的 CLI 与事件

状态：范围调整已确认（2026-09-24）；CLI 与事件契约等待确认，尚未实现或验收。将已验证的 Agent 入口暴露为 CLI；取消 qinyuan-bat 专用转接任务，不将多题目获取或平台 flag 提交纳入 T5。

- [x] 确认 Agent 与后续独立转换器的职责划分，并同步架构、接口、协议和全局约束文档。
- [x] 同步 `Qinyuan_Agent_confirmed_flow.svg`：独立转换器经待确认的公共入口调用 Agent，标明 T5 范围及完整链路尚未验收。
- [ ] 确认 CLI 输入载体、输出格式、同步/异步、事件协议、退出码及 `resume/cancel` 语义。
- [ ] 按确认后的契约暴露已验证入口，明确本次 CLI 能力范围并完成验收；T4 单 Intent 入口不能被标为完整 TaskRun 链路。

T5 不要求兼容 qinyuan-bat wire protocol，也不预定后续转换器使用 CLI、Python API 或其他调用方式。

### T6：首条纵向链路验收

状态：完整 Agent 尚未完成端到端链路，T4 单 Intent 切片已通过；等待 T5 及整体调度、任务目标判断等后续能力。T1、T2 模拟验收和 T3 工具切片的已有结果不能替代完整 Agent 验收。使用 Fake Model/Tool 或用户允许的本地 Sandbox 命令完成 `CLI -> Model -> Intent 调度 -> Tool -> ResultFact -> GoalEvaluator -> TaskResult`，再决定是否申请真实目标。

### T7：Stage、Procedure、RAG

状态：延期。StagePlanner、ProcedureSelector/Runner、ProcedureFact、失败策略记忆、RAG 查询与候选生命周期必须分别确认后再实现。

### 后续独立转换器（不属于 T5）

状态：职责方向已确认，设计与实现待后续单独确认。负责多题目的自动获取、调用 `qinyuan-agent-new` 和向题目平台提交 flag。题目平台与认证、题目/附件映射、调用方式、批次调度与并发、失败重试、提交去重及平台回执处理均未冻结。转换器的存放位置与部署方式也未决定；这些问题不作为 Agent T5 的验收前置条件。

本次范围修订以用户 2026-09-24 的决定为准，已同步至流程图，替代原图中“qinyuan-bat（未来可选）/只做基础调用、协议转接”的旧入口约定；T1～T4 已验证历史及验收边界继续保留。

## 7. 待用户确认的问题

以下问题会改变公共接口、状态、权限、外部副作用或验收方式；在得到答案前保持等待确认。

1. 独立 Agent 的 CLI 接收任务使用参数、JSON 文件、stdin 还是其他方式？输出人读文本、JSON、文件还是组合？采用同步返回还是异步任务句柄？是否实时事件？退出码、`resume/cancel` 的标识、入口及语义如何定义？
2. T2 已确认只用 Fake Docker 和固定写入/读取/等待脚本模拟验收；后续真实 Docker 或真实目标验收仍需确认环境、镜像及授权范围。
3. T3 File Tool 的故障证据、分类与单次显式重试已确认；其他 Tool 和真实目标的重试、重启/重建、换 Tool 规则是什么？
4. Procedure 执行的非零、超时、权限拒绝和后置条件失败分别回到同一 Stage、下一 Stage、下一 Intent 还是终止？
5. Procedure 校验失败时，是否只丢弃 Procedure Candidate、保留 ResultFact Candidate，或整个 Item 不提交？是否写失败记忆？
6. `ResultFact Candidate` 与 `ProcedureFact Candidate` 如何汇合为一次 `completed Item` 原子提交？
7. “相同策略”的判定、失败记忆范围/格式/有效期，以及精确 command 去重字段、作用域、硬拦截/提示方式是什么？
8. Stage 的前置/后置条件、ContextBuilder 相关性和 GoalEvaluator 的确定性输入是什么？
9. 是否采用补充图的“每轮新开 LLM、只注入相关 Fact/失败摘要/Skill 规则、允许读取工作目录”设计？ `Process_Fact` 是否等同 `ProcedureFact`？
10. T3 内部工具类型已冻结；其他 Tool 的扩展、通用外部版本/序列化以及 TaskResult 字段是什么？
11. T3 工具入口已确认异步；应用级 Python API 需要同步、异步还是两者？Event 的顺序、持久化、重放、订阅和背压规则是什么？
12. 真实 Codex/Claude/DeepSeek 的入口、模型名、端点、凭据来源、子进程权限、超时、取消和原生/JSON Tool Calling 规则是什么？
13. 后续独立转换器对接哪些题目平台，通过何种公共入口调用 Agent？题目与附件如何映射为任务，如何接收可提交的 flag，如何处理批次调度、并发、重试、重复提交和平台回执？以上留待转换器设计阶段逐项询问，不在本次补写契约。

## 8. 验收矩阵

| 场景 | 必须观察到的结果 | 状态 |
| --- | --- | --- |
| 合法请求 | 创建唯一 TaskRun 和 Sandbox | T2 模拟已验证；真实链路待验收 |
| 多 Intent | 按概率顺序、深度优先、同 Sandbox 顺序执行 | 核心 Fake 已验证 |
| 工具越权/范围越界 | Policy 在副作用前拒绝 | T3 已验证，拒绝时无 MCP/Sandbox 调用 |
| MCP 命令生成失败 | 有 PlanValidationRecord 和证据引用，不创建虚假 Execution | T3 已验证，独立审计保留拒绝/失败记录 |
| 环境故障 | 与目标失败分开记录，保留重新调用路径 | T3 已验证 File Tool 分类及一次显式重试；真实 Docker 故障待验收 |
| 目标任务失败 | 短失败原因和证据引用，按确认规则回溯 | 路径仍待确认 |
| Fact 候选 | 未验证内容不能进入 ResultFact | T4 文件类型证据链及拒绝分支已验证 |
| 原子提交/幂等/恢复 | 不产生半提交，不重放未知副作用 | 控制面已验证 |
| 取消/Deadline | 停止副作用，清理 Sandbox，只输出已验证事实 | T2 模拟顺序已验证；真实 Docker 待验收 |
| 终态重载失败后的恢复 | `reload_state() -> cleanup_sandbox() -> close()`；释放 Registry 后允许同 Registry 再绑定；重载及业务 mutation 重放不访问 Docker | T2 第二轮模拟验收通过；重载失败仍保持缓存失效 |
| 全局检查 | 相关测试、模块测试、compileall、静态检查和 `git diff --check` 通过 | T4 全套 434 passed、1 skipped；检查范围和构建结果见末节 |
| T3 已确认工具切片 | `ToolGateway -> Policy -> MCP -> Sandbox` | 2026-09-11 用户依据已有实现及 323 passed 记录验收通过 |
| T4 单 Intent 切片 | Fake Model → File MCP → Fake Docker → Execution → verified Fact → Intent 完成 | 2026-09-24 用户确认独立复验通过，434 passed、1 skipped；仅完成当前 Intent |
| T5 独立 CLI 与事件 | 按已确认的公共契约调用 Agent 并输出结果/事件；具体格式及退出语义待确认 | 范围调整已确认，契约及实现待确认；不含专用转接、多题目获取或平台 flag 提交 |
| 完整 Agent 链路 | CLI、模型、Intent 调度、工具、Fact 验证、GoalEvaluator、TaskResult 连通 | 尚未完成端到端验收，不计入 T3 或 T4 完成 |

## 9. 当前验证记录

以下保留 T1～T4 的历史验证记录。用户于 2026-09-08 确认 T1 结果，于 2026-09-09 确认 T2 第二轮验收结果；2026-09-10 按逐项确认的 T3 范围完成实现与最终验证。2026-09-24 的 T4 实现验证与独立复验分别记录，保留各自耗时及验收范围。

2026-09-11 用户确认最新验收结论：T3 切片完成，依据已有实现及 **323 passed** 记录验收；上述范围外能力均不计入 T3 完成。本次只同步文档，未重新运行测试或构建。

临时目录约束（2026-09-11，用户确认）：**保留 `.venv/`；测试、缓存、构建及临时产物统一放入 `.test/`**。正式 TaskRun 数据和题目附件不作为临时产物迁移；具体命令与目录配置见 `restrict.md` 的“临时文件目录”。

- T1 接入前基线：全套测试 `118 passed`；首次接入后最终记录为 `153 passed`（先前记录的 149 是中间结果）。
- T1 完成后的回归验证：新增回归测试 **43 项**，全套测试 `196 passed`。
- 本次验证：`uv run --cache-dir .uv-cache --no-project python -m compileall -q agent_core tests`，通过。
- 本次验证：`git diff --check`，通过。
- T1 回归测试现为 **78 项通过**（本轮新增 43 项），覆盖完整数据库不变、缓存未推进、Sandbox 未清理、修复证据后同 mutation 首次提交、证据不可用时重放、修改前后跨任务归属和普通操作冒用恢复例外。
- 静态检查：`uv run --cache-dir .uv-cache --no-project --with ruff ruff check agent_core/control_candidate.py agent_core/control_state_repository.py agent_core/controller.py agent_core/turn_manager.py agent_core/sqlite_control_store.py tests/test_turn_working_copy.py --select E9,F`，通过；只声明本次修改模块范围，不宣称全库 lint 无历史问题。
- 类型检查：`uv run --cache-dir .uv-cache --no-project --with mypy mypy agent_core/control_candidate.py agent_core/control_state_repository.py --follow-imports=silent --ignore-missing-imports --cache-dir .uv-cache/mypy`，两份候选校验模块通过。
- 格式检查仅覆盖本轮新文件 `agent_core/control_candidate.py` 与本轮回归测试 `tests/test_turn_working_copy.py`，避免全库格式改写。
- T1 时 `uv build` 因 Windows 拒绝构建临时目录访问而失败，另有现有配置声明 `agent.py` 入口但文件不存在的警告；T2 重跑结果见下文。
- 验收边界：提交前复核发现证据缺失或查询异常时，不产生新的状态写入、mutation、event、checkpoint；不承诺消除复核之后的并发失效。路径/ID/Hash 索引不等同证据保留。已被旧实现污染的数据未自动迁移或删除。
- 未能运行或失败的检查必须在这里补充原因，不得以文档完成代替验证。

### T2 验证记录（2026-09-09）

- 起始基线：196 项通过。本轮专项 `tests/test_task_run_sandbox.py` **53 项通过**；完整测试 **249 passed**。命令：`uv run --cache-dir .uv-cache --no-project --with pytest --with docker python -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-t2-verified`。
- 覆盖：持久化 Controller 与 Fake Docker 的跨 Intent/跨 TaskRun 标记和 ID 断言；创建/启动次数；创建超时后唯一资源确认；创建失败、结果未知、补偿失败及错误保留；精确恢复的缺失/停止/归属/配置/查询失败；显式取消/Fake Clock Deadline；提交前、事务、重载失败；等待命令与排队准入；重复取消和同 mutation 重放不访问 Docker；停止结果未知不重发 stop；终态恢复和显式清理。Fake SDK 驱动实际 DockerRuntime 执行代码，验证不准备旧网络、控制请求超时和迟到输出流关闭。
- 静态检查通过：`uv run --cache-dir .uv-cache --no-project --with ruff ruff check agent_core/controller.py agent_core/runtime.py agent_core/interfaces.py agent_core/control_store.py agent_core/sqlite_control_store.py Sandbox/task_run_sandbox.py Sandbox/docker_runtime.py tests/test_task_run_sandbox.py tests/test_controller.py --select E9,F`。
- 新适配器类型检查通过：`uv run --cache-dir .uv-cache --no-project --with mypy --with docker mypy Sandbox/task_run_sandbox.py --follow-imports=silent --ignore-missing-imports --cache-dir .uv-cache/mypy-t2`；不宣称全库严格类型检查通过。
- 新文件格式检查通过：`uv run --cache-dir .uv-cache --no-project --with ruff ruff format --check Sandbox/task_run_sandbox.py tests/test_task_run_sandbox.py`。`compileall -q agent_core Sandbox tests` 与 `git diff --check` 通过。
- 打包先因 Windows 沙箱临时目录访问限制失败，经用户许可在沙箱外重跑 `uv build --cache-dir .uv-cache --out-dir dist-t2`，源码包与 wheel 构建成功。既有 `agent.py` CLI 入口缺失警告仍在，不将可打包等同 CLI 可运行。打包缓存位置警告不等同已包含缓存。
- 初次测试环境缺少 pytest/docker，使用项目已有 Docker 依赖和隔离的临时测试工具环境验证，未修改项目依赖；pytest 默认缓存目录在当前 Windows 环境拒绝访问，最终命令关闭 pytest 缓存，完整测试通过。
- **真实 Docker 验收未运行**：模拟标记、隔离配置检查和等待中断不证明真实文件复用、网络隔离或进程终止。没有访问真实目标、挂载宿主目录或启动容器。需要后续确认 Docker 环境、镜像和验收执行，才可升级此项状态。

### T2 第二轮验收：重载失败后的清理恢复（2026-09-09，用户确认）

验收结论：`reload_state()` 成功恢复缓存，失败继续保持失效；显式 `reload_state() -> cleanup_sandbox() -> close()`、清理完成、Registry 释放及同 Registry 再绑定均通过。重载和业务 mutation 重放均不访问 Docker。新增回归 **15 项通过**，T2 专项累计 **68 项通过**，全套 **264 passed**；静态、类型、格式、编译、打包及 `git diff --check` 在下述范围内通过。真实 Docker 仍未验收，`agent.py` 入口缺失警告按约保留。

- 新增 `TaskRunController.reload_state()` 和端口声明。完整状态与外部证据复核、TaskRun/Checkpoint 归属、Sandbox ID 不变及 Checkpoint 不回退均通过后，才更新缓存并解除失效；失败继续持有 Registry 并保持缓存失效。仅允许未关闭且仍持有所属 Registry 的 Controller 调用。无 mutation、时间推进或 Sandbox 调用。
- 新增回归测试 **15 项通过**，专项累计 **68 项**。完整恢复链在内存 SQLite 与文件 SQLite 上各覆盖取消和 Deadline；另覆盖读取/证据未恢复、缺失/外来/陈旧快照、未绑定/已关闭/所有权丢失。断言完整数据库转储不变、Fake Docker 无新调用、重放不恢复缓存；显式恢复后清理、释放注册及同 Registry 再绑定通过。
- 本次完整测试 **264 passed**：`uv run --cache-dir .uv-cache --no-project --with pytest --with docker python -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp-t2-reload`。
- 本次静态检查范围：`agent_core/controller.py agent_core/interfaces.py tests/test_task_run_sandbox.py`，Ruff `--select E9,F` 通过；测试文件格式检查通过；`compileall -q agent_core Sandbox tests` 通过。
- 本次类型检查：`mypy agent_core/controller.py agent_core/interfaces.py --follow-imports=silent --ignore-missing-imports --cache-dir .uv-cache/mypy-t2` 通过。补充已有快照的可空类型声明，并保留 mutation ID 校验后的非空返回值，不改变业务行为。
- 构建通过：沿用已批准的沙箱外 `uv build --cache-dir .uv-cache --out-dir dist-t2`，刷新源码包和 wheel；既有 `agent.py` 入口缺失警告仍在。最终 `git diff --check` 通过。
- 真实 Docker 仍未启动或验收，本次修复只确认模拟控制面与清理恢复路径。

### T3 最终验证（2026-09-10）

- 起始基线 **264 passed**；T3 新增 **59 项**，最终完整测试 **323 passed in 30.90s**，无排除项。使用锁定版本 MCP 1.29.1，包含真实本机 stdio MCP -> Fake Docker -> 独立证据 -> Controller/SQLite 全链路。命令：`. ./scripts/test-env.ps1` 后 `uv run --no-project --with 'mcp==1.29.1' --with pytest --with docker python -m pytest -q`。
- 实际派发超时及执行中提交失败专项 **9 passed**，已纳入上述完整测试。覆盖原请求 30 秒、剩余 12.9 秒派发 12 秒、未派发为空、未知结果/异常保留秒数、证据重新读取与幂等重放保持一致。
- 其他覆盖：授权和精确路径、命令篡改、MCP 结构解析和启动预算、受控脚本拒绝各级符号链接/非普通文件、同 Sandbox 两步调用、Fake 文件预置与缺失、成功/拒绝/环境/未知分类、命令生成及执行的单次显式重试、改参拒绝、持久化收据及授权快照、取消/Deadline 先提交后清理、终态审计及重放无副作用、超时和未知停止准入、证据/Controller 写入失败、截断/脱敏/完整性、控制面不保存命令或原始输出。
- Ruff 静态检查（E9/F）在本轮新增文件及修改的接口/MCP/Sandbox/测试范围通过；十份新增或聚焦修改文件的格式检查通过。mypy 在 `tool_contracts / tool_policy / tool_gateway / interfaces / file_tool_recorder / file_tool_adapter / tool_runtime` 七份模块通过；不宣称全库严格类型检查通过。`compileall` 覆盖 agent_core、agent_adapters、Sandbox、File MCP 与 tests，通过；`git diff --check` 通过。
- 用户明确允许沙箱外完成最终测试及构建。受限环境曾有 Windows 临时目录/子进程权限拒绝，自动审批服务曾超时/限流；本次已成功完成沙箱外完整测试、构建和产物只读核验，这些早期阻塞均已解除。
- `uv build --out-dir .test/dist-t3` 成功生成源码包与 wheel。两份产物中 14 份 T3 代码/配置均与当前文件逐字节一致；未包含测试数据库、原始执行证据、Python 字节码或工具缓存。源码包中保留 `.test/qinyuan_agent.egg-info` 构建元数据；既有 `agent.py` CLI 入口缺失警告仍在，CLI 属于 T5。此前失败构建的根目录残留已保留移入 `.test/build-residue-t3/`。
- 最终构建产物：`.test/dist-t3/qinyuan_agent-0.1.0.tar.gz`（161061 bytes）；`.test/dist-t3/qinyuan_agent-0.1.0-py3-none-any.whl`（148732 bytes）。
- 验收边界：真实 Docker 未启动，Linux 内核路径隔离和真实进程终止未验收；没有访问真实目标、自动提交 Fact、实现 CLI 或模型工具循环。后续真实 Docker 验收仍需确认环境与权限；这些属于后续切片，不阻塞本轮已确认的真实 MCP + Fake Docker 交付。


### T4 最终验证（2026-09-24）

已按本轮逐项确认的契约完成单 Intent 可重复验证链路。入口为 `agent_core/intent_loop.py` 的 `IntentToolCallingLoop.run`，契约汇总为 `T4_proposal.md`；Architecture/interface/protocol/restrict 已同步。工具成功只允许完成 Plan，文件类型候选另行核验、生成不可覆盖证明，由 Controller 原子提交 verified Fact；只读评估通过后仅完成当前 Intent。

- 完整回归：**434 passed, 1 skipped in 37.26s**。在已允许本机 MCP 子进程的环境运行 `. ./scripts/test-env.ps1` 后，`uv run --offline --no-project --with pytest --with docker python -m pytest -q --basetemp=.test/pytest-t4-final-0924`。唯一跳过项是当前 Windows 无法创建符号链接的本地附件测试，不把该测试记为通过。
- 端到端测试 `tests/test_intent_loop.py::test_real_mcp_loop_commits_fact_and_only_completes_intent` 实际执行本机 File MCP，与 Fake Model、Fake Docker、真实 SQLite 和不可覆盖证据文件串联；断言 Fact 有证明、Intent.succeeded、Turn/TaskRun 仍 running、未进入任务完成确认。重复调用不增加模型、工具或控制面事件。
- 恢复覆盖：schema 5→6 原子迁移及迁移失败回滚；模型调用与 Plan/动作声明不可覆盖；历史完成调用缺声明及未完成工具收据不自动重派；Plan 注册/启动/完成、Fact/Intent 提交后重载失败按原 ID 续接；孤立证明复用；数据库重开后调用次数和请求 ID 保留；环境故障最多一次显式重试；未知执行不重派。
- 失败/中断覆盖：不受支持输出、候选拒绝、越界/多步骤/新重复检查独立拒绝码及重放；结构纠正最多一次、模型错误保留 TaskRun；取消/Deadline 在模型等待、工具等待和消费拒绝动作之前生效，迟到模型响应不派发工具、不写 Fact。同 TaskRun 两个 Intent 使用独立计数和事实、复用同一 Sandbox。
- 静态：本轮变更的核心控制面/模型/工具 Policy、所有 agent_adapters 及新增循环测试通过 Ruff E9/F；新增循环、声明及三份对应测试另通过默认 Ruff 与格式检查。未对既有全库未使用导出/测试变量问题作无关清理。
- 类型：`intent_loop / intent_action_application / modeling / model_invocation_audit / intent_goal_evaluator / file_type_fact / file_type_proof / file_type_verifier` 八份模块的 mypy 通过（follow-imports=silent、ignore-missing-imports）；不宣称全库严格类型检查通过。
- 编译：agent_core、agent_adapters、Sandbox、File MCP 及 tests 的 compileall 通过；git diff --check 通过。
- 构建：受限文件沙箱中遇到 Windows 临时目录权限错误，随后本机离线 `uv build --offline --out-dir .test/dist-t4` 成功生成源码包和 wheel。已只读检查两包无 .env/uv-cache/pytest-tmp，wheel 包含 T4 模块并可独立导入，schema 为 6。现有 `agent.py` CLI 入口缺失警告保留；可打包不等于 CLI 可用。
- 所有本轮临时产物位于 `.test/`，`.venv/` 保留，仓库根目录未遗留构建展开目录。

边界：真实模型、真实 Docker 导入/执行/隔离/进程终止、任意文件类型真实性、多步骤 Plan、自动多 Intent 调度、整体 TaskRun 目标评估/完成确认与 TaskResult、CLI、Stage/Procedure/RAG 均未由本轮验收。T4 拒绝只停止当前 run，Intent 可仍 running；调用方必须处理，不能自动空转重调。模型线程只能协作取消，真实适配器须另行验证超时和终止。

### T4 独立复验记录（2026-09-24，用户确认）

结论：**T4 已确认切片完成，本次独立复验通过**。以下结果由用户提供，本次仅同步文档，未重新运行测试或核验输入文件。

- 全套测试：**434 passed、1 skipped，43.86 秒**。这是独立复验记录，上述实现阶段的 37.26 秒记录保留。
- 真实本机 File MCP 端到端测试通过，**仅完成当前 Intent**，不据此宣告 TaskRun 完成。
- 唯一跳过项：Windows 无法创建符号链接的测试；明确记为 skipped，不计为通过。
- **143 个输入文件与当前源码一致**；独立复验未修改原项目源码。
- 验收范围外：**真实模型、真实 Docker、CLI 和完整 TaskRun 链路**，均不计入此次 T4 验收。
