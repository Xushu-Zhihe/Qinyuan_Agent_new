# Qinyuan_Agent TODO

> 本文件以 `Qinyuan_Agent_dual_llm_flow.svg`、当前仓库和用户已确认的决策维护。较早的 `Qinyuan_Agent_confirmed_flow.svg` 是不完整的第一版图。
> 未确认的功能、字段、状态、算法、错误语义和外部授权不得自行推断。

## 0. 框架状态

高层框架已基本确定，可以按切片开始实现；这不表示双 LLM 架构已实现，也不表示所有公共接口和数据协议已经冻结。已确认模型职责、Turn 与 Intent 的关系、触发式复盘、受限建议作用域、独立验证、不可变授权、影响边界、证据可见和工作区共享规则。凭据使用及敏感查看、验证器注册契约、精确授权表达、证据访问协议、持久化字段和数值阈值须在相关切片启动前逐项确认。Stage、Procedure、RAG 和多题目转换器不属于当前框架实施前置条件。

2026-10-02 用户确认后续以双 LLM 为实施主线：先核对现有 T6 基线与缺口，再按 D0～D7 分切片落地，最后验收新版真实单题闭环。旧 T6 的真实验收不作为全部新工作的前置条件；受环境权限阻塞的项目单独保留，不计为通过。当时仅更新任务安排，未启动代码改造或真实目标测试；本次 D0 结果见下文。

2026-10-02 **D0 已完成；D1 内部单 Turn 模拟切片已完成验收**：[D0_baseline_and_D1_contract.md](D0_baseline_and_D1_contract.md) 保留源码基线与确认契约；[D1_acceptance.md](D1_acceptance.md) 记录本轮实现和实际验证。`D1Application` / `D1SingleTurnRunner` 仅覆盖一个本地附件、一个候选和一个获选 Intent。D1 与旧模型专项 **98 passed**，共享控制面/旧链回归 **246 passed**；仅为 Fake 模型与 Fake Sandbox 本地模拟，不代表真实模型、真实 Docker、目标网络或 D7 公共 CLI/工作台接入已通过。

## 1. 项目目标

构建独立的自动化渗透测试 Agent（Qinyuan_Agent_new），在明确授权的目标与影响范围内自动发现漏洞和 FLAG，同时避免破坏目标环境的可用性与数据完整性。Agent 在每个 TaskRun 内独立调度，采用外层 LLM 负责 Turn 目标候选、有序 Intent、复盘和过程总结，内层 LLM 负责当前 Intent 的 Plan/Action；可信代码执行授权检查、工具准入、证据验证、状态提交和最终结果判定。

同一 Turn 的多个 Intent 是实现同一 Turn 目标的候选路径，可信侧串行选择；目标注册后保持不变，调整目标通过下一 Turn 表达。执行证据先保存，Fact 与目标结论由独立验证能力支持；用户可以通过证据视图查看来源和内容，不能被迫只靠搜索日志理解运行过程。

多题目的自动获取、调用 `qinyuan-agent-new` 和向题目平台提交 flag，由后续独立转换器负责。本项目当前聚焦 Agent 能力与公共调用入口，T5 不再包含 qinyuan-bat 专用转接。转换器的接口和运行规则需另行确认。

目标链路：

```text
CLI / 应用入口 -> TaskRun / 不可变授权与总预算
    -> 外层 LLM：Turn 目标与完成标准候选、有序 Intent、复盘
    -> 可信校验 / 候选注册 -> 内层 LLM：所选 Intent 的 Plan / Action
    -> Policy -> 专用 MCP 或通用脚本 -> Sandbox
    -> ExecutionRecord / 可查看证据 -> 注册规则的可信验证器
    -> ResultFact / Intent / Turn 结果 -> TaskRun 目标评估 -> TaskResult 或下一 Turn
```

完成目标必须同时满足：接口边界清晰；核心不依赖外部 SDK；模型不能扩大授权或直接提交结论；原始输出不能直接成为事实；控制面变更可原子提交、恢复和审计；用户可由证据引用查看执行依据；主路径及关键失败路径有实际验证证据。

## 2. 已确认的框架约束

1. 外层 LLM 生成 Turn 目标与完成标准候选、有序 Intent 候选，并承担复盘和当前 TaskRun 的 Process_Fact 提炼；内层 LLM 只对已选 Intent 生成 Plan/Action。
2. 同一 Turn 的 Intent 是实现同一目标的备选路径。候选按可信调度顺序串行执行；一个路径经验证达成目标即可完成 Turn，不要求全部路径成功。
3. 复盘对照已注册 Turn 目标、执行预期、已提交结果和证据，形成有来源的判断与策略建议；它不是可信验证或状态提交。Turn 首尾复盘；轮内只在可信代码触发或内层请求时复盘，且当前 Action 必须结束并保存结果与证据。
4. 外层复盘可建议继续当前 Intent、切换兄弟候选或重规划。重规划结束旧 Plan 未执行部分并创建新 Plan；策略性放弃与证据确认目标不匹配分别记录。切换前停止当前 Intent 的进程，无法确认停止时进入恢复边界。
5. 受限约束须明确绑定当前 Intent 或 Turn，范围结束即失效；不能扩大授权、TaskRun 总预算或 Deadline，也不能修改已注册 Turn 目标。目标调整通过创建下一 Turn 表达。
6. 过期提案丢弃。确定性拒绝可受限重新生成并保留原提案与拒绝记录；失败时先刷新已提交状态和证据上下文，再按限制重试，仍失败则暂停。未知模型调用结果不自动重发。数值与信号细节留到构建阶段确认。
7. Turn 目标注册后不可变，目标调整通过创建下一 Turn 表达。外层 LLM 选择并参数化已注册验证规则，验证由可信代码执行；没有匹配规则时不得宣告目标完成。
8. `Process_Fact` 是带来源的当前 TaskRun 总结，与 verified ResultFact 分开；成功复盘发现新执行信息时提炼，失败总结不阻断。历史保留、按对象版本判断适用性、追加版本并显式呈现冲突。
9. 证据是可查看的正式产物，不是只能从日志中查找。Fact 和 Process_Fact 通过来源引用连接证据；凭据等明文仅在受保护证据视图中按需查看，普通日志、事件和 Process_Fact 默认脱敏。
10. TaskRun 授权不可变；发现新资产不自动扩大授权。默认低影响，状态变更须明确授权，破坏性操作禁止。授权内无安全可行路径时记录证据、报告未完成并停止，不强制人工介入。
11. 工作区按 Intent 隔离。同一 TaskRun 内显式共享保留来源与原验证状态；跨 TaskRun 产物须显式导入，不继承旧授权或验证结论，并在新 TaskRun 下重新核验。TaskRun 总预算统一累计，操作另有限额，跨 Turn、Intent 切换和恢复持续记账。
12. TaskRun 是一次完整任务实例，Sandbox 为 TaskRun 独占；核心控制面、证据、模型调用台账与业务验证各有明确所有者。控制面事务、幂等和未知副作用不重放等已确认既有约束继续有效。
13. Agent 独立于后续转换器；多题目获取、调用 Agent 和提交平台 flag 归后续独立转换器。Stage、Procedure、RAG 延期。

## 3. 架构与接口任务

- [x] 将双 LLM 目标架构与实现状态分层写入 `Architecture.md`。
- [x] 在 `interface.md` 记录外层/内层、注册验证器和证据读取边界；未冻结字段不补猜测签名。
- [x] 在 `protocol.md` 记录已确认的逻辑交互顺序、复盘、安全和证据可见规则；不冒充外部 wire protocol。
- [x] 在 `restrict.md` 写入全局授权、低影响、证据、模型角色和验证规则决策。
- [x] 更新 `TODO.md` 与 `Qinyuan_Agent_dual_llm_flow.svg`，保留历史验收边界。
- [x] 同步复盘定义、触发边界、约束作用域、重新规划与跨 TaskRun 导入规则。
- [ ] 按本文件待确认问题冻结剩余公共类型、验证器注册契约、凭据使用/保护机制和证据访问方式后，再补代码 Protocol 声明；本次不新增业务实现。

## 4. 模块边界与接口任务

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

## 5. 既有任务与实施状态（保留 T1～T7 编号）

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

### T5：CLI、事件与真实单 Intent 文件类型识别

状态：**历史真实效果复核通过；本次完整独立复验受环境权限阻塞，不能宣告全部通过**（2026-09-25，用户提供复核结论）。两次历史真实成功记录均确认文件类型、verified Fact、Intent、事件和清理符合切片目标，整体目标保持 `not_evaluated`。本次 T5 专项为 **12 passed、4 failed、18 errors**，真实 Docker 与重新构建也受阻，详见末尾独立复验记录。完整 TaskRun 解题调度仍留在 T6。

以下已完成项保留实现阶段与历史本机验收证据，不代表本次独立复验全部通过。

- [x] 两阶段范围、整体目标 not_evaluated、单 Intent 切片终止规则及工具授权逐项确认；不根据 Fact 数量生成 partial/achieved。
- [x] 同步 `qinyuan-agent-new run-intent --request <JSON文件|-> --mode simulated|real --state-dir <目录>`；保留 `qinyuan-agent` 别名，模拟夹具与真实配置分开。
- [x] stdout 单个 protocol_version=1 最终 JSON，区分 Intent、TaskRun、整体目标、清理与错误。SQLite 提交后可选 JSONL 导出，独立写进程、2秒超时、不回滚；按 TaskRun ID/序号只读重放。
- [x] 每任务 UUID 目录、不可覆盖运行声明和独立证据、OS 写锁。status/终态 resume/replay 只读；新工具派发需当前授权，未知模型/工具不重派，未知导入仅显式读回。
- [x] `cancel` 通过取消请求交给持锁进程处理；`resume --cleanup-only` 独立清理。退出码 0/2/3/4/5/130 与未知结果优先级已确认并验证。
- [x] 正式模型为 `.env` 中 DeepSeek API，官方 HTTPS、JSON object + thinking disabled、不暴露工具；最多一次结构纠正，无网络/429/5xx自动重试；180秒总等待/10秒连接且受剩余任务预算限制。Codex CLI 不在本轮接入。
- [x] 真实附件导入使用固定程序/stdin、精确普通文件路径、不可覆盖、共享准入锁与 SHA-256 读回；既有镜像 `qinyuan/pentest-sandbox:0.3.4` 不构建、不拉取。
- [x] 历史真实单 Intent 链路已通过并完成效果复核：附件 -> DeepSeek -> File MCP -> Docker -> Execution/证据 -> verified Fact -> Intent satisfied -> TaskRun inconclusive -> 清理/事件。真实文件识别不证明找到 flag。
- [x] Ctrl+C 使用协作取消和可终止进程树；模型响应取消后不得派发工具/写 Fact。真实模型等待中取消 2.911 秒退出，清理成功、130、无 Execution/Fact。
- [x] 历史全量回归 **468 passed、1 skipped，150.67 秒**；新增模块 Ruff 和限定源文件 mypy 通过。历史构建与安装入口验收详见下文，不记为本次独立复验结果。
- [ ] 环境权限恢复后，补齐本次受阻的 T5 专项、完整独立复验、真实 Docker 和重新构建验证；重新记录实际结果，不能沿用历史通过数代替。

初次真实执行发现镜像不接受既有 `file --error` 长参数，已改为同语义 `-E`。旧固定程序只允许历史证据复核，新工具派发必须匹配修正版；不重写旧收据。失败 TaskRun 保留证据，并实际验证 Deadline 终态后清理未确认返回 5，显式 cleanup-only 才完成清理。

T5 不兼容预定的 qinyuan-bat wire protocol，不实现平台取题/提交，也不决定未来转换器的调用方式。正式协议见 protocol.md 的 T5 v1，使用说明见 README.md。

### T6：真实单题解题闭环

状态：T6 `run-task` 与本地工作台链路已接通，聚焦及模拟回归记录见下文；真实 Docker、模型和授权目标上的完整单题闭环仍待实际验收。T6 完整链路为 `CLI -> Model -> Intent 调度 -> Tool -> ResultFact -> GoalEvaluator -> TaskResult`。当前实现状态不等于本轮确定的双 LLM 架构已经实现；双 LLM 迁移列为后续架构落地任务。平台取题和提交 flag 仍由后续独立转换器负责。

2026-09-25 用户确认：T6 继续以 CLI 为入口，增加供用户自行复测的手工测试步骤；前端单列在 T6 闭环稳定之后，不作为 T6 完成条件。

2026-09-25 执行流对照见 `T6_flow_audit.md`：该记录描述的是当时工作台仅调用 T5 单附件文件识别的旧状态。当前工作台已改为调用 T6 `run-task --mode real`，保留只读进度、取消和暂停后清理边界；目标网络隔离仍归 T6-6。

2026-09-25 已完成 T6 通用脚本第一切片：`T6CommandGateway -> Policy -> T6 Sandbox` 直接执行任意 Bash，专用 MCP 不参与；请求收据、命令摘要、字节输出和 `ExecutionRecord` 可重放且不可覆盖。正常非零退出提交为已结束 Execution，留给后置条件验证器；可信超时清理后同一步只允许一次改命令重试，未知结果阻止新派发。聚焦测试 **33 passed**；本次未接通完整 T6 CLI/多 Intent/GoalEvaluator/TaskResult，真实 Docker 隔离和真实题目仍未验收。

随后按用户确认增加两类执行前固定且全部满足的通用后置条件：指定步骤 stdout/stderr 包含原文，或 Plan 结束时工作区普通文件 SHA-256 等于预声明摘要。三态验证区分 `satisfied`、`not_satisfied` 和 `inconclusive`；文件哈希使用普通 UID 的固定只读程序，拒绝符号链接，首次观察不可覆盖。真实 Docker 无网络探针已验证任意 Bash 的二进制输出、非零退出码、超时清理（含进程回收）、工作区复用、64 KiB 双流上限、文件哈希、符号链接拒绝和容器精确删除，报告在 `.test/t6-direct/docker-report.json`。

本批（2026-09-26）新增独立通用观察证明及 Controller 原子提交：全部固定条件满足后，已保存的证明、Execution 与文件摘要观察被再次复核，单次事务提交 verified 观察 Fact 与当前 Intent 成功。Fact 仅陈述观察成立；候选 flag 仍 pending；未配置独立答案校验器时不自动把整题置为 achieved。已修正超时重试后的证据选择、证明重复保存，以及摘要进程创建结果未知时未关闭派发入口的问题。聚焦测试 **38 passed**，覆盖事务回滚、幂等重放、证据损坏/缺失、取消/Deadline 后拒绝提交和摘要未知结果；日志 `.test/t6-final/focused.log`。全套回归 **558 passed、1 skipped、1 failed**：唯一失败为旧的真实本机 File MCP 集成测试 `test_real_mcp_loop_commits_fact_and_only_completes_intent` 返回 `tool_failed`，单项复测 **1 passed**，失败原因尚未确定，不能将本次全套写成通过。日志 `.test/t6-final/regression.log`、`.test/t6-final/mcp.log`。新证明/后置条件文件 Ruff、证明接口限定 mypy 通过；共享旧模块 Ruff 仍有历史告警。当前源码 wheel/sdist 构建成功，见 `.test/t6-final/build.log`。**多 Intent CLI、GoalEvaluator、TaskResult、真实目标网络仍未接入或验收。**

历史回归 **535 passed、1 skipped、1 failed** 的迁移夹具问题已定位并修正：模拟 schema 5 时须同时移除 schema 6/7 版本记录和 schema 7 的 `target_network_json` 列。修正后迁移单测通过；完整回归结果以上文最新记录为准。

- [ ] 在 T6 功能及 CLI 契约逐项确认后，补齐真实单题请求示例、题目/附件准备、环境与授权前置条件、实际命令和预期结果；不能将 T5 的 `run-intent` 示例当作完整解题命令。
- [ ] 手工测试说明覆盖题目提交、运行进度与事件读取、执行过程与证据检查、最终结果核对，以及取消/恢复和失败后的处理；具体操作以届时确认的 T6 契约为准。
- [ ] 为可重复测试明确 `.test/` 下的专用产物目录、TaskRun 标识和验收证据位置，区分单 Intent 满足、整题目标完成和 Sandbox 清理结果；保留 `.venv/`。

#### T6 分批交付安排（2026-09-25 用户确认）

以下保留旧 T6 的实施分批及历史状态，不作为双 LLM 主线的任务轮安排。双 LLM 新任务按第 6 节 D0～D7 执行；不将旧链路的模拟结果、直接模型行为或真实单 Intent 效果自动视为新架构验收。

下表的“代码批次”只划分实施和验收范围，不是 Agent 的运行轮数。TaskRun 的 Intent 执行轮不设置轮数上限；当前及可回溯父轮候选耗尽后，保留分类失败摘要并生成新轮，直到总 Deadline 或用户取消。模型/外部结果未知仍停在显式恢复边界。现有模型与 Turn 注册契约只要求候选列表非空；本次不新增“每轮最多 4 个 Intent”限制。

| 代码批次 | 对应流程图路径与最小交付 | 状态与验收证据 |
| --- | --- | --- |
| T6-1 执行与工作区基础 | Policy → 通用 Bash/Sandbox → Execution；附件原件只读、工作副本校验、单步时限与证据 | 已有代码、聚焦测试与禁网 Docker 探针；真实目标网络留待 T6-6 |
| T6-2 观察 Fact 与 Intent 完成 | 执行前固定后置条件 → 独立证明 → 原子 Fact/Intent/Checkpoint | **本批实现与聚焦验证完成**：38 项通过；完整回归一次 MCP 失败、单项复测通过，保留未消除的不稳定项 |
| T6-3 可恢复的有序 Intent 生成 | 已提交 Fact/失败上下文 → direct 模型 → 有序 Intent 列表持久化 | 内部链路已实现：schema 8 轮级调用台账、逐调用结构纠正、有序候选与固定注册 ID、崩溃后只读重用；已由 T6-5 `run-task` 和工作台真实模式组装 |
| T6-4 深度优先调度 | 当前最高优先级 Intent → Plan/执行 → 成功后下一轮；失败回溯同层/父轮 | **调度切片已接入 T6-5**：`RunSupervisor + IntentRunnerPort` 按 priority 深度优先推进；完整目标失败生成新 sequence；未完成执行返回 `recovery_required` 并保留 running；已结束但条件无法判断才暂停为 `inconclusive`；模型错误由审计/失败摘要只读回放并停止调度。工作台只投影该状态 |
| T6-5 完整 CLI 与临时整题结果 | CLI → TaskRun/Sandbox → 调度 → pending 候选/已验证观察 → TaskResult/清理 | **代码链路已接通，真实验收待用户执行**：`run-task` v2 和工作台真实模式以逐步 Bash 动作串联短 Plan、独立 Fact、候选台账和 Supervisor；`status/replay/resume/cancel` 复用持久运行目录。真实模式仅接本地附件，原件导入和工作副本分别留收据，暂停保留 TaskRun/Sandbox，取消或 Deadline 走清理；无答案校验器时不宣称整题 achieved。真实 Docker、真实模型由用户手工验收，目标网络归 T6-6 |
| T6-6 真实闭环验收 | 受控目标网络、附件执行、真实模型、多 Intent/回溯、最终状态及精确资源清理 | 单一公网目标、精确授权、零附件目标题、隔离转发和 Intent 关系图已接线；真实 Docker/模型/目标链路及资源清理由用户按 README 手工验收，不以模拟测试代替 |

每批只实现并验收该批目标，再更新实际结果与下一批缺口。Stage/Procedure/RAG 仍归 T7；前端继续复用正式应用入口。

T6-5 本地模拟验证覆盖 `run-task → status → replay → cancel`、追加夹具后恢复、精确授权预检、文件快照条件、动作证据损坏、导入源文件消失后的只读恢复、运行中取消等待，以及状态读取不创建证据目录。2026-09-27 最终全量回归 **649 passed、1 skipped**；本批代码定向 mypy、Ruff E9/F/I、wheel/sdist 构建和 `git diff --check` 通过。真实 Docker/模型由用户按 README 手工复测，不以模拟结果替代。

### 独立前端建设（T6 闭环稳定后）

状态：**本地工作台已接入 T6 `run-task --mode real`**（2026-09-27）。服务监听 `127.0.0.1`，提供题目/附件持久化、归档与永久清理、内存模型配置和浏览器管理页面；开始 Agent 只启动 CLI，网页通过只读接口显示 TaskRun→Turn→Intent 关系、短 Plan、Execution、Fact、pending 候选和停止原因，不改写 Agent 控制面。每题支持 0～16 个附件，运行时合计最多 64 MiB；零附件须提供公网目标。运行中题目只能取消，暂停保留 Sandbox，取消后清理。目标专用网络隔离已接线，真实网络验收由用户执行。

2026-09-27 T6-6 构造检查：本地模拟、动作与工作台聚焦测试首次 **35 passed、2 failed**（旧网络夹具仍用回环地址）；夹具改为公网字面量后网络聚焦 **4 passed**，新增恢复映射单项 **1 passed**。前端 JS 语法、Python 编译、定向 Ruff E9/F/I、wheel/sdist 和 `git diff --check` 通过；未重跑全套回归。真实 Docker、模型、DNS 与目标访问未执行；用户手工验收结果待补。

模型配置支持 DeepSeek 和自定义 HTTPS OpenAI Chat Completions 端点；API Key 只保留在本地服务进程内存，GET、数据库、日志、草稿和浏览器存储均不返回或保存。T6 Runner 的默认总时限为 60 分钟、单步 60 秒、调用次数不设上限；Sandbox 允许经 Policy 审核的 bash/二进制分析工具和工作目录写入；有目标时网络只允许该题白名单端点，无目标时完全禁网。不开放宿主网络、入站端口、宿主挂载或 Docker socket。

结果规则已确认：模型输出的候选 flag 和证据先记录为 `pending`，不能只因匹配 flag 形状或命中疑似假 Flag 就结束；没有独立验证时继续尝试直到预算耗尽或用户取消，最终由用户自行验证。仓库此前没有假 Flag 规则，题目说明中的相关描述只作为不可信输入提供给模型，不能自动变成校验器。

启动：`uv run python -m agent_adapters.workbench --port 4174 --data-dir workbench_data`。本切片测试位于 `tests/test_workbench.py`，不连接真实模型、Docker 或题目。

2026-09-27 工作台后续修正：题目列表进入独立全宽详情视图，解题图仍只表达已提交归属和轮次顺序；永久清理按钮仅在归档后显示。已终止 TaskRun 若资源清理未确认，可显式调用 CLI `resume --cleanup-only`，清理成功后工作台标为 `cleanup_confirmed` 并允许归档，原有证据恢复信息仍保留。Intent 生成提示明确同轮候选是可独立尝试的替代方向，下一轮继续从已验证 Fact 和目标失败摘要形成上下文。本次仅做静态检查、浏览器布局和模拟清理状态检查；未启动真实 Agent、Docker、模型或目标网络。

拟讨论范围：T6 任务的运行进度、执行过程与证据查看、取消和恢复。当前工作台已接入 T6 CLI；页面是控制面和证据的只读投影，不改变 `Turn / Intent / Plan / Execution / Fact` 的所有权或状态转移。

### T7：Stage、Procedure、RAG

状态：延期。StagePlanner、ProcedureSelector/Runner、ProcedureFact、失败策略记忆、RAG 查询与候选生命周期必须分别确认后再实现。

### 后续独立转换器（不属于 T5）

状态：职责方向已确认，设计与实现待后续单独确认。负责多题目的自动获取、调用 `qinyuan-agent-new` 和向题目平台提交 flag。题目平台与认证、题目/附件映射、调用方式、批次调度与并发、失败重试、提交去重及平台回执处理均未冻结。转换器的存放位置与部署方式也未决定；这些问题不作为 Agent T5 的验收前置条件。

本次范围修订以用户 2026-09-24 的决定为准，已同步至流程图，替代原图中“qinyuan-bat（未来可选）/只做基础调用、协议转接”的旧入口约定；T1～T4 已验证历史及验收边界继续保留。

### 双 LLM 目标架构落地

状态：高层职责与控制边界已确认，当前 direct 模型实现尚未接入完整规划/复盘/过程总结闭环。原先的笼统实施清单由第 6 节 D0～D7 接替；每轮实施前只冻结该切片需要的契约，不要求一次决定所有延期模块或公共 API。

- [x] 定义外层目标/Intent/复盘/Process_Fact 与内层 Plan/Action 的职责边界。
- [x] 定义 Turn 候选路径、目标不可变、可信验证、TaskRun 授权与预算边界。
- [x] 定义证据可查看且不依赖日志、敏感值默认脱敏的原则。
- [x] 明确后续采用双 LLM 主线；旧 T6 真实验收不阻塞全部新工作。
- [ ] 完成 D0～D7 各轮对应的契约确认、实现与独立验收。

## 6. 后续任务轮：双 LLM 主线 D0～D7（2026-10-02）

### 执行顺序与共同完成条件

D0～D7 是开发与验收任务编号，与运行时的 Turn/Intent 不是同一概念。T1～T6 的历史证据不重编号；T7 继续延期，编号较小不表示必须先做。

主线顺序为 `D0 -> D1 -> D2 -> D3 -> D4 -> D5 -> D6 -> D7`。D0～D2 已完成各自确认范围内的内部模拟验收；D3、D4 内部 Fake 切片已完成，D5～D7 尚未开始。旧 T5 权限受阻的独立复验、旧 T6 真实环境验证继续登记为遗留验收，不自动升级为完成，也不阻止不依赖它们的契约声明与模拟切片。若发现会影响新切片的数据完整性或副作用控制缺陷，先修复该缺陷再推进依赖它的工作。

每轮先给出当前源码依据、最小输入到输出路径、需要用户决定的契约与验收示例；未确认的功能、字段、权限或错误语义必须先问用户。确认后复用现有类型并补窄小 Protocol 声明，再实现一条路径及关键失败分支。类型/接口声明阶段不实现业务函数，不能以空接口或文档完成代替运行切片验收。

从 D1 起就要求 Policy、副作用前审计、不可变 TaskRun 授权、已有总 Deadline/局部限额、取消和未知结果不重派有效；从首次新增调用或计量起跨恢复保存账目。D4 补齐完整计量与恢复场景，D6 扩展已确认的授权/敏感证据能力，不表示这些约束可以推迟到后面才执行。没有已确认授权与保护机制的能力不开启，不自行扩大真实目标或凭据使用范围。

每轮提交实际验收记录：源码版本/差异、命令、通过/失败/跳过/受阻结果、证据位置和剩余缺口。相关行为与契约测试通过后，按该轮风险运行静态、类型、编译与必要构建检查；单纯文档修改不重跑业务测试。同步 Architecture.md、interface.md、protocol.md、restrict.md 和 TODO.md 中被该轮改变的契约；职责或运行流变化时再更新 SVG。临时产物放 `.test/`，保留 `.venv/`，正式运行数据不作为临时文件迁移。

| 任务轮 | 可观察的交付结果 | 前置依赖 | 启动前重点确认 | 状态 |
| --- | --- | --- | --- | --- |
| D0 基线核对与迁移切片 | 建立当前代码/历史验收/未验收清单，给出 D1 最小契约和迁移方案 | 当前仓库与已确认架构 | D1 首个目标、可用验证规则、旧运行兼容与双模型角色配置 | 已完成（2026-10-02），记录见 D0 文档 |
| D1 双 LLM 单 Turn 最小链路 | 外层提目标/标准/Intent，可信注册，内层执行一个获选 Intent，独立验证并提交分层结果 | D0 与 D1 契约确认 | 提案类型、规则参数、角色调用审计、单 Intent 目录及授权/预算保存 | 内部模拟切片已完成（2026-10-02） |
| D2 可恢复的规划与复盘闭环 | Turn 首尾及被触发的 Action 后复盘，可信侧接受继续建议与有作用域的约束 | D1 | 触发信号、复盘输入/输出、约束作用域、重生成/重试与暂停语义 | 内部模拟验收完成（2026-10-02），见 [D2_acceptance.md](D2_acceptance.md) |
| D3 多候选切换与重新规划 | 同 Turn 兄弟候选串行切换，旧进程停止后进入隔离目录，新 Plan 接续 | D2 | 分类与状态、目录/共享引用、停止确认、旧 Plan 收尾、候选耗尽与下一 Turn | 内部 Fake 模拟切片已完成（2026-10-04）；真实模型/Docker/公共入口未验收，见 [D3_acceptance.md](D3_acceptance.md) |
| D4 预算、取消与恢复综合切片 | 中断/重启后不重复模型调用、工具副作用、账目或状态提交，约束按版本与作用域核验 | D3 | 总/局部预算维度、限额、未知结果、过期提案、恢复入口与优先级 | 内部 Fake 切片已完成（2026-10-04）；复用现有核心及真实持久化，见 [D4_acceptance.md](D4_acceptance.md) |
| D5 Process_Fact 与普通证据查看 | 新信息的成功复盘形成带来源版本总结，用户沿引用查看执行证据 | D4 | 总结结构/版本、来源适用性/冲突、脱敏、证据读取及展示操作 | 待开始 |
| D6 授权、敏感证据与跨运行交接 | 按确认后的资产/影响规则执行，受保护查看敏感证据，显式导入不继承旧授权/验证结论 | D5 | 凭据是否可认证、敏感保护/访问、影响分类、跨 TaskRun 导入与再验证 | 待开始，含待决功能 |
| D7 应用接入与新版真实单题验收 | 同一 CLI/工作台链路展示新版分层结果；真实模型、Docker 与授权题目完成可验证闭环 | 对应能力的 D1～D6 验收及真实环境就绪 | 公共输出兼容、事件/退出/恢复、实际题目与独立答案验证能力 | 待开始 |

### D0：核对现有 T6 基线，准备第一条迁移路径

- [x] 对照当前源码核对 Intent 生成、调度、动作、Fact、TaskResult 和工作台实际调用链，区分已声明、已接线、模拟验证和真实验证；先检查工作区现有未提交改动，不覆盖来源不明的工作。
- [x] 核对旧 T5/T6 复验和不稳定项，选择能验证 D1 依赖的检查并记录实际结果；环境受阻时保留原因，继续独立的声明与模拟设计，不沿用历史测试数冒充本次通过。
- [x] 列出双 LLM 与当前代码的差异及兼容迁移方案，包含旧 TaskRun/Checkpoint/证据如何读回；不能替旧对象补造目标标准、授权、调用完成记录或证明。
- [x] 向用户确认 D1 首个示例目标、复用验证规则、提案字段、调用角色配置和兼容边界；仅补已经确认的签名与类型。验证规则不能由模型生成可执行代码替代。

本次证据：核心依赖 **355 passed、1 skipped**，可信调度器 **20 passed**，选定 T5/T6/应用/工作台专项 **177 passed、1 failed**，历史不稳定本机 File MCP 单项 **1 passed**。专项失败的两次 MCP 准备均为 `environment_fault/mcp_unavailable_or_timed_out`，未派发 Execution；准确根因仍未确定。受限环境临时目录 `WinError 5` 经批准的沙箱外本地模拟复验后取得上述结果；4 个被忽略、未跟踪的旧测试有收集错误，另一个同类旧测试仅源码检查。不能称全套测试通过，也不代表真实模型/Docker/目标网络通过。命令、源码位置、输入输出及失败示例见 D0 记录。

D1 冻结边界：只做一个本地附件、一个候选、一个获选 Intent；规则 `attachment_copy_sha256` v1 仅选择附件索引，可信导入收据固定期望摘要，目录为 `/workspace/work/intents/<intent_id>/`，内层仅固定只读检查。外层最多 2 次、内层总计最多 4 次且 probe/complete_intent 各最多 2 次；内层工具派发 1 次、首次可信摘要观察另计 1 次，导入/副本准备独立审计。开始前持久记账，禁止未计账自动重试；预算耗尽仍允许保存已开始结果、查询、取消和清理。角色配置引用固定不可变内容并在恢复时校验；已收到但被拒绝/过期/未应用的响应仍是已知完成结果，只有无法确认结果的开始记录保持未知。历史观察不证明后来文件版本，D1 不自动新观察或下一 Turn。D0 只完成契约记录，窄类型与业务实现留在 D1。

完成条件：有源码位置支持的差异/基线清单、D1 的输入输出与主路径/失败示例，以及经用户确认的最小契约。D0 完成仅表示下一切片可启动，不表示新框架运行通过。

### D1：打通双 LLM 单 Turn、单获选 Intent 的最小链路

- [x] 复用供应商无关模型传输端口，明确外层规划和内层 Plan/Action 两种调用角色；两者可用同一供应商，不按模型名称判断职责。
- [x] 打通 `任务输入 -> 外层目标/完成标准/有序 Intent 提案 -> 可信校验与注册 -> 选中一个 Intent -> 内层 Plan/Action -> Policy/Sandbox -> Execution/证据 -> 注册规则验证 -> Controller 提交 -> 只读结果`。
- [x] 先接一个经确认、能力匹配且可复用的验证规则；注册后 Turn 目标及标准保持固定。Fact、Intent、Turn 和 TaskRun 结论分别输出，缺少整题验证能力时不得宣告 achieved。
- [x] 为当前 Intent 分配独立工作区；固定 TaskRun 授权、调用审计、预算与取消边界。模拟切片不得接触未授权真实目标；模型调用或执行结果未知时停止继续派发。

完成条件：单路径经模型角色候选、工具、证据与可信提交可重复验证；非法/越权/过期提案、规则不匹配、证据缺失、取消和未知结果不会产生虚假完成或重复副作用。本轮只验收角色分工与单路径，完整复盘、多候选和敏感证据分别留给后续轮。

D1 收尾语义：一个 Turn 按固定规则独立验证成功后，结果为 `Turn.completed`、`TaskRun.running`、`overall_goal=not_evaluated`；本轮派发停止，不自动创建下一 Turn，Sandbox 与证据保留。`running` 仅表示 TaskRun 尚未终态，展示/结果必须同时表明本轮已停止。用户显式结束时先由 Controller 提交取消终态，再调用现有 `cleanup_sandbox()`；Deadline 仍遵循既有 Controller 规则。自动化测试夹具在分别记录 Turn 验收与清理结果后负责取消并清理测试资源。

验收记录（2026-10-02）：新增 D1 测试 **74 passed**，与旧模型专项合计 **98 passed、1 warning，41.71 秒**；共享控制面、Controller、模型协调、旧 Supervisor、Gateway、观察证明及 T6 模拟应用/CLI 回归 **246 passed、1 warning，85.25 秒**。D1 新文件 Ruff E9/F/I、格式检查、7 个源码文件定向 mypy、离线 wheel/sdist 构建通过，完整命令/产物见 [D1_acceptance.md](D1_acceptance.md)。恢复核对原请求、纠正拒绝来源与固定提交；历史证明缺失不重签发；语义拒绝单独持久保存；结果区分历史已提交 Fact 与当前可复核 Fact。受限环境 WinError 5 / uv 包装器权限故障的执行不计通过，最终证据来自批准的本地模拟复验。未声明全库测试、真实模型、Docker、目标网络或 D7 公共入口验收通过。

### D2：把复盘接入可信控制闭环

- [x] 实施 Turn 首复盘（Intent 选定后、内层动作前）、Action 触发复盘及可信 Turn 完成后的只读尾复盘。
- [x] 仅由内层 `review_requested`、可信代码确定 `not_satisfied` 和确定性动作拒绝触发；同候选/上下文合并原因，未知结果走恢复，不触发复盘。
- [x] 对照固定目标、执行预期和已保存来源生成有来源建议；可信侧只接受 `continue_current_intent`，并独立控制后续动作及完成提交。
- [x] 实施 `allowed_action_kinds` 许可交集和仅用于工具动作的 `max_action_timeout_seconds` 更小上界；约束绑定 Intent/Turn，只能收紧并在范围结束时失效。
- [x] 每复盘 root 最多两次外层实际调用、每 TaskRun 共用最多八次；结构/语义无效、空响应和上下文刷新共用一次再生成。provider failure、认证失败、拒答和未知消耗额度后恢复不重发。
- [x] schema 12 原子保存 D2 运行身份、应用、generation、约束版本和暂停门；原始响应/来源留在证据台账；已知响应在 Controller 应用与证据台账之间中断时幂等恢复。

本轮沿用 D1 单附件、单候选、固定 SHA-256 检查：摘要只证明副本符合固定摘要，不代表整题成功。`action_candidate_id` 等于首次内层动作请求的可信内部台账 ID；结构纠正请求有独立内部请求 ID并沿用动作根。供应商 request ID 只作审计。Turn 首尾 action_candidate_id 为空，Action 必填。已派发 probe 按原边界完成；未派发动作和未提交的 `complete_intent` 按最新约束检查。空许可交集/动作额度耗尽时持久暂停；取消、Deadline 和结果未知沿既有路径处理。尾复盘失败不回退已完成 Turn；调用被取消/Deadline 阻止时保存 `not_called` 及原因。

验收记录（2026-10-02）：D2 review/链路/SQLite 聚焦测试 **67 passed、1 warning**；D1 链路、建模、持久化、工作区及 ControlStore 回归 **102 passed、1 warning**；Controller、控制状态仓库与 Turn 工作副本回归 **122 passed、1 warning**。Ruff `E9/F/I`、新增文件格式、定向 mypy、编译、离线 wheel/sdist 与 `git diff --check` 通过。覆盖过期响应刷新、普通应用异常恢复、尾复盘取消/Deadline、不重复自身约束复盘和 OS 运行锁。pytest 警告为禁用 cacheprovider 后不识别项目 `cache_dir` 配置。测试使用 Fake 模型与 Fake Sandbox；未验收真实模型、Docker、目标网络或公共 CLI/工作台。命令和具体覆盖见 [D2_acceptance.md](D2_acceptance.md)。

### D3：多路径尝试、隔离工作区与新 Plan

- [x] 在同一 Turn 中串行尝试实现同一目标的候选 Intent；策略性放弃与证据确认目标不匹配分别记录。环境故障和未知结果不改记为策略失败。
- [x] 切换前停止旧 Intent 所属进程并确认结果，确认后才进入兄弟 Intent 的独立工作区；停止未知时保持恢复边界，不启动下一候选。
- [x] 实施经用户确认的目录布局和显式产物共享，保留来源、对象版本和原验证状态；禁止仅因共享将观察升级为 verified。
- [x] 复盘要求重规划时结束旧 Plan 未执行部分并新建 Plan，保留旧 Execution/证据；一个候选经独立验证达成轮目标可完成 Turn。
- [x] 候选耗尽或需要调整目标时，由外层提出下一 Turn 并经可信注册；旧 Turn 目标不变。具体收尾状态按本轮已确认契约提交。

完成条件：以上内部模拟路径及拒绝/恢复场景均已验证；真实模型、Docker、目标网络、整题评估与公共 CLI/工作台不在本轮验收范围。详细结果和宽范围测试遗留见 [D3_acceptance.md](D3_acceptance.md)。

### D4：把预算与恢复覆盖到完整协调链

- [x] 扩展现有台账到已确认的八类操作开始预算：规划 4、动作 16、复盘 8、提炼 4、工具派发 6、独立 SHA 观察 4、产物共享 4、附件导入 2，总额 48。跨 Turn、切换和恢复累计，失败/超时/未知不退额；提炼只预留计量端口，Token/费用不计量。
- [x] 在模型调用、提案保存/应用、工具派发、证据保存、控制面提交和重载边界验证中断与恢复；同 ID 已完成结果只读重用，未确认结果不重问或重派。schema 14 原子镜像 D3/D4 记录，不回填旧运行。
- [x] 验证持久取消、Deadline、预算耗尽、过期提案与作用域结束时的停止规则；未知优先报告恢复要求，迟到响应不得重新派发或应用。
- [x] 核验上下文刷新、原拒绝/原错误回放、每个规划/动作逻辑请求最多一次再生成及资源清理；终态提交并确认重载后才清理，清理未知需显式核实存在性。

完成条件：恢复后授权、计量、作用域和已提交状态连续一致；没有重复副作用、重复消费、半提交或被旧提案改写的状态。环境未知或清理未知如实报告。

本轮证据：D4 **48 passed**，D1-D3 兼容回归 **187 passed**，共享控制面/Sandbox **222 passed**；定向类型、Ruff、格式、编译及差异检查通过，wheel/sdist 已构建并核对 10 个 D4 源码摘要及运行数据排除规则。宽范围静态检查的遗留与明确保留项见 [D4_acceptance.md](D4_acceptance.md)。Fake 仅隔离模型/工具，核心与持久化使用现有实现；真实模型、Docker、目标网络、整题评估及 CLI/工作台留后续验收。已确认契约见 [D4_proposal.md](D4_proposal.md)。

### D5：Process_Fact 版本与面向用户的证据读取

- [ ] 仅在有新增执行信息的成功复盘后，由外层提炼当前 TaskRun 的 Process_Fact 候选；可信侧校验来源与归属，和 verified ResultFact 分开保存。
- [ ] 实施总结追加版本、按来源对象版本判断适用性和冲突显式呈现；总结失败不单独阻断执行，也不用于宣告目标成功或执行脚本。
- [ ] 通过确认后的只读应用入口，让用户从 Fact、总结或 Execution 引用查看来源、操作、验证状态与可展示内容，证据损坏/缺失如实呈现；界面只是投影，不写控制状态。
- [ ] 核验普通模型上下文、日志、事件和总结的脱敏边界；敏感明文查看依赖 D6 保护契约，不能在本轮未确认时开放。

完成条件：用户可从引用追溯执行依据，不必只翻日志；版本/冲突可见，总结失败仍按可信状态执行，未验证文本不会冒充 verified Fact。

### D6：授权影响、敏感证据与跨 TaskRun 导入

- [ ] 确认精确资产/范围/端口与允许影响的表达，并在 Policy 和恢复入口核验 TaskRun 内授权不可变；发现新资产不得自行扩权。默认低影响、状态变更需明确授权、破坏性操作禁止。
- [ ] 先问清“发现凭据后是否允许在授权资产内认证测试”；只有确认允许时才设计执行组件按引用取用的受控适配，不把明文送入普通模型上下文或 Process_Fact。
- [ ] 冻结敏感证据保存、访问、默认脱敏、显式明文查看、保留/清理与导出规则，再完成证据写入到用户受保护查看的链路；不预设存储加密或认证实现。
- [ ] 补充授权变化后新建 TaskRun 的交接：旧产物显式导入、来源保留，旧授权与验证结论不继承，新运行重新判断。

完成条件：实际验收范围内的影响/越权拒绝、敏感保护、读取权限和导入再验证通过；未确认的凭据使用仍列为未交付，不用无凭据题的成功代替敏感能力验收。

### D7：CLI/工作台接入与新版真实单题闭环验收

先做应用接入，再做真实验收；不把两部分混记为完成。

- [ ] 接入已确认的新结果、事件、错误/退出码和 resume/cancel 契约，复用现有 CLI/工作台应用入口；旧运行按已确认兼容方案读回。工作台只显示状态、预算、复盘、总结与证据，不形成第二套调度。
- [ ] 提供 `.test/` 下可重复的请求/附件准备、配置与授权前置条件、启动/状态/事件/证据/取消/恢复/清理步骤；先通过模拟端到端检查，再在环境可用时验收真实模型和 Docker。
- [ ] 与用户确认实际题目/目标、授权影响和可独立判断整题正确性的验证规则，验证单题多路径尝试、复盘、新 Plan、证据追溯、最终状态及精确资源清理。
- [ ] 缺少答案验证能力的运行保持未完成/未评估，候选 flag 为 pending；分别记录观察 Fact、Intent、Turn、TaskRun 和清理结果。

完成条件：接入检查和真实题目证据分别通过；成功案例有独立目标验证，关键取消/恢复/未知分支及资源清理可复测。仅模拟、仅候选 flag、仅单 Intent 满足或仅界面接通，都不能宣布新版真实解题闭环完成。真实验收受阻时保持待验收。

### 延期任务与遗留验收

T7 的 Stage/Procedure/RAG 和独立取题/平台提交转换器继续延期，不作为 D1～D7 前置条件。模型供应商更换、更多 MCP、通用订阅与公共 Python API 按实际切片需求另行确认，不为本次排期提前建设。

旧 T5 的权限受阻复验、旧 T6 的真实验收保留原标签；若 D7 覆盖了其中某项场景，附新的证据并说明覆盖范围，不改写历史通过数或替尚未复验项宣告完成。

## 7. 待确认问题（按任务轮逐项冻结）

下表区分已冻结的 D1 最小契约和仍待对应任务轮确认的问题；已确认内容不重新猜测，超出该切片的字段与行为仍须确认。高层原则已经确认的项目仅将具体实现参数留到对应构建阶段。

| 冻结时机 | 要先问清的事项 |
| --- | --- |
| D0 / D1 前 | 已在 D0 逐项确认首个目标/规则、最小提案、角色配置/审计、旧运行兼容及单路径授权/预算/目录；见 D0 记录。通用规则、新增能力及外部协议不继承本切片确认 |
| D2 | 已确认并完成内部模拟验收；输入/输出、触发、约束、额度和恢复见 [D2_acceptance.md](D2_acceptance.md) |
| D3 前 | 已逐项冻结候选分类与剩余候选收尾、停止证明、目录布局、共享产物引用、新 Plan 和下一 Turn 注册；见 [D3_proposal.md](D3_proposal.md) 与 [D3_acceptance.md](D3_acceptance.md) |
| D4 前 | 已逐项冻结 48 次预算矩阵、操作开始计量、一次再生成与恢复/清理顺序；内部 Fake 验收见 [D4_acceptance.md](D4_acceptance.md) |
| D5 前 | Process_Fact 字段/来源/版本/冲突；普通证据读取入口、缺失处理、脱敏和展示方式 |
| D6 前 | 资产/影响规则、凭据是否可认证、敏感存储/显式查看/导出，以及跨运行导入再验证 |
| D7 前 | CLI/事件/结果兼容、退出码和恢复字段；实际题目/目标授权与独立答案验证 |

以下保留完整问题清单。每轮只确认本轮所需项目；这里的排期不会自动冻结其答案。

1. 发现的凭据是否允许在当前授权资产内进行认证测试；若允许，如何让执行组件按引用取用明文而不把它放入普通模型上下文、Process_Fact、日志或事件。
2. 授权清单如何表达精确资产、范围模式和端口；已确认发现的新资产不能扩权，只有现有规则明确覆盖时才可测试。
3. 受控验证规则的登记、版本、参数校验、能力匹配、证据输入和不可评估结果的具体类型。
4. D2 review 触发、调用额度及恢复已冻结；D4 再确认跨能力总预算和其他局部限额，不能把 D2 review 上限外推。
5. 证据视图对应的 CLI/UI/API 入口、访问控制、敏感值显式查看、保留/清理和导出协议。已确认用户不应只能翻日志。
6. D3 已确认同 TaskRun 的 Intent 隔离目录与显式共享引用；跨 TaskRun 共享/导入和授权不继承规则仍待 D6 冻结。
7. 公共 Python API、双 LLM 外部序列化/兼容版本和通用事件订阅契约。
8. 后续独立转换器的平台、调用方式、题目/附件映射、批次调度、重试、提交去重和回执语义；不属于 Agent 当前框架边界。
9. Stage、Procedure、RAG 的职责和接口仍延期，不从当前双 LLM 框架推导其字段或业务语义。

## 8. 验收矩阵

| 场景 | 必须观察到的结果 | 状态 |
| --- | --- | --- |
| 合法请求 | 创建唯一 TaskRun 和 Sandbox | T2 模拟、T5 真实单 Intent 均已验证 |
| 同 Turn 多 Intent | Intent 是实现同一 Turn 目标的有序候选路径，串行调度；一个被验证的成功路径可完成该 Turn | D3 内部 Fake 切片已验收；真实模型/Docker/公共入口留待后续 |
| 工具越权/范围越界 | Policy 在副作用前拒绝 | T3 已验证，拒绝时无 MCP/Sandbox 调用 |
| MCP 命令生成失败 | 有 PlanValidationRecord 和证据引用，不创建虚假 Execution | T3 已验证，独立审计保留拒绝/失败记录 |
| 环境故障 | 与目标失败分开记录，保留重新调用路径 | T3 已验证 File Tool 分类及一次显式重试；真实 Docker 故障待验收 |
| 目标不匹配 / 策略放弃 | 分别记录；有可信停止收据后可切换兄弟候选；候选耗尽按原因处理 | D3 已验收分类与证明门；固定 v1 不产生 Intent 级 `target_mismatch` |
| Fact 候选 | 未验证内容不能进入 ResultFact | T4 文件类型证据链及拒绝分支已验证 |
| 目标完成标准 | 外层选择已注册规则及参数；可信代码独立执行；缺少匹配验证器不宣告成功 | 架构已确认；通用注册验证器待实施 |
| Action 复盘 | 仅由内层请求、可信 `not_satisfied` 或确定性动作拒绝触发；派发中的 Action 完成并保存证据后复盘 | D2 内部 Fake 模型/Fake Sandbox 链与关键恢复已验收；真实模型、Docker 与公共入口留待 D7 |
| 目标授权与影响 | TaskRun 授权不可变；默认低影响；状态变更须明确授权；破坏性操作禁止 | 架构原则已确认；授权声明与 Policy 覆盖待逐项验收 |
| 用户查看证据 | Fact/Process_Fact 引用可查看证据；运行日志不是唯一入口；敏感值默认脱敏 | 架构已确认；访问界面和证据查看协议待实施 |
| Process_Fact | 与 verified ResultFact 分开、带来源引用、可追溯版本；总结失败不阻断运行 | 架构已确认；持久化字段/版本机制待实施 |
| Intent 工作区共享 | 同 TaskRun 按 Intent 隔离；显式共享保留来源和原验证状态。跨 TaskRun 显式导入，不继承旧授权或验证结论 | D3 同 TaskRun 目录/共享及恢复已验收；跨 TaskRun 导入留待 D6 |
| 原子提交/幂等/恢复 | 不产生半提交，不重放未知副作用 | 控制面已验证 |
| 取消/Deadline | 停止副作用，清理 Sandbox，只输出已验证事实 | T2 模拟顺序已验证；真实 Docker 待验收 |
| 终态重载失败后的恢复 | `reload_state() -> cleanup_sandbox() -> close()`；释放 Registry 后允许同 Registry 再绑定；重载及业务 mutation 重放不访问 Docker | T2 第二轮模拟验收通过；重载失败仍保持缓存失效 |
| 全局检查 | 相关测试、模块测试、compileall、静态检查和 `git diff --check` 通过 | T4 历史 434 passed、1 skipped；T5 历史 468 passed、1 skipped；本次检查范围和受阻项见独立复验记录，不能宣告全部通过 |
| T3 已确认工具切片 | `ToolGateway -> Policy -> MCP -> Sandbox` | 2026-09-11 用户依据已有实现及 323 passed 记录验收通过 |
| T4 单 Intent 切片 | Fake Model → File MCP → Fake Docker → Execution → verified Fact → Intent 完成 | 2026-09-24 用户确认独立复验通过，434 passed、1 skipped；仅完成当前 Intent |
| T5 第一阶段 CLI 与事件 | 使用模拟链路验证输入、输出、持久化事件、错误和取消 | 历史模拟验证保留；本次 6 项 CLI 输入检查通过，T5 专项 12 passed、4 failed、18 errors，权限阻塞未解除 |
| T5 第二阶段真实单 Intent | 真实模型、真实 Docker 和实际附件贯通工具执行、证据、Fact 验证与 Intent 结果 | 两次历史真实效果复核通过；本次真实 Docker 与重新构建受阻，不能宣告本次全部通过 |
| T6 真实单题解题闭环 | 多 Intent 尝试、失败回溯、任务目标判断与最终 TaskResult | `run-task` 与工作台代码链路已接通；完整真实目标验收仍待用户按 README 复测，平台取题和 flag 提交仍归后续转换器 |
| T6 CLI 手工复测 | 用户按已确认的请求示例、命令和预期结果复测，并找到对应事件、证据及清理结果 | 已确认纳入 T6；具体步骤随 T6 契约补齐 |
| 独立前端 | 通过确认后的 Agent 入口提供可观察的任务操作 | 已确认安排在 T6 闭环稳定后；功能、界面、协议与验收标准待确认 |
| 完整 Agent 链路 | CLI、模型、Intent 调度、工具、Fact 验证、GoalEvaluator、TaskResult 连通 | 代码已接通主要 T6 路径；目标验证器缺失时不宣称目标成功，真实端到端验收未完成；不计入 T3 或 T4 完成 |

## 9. 历史验证记录

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


## T5 历史本机验收证据（2026-09-25）

本节保留实现阶段的历史记录。下节独立复验结论不能由这些历史通过结果替代。

- 全量：`.test/t5/pytest-final.log`，468 passed、1 skipped（Windows 符号链接条件跳过），150.67秒。新增 34 项测试覆盖 CLI 文件/stdin、严格授权、事件与只读重放、超时导出、清理失败、跨进程锁/取消、5秒进程退出、HTTP 分类/无重试、一次结构纠正、未知模型/工具不重派、未知导入显式读回、恢复新授权、Deadline、旧程序证据兼容。模拟验证与真实证据分开。
- 真实成功 TaskRun：`f0ea4728-9472-4dd0-8a75-4bd6267ac220`，`.test/t5/real-runs/`；请求模型 `deepseek-v4-flash`，真实 `ASCII text`、1个 verified Fact、Intent succeeded、TaskRun inconclusive、single_intent_slice_finished、整体目标 not_evaluated、清理 succeeded、退出0；14条控制事件在 `.test/t5/real-events-2.jsonl`。最小 API 预检返回的模型别名 `deepseek-flash` 与请求名不同，不据此改写配置。
- 真实失败/恢复 TaskRun：`b28a55f5-75ba-452c-9ebe-ea4ae6e79488`，初次 file 参数不兼容，工具结果保留 unknown，未自动重试、未生成 Fact；Deadline/清理5和 cleanup-only 已实际经过。保留历史错误，不改为成功。
- 最终连接/DNS超时补强后真实复验 TaskRun：`cfc1762c-38e5-4b92-b621-76e03286617a`，`.test/t5/real-events-final.jsonl`；Intent satisfied、1个verified Fact、14条事件、TaskRun inconclusive、清理成功、退出0，所有外部/模型未知标记false。
- 隔离：`.test/t5/real-isolation-report.json`，14项检查全通过，涵盖配置、uid=10001、只读根、仅loopback、出网拒绝、无挂载/socket、导入读回、禁止覆盖/原内容保持及清理确认。
- 真实取消：`.test/t5/real-cancel/acceptance.json`，TaskRun `60523bfa-bca1-43f0-bae2-a1d418b99dcb`，中断后2.911秒退出，cancelled/cleanup succeeded/130；仅模型响应未知并已丢弃，无工具执行或 Fact。
- 安装后入口已从独立 `.test/t5-installed-cwd/` 跑通模拟模型/容器 + 真实本机 MCP，TaskRun `91fcc79c-1a61-46e1-ba59-f4d8e4707109`，Intent satisfied、14条事件、清理成功、退出0；89个打包源码文件与工作区一致。
- 构建产物在 `.test/dist-t5/`；wheel 含两个 CLI 入口及所需模块，无 `.env`、运行证据、缓存或测试包；sdist 仅保留 `.test/qinyuan_agent.egg-info` 构建元数据，无运行数据。Windows 受限构建遇到权限拒绝后，在授权的沙箱外完成构建。
- 新增文件通过 Ruff；12个已跟踪修改文件的48条既存 lint 问题与 HEAD 对比无新增，不扩展为全库重构。限定10个本轮源文件的 mypy（follow-imports=silent）通过，不宣称全库类型检查无错误。

验收限于当前 Windows / Docker Desktop Linux 环境、指定本机镜像及无敏感小附件。HTTP 错误和慢速写入使用受控测试模拟；并未人为制造真实供应商429/5xx或真实慢磁盘。POSIX 进程组代码未在本轮主机实测。未知创建/停止与损坏证据不自动修复；保留ID/证据，按显式恢复或人工调查处理。所有验收数据保留在 `.test/`，正式 state-dir 不自动删除。


最终核对：Docker 按 TaskRun component 标签查询为空，无本轮容器残留；根目录未留下构建/测试临时目录，测试证据保持在 `.test/`。`.test/t5/final-integrity.json` 记录127个源码/文档/验收文件的密钥扫描无匹配，89个wheel源码一致及SVG XML有效。工作区改动尚未提交Git。流程图已同步，但 `Qinyuan_Agent_confirmed_flow.svg` 仍按原 `.gitignore` 忽略；本轮仅为三个新增测试文件增加精确取消忽略规则。

## T5 独立复验记录（2026-09-25，用户提供）

结论：**T5 的历史真实效果复核通过；本次完整独立复验受环境权限阻塞，不能宣告全部通过。**

- 两次历史真实成功记录均确认：`ASCII text`、1 个 verified Fact、Intent 成功、14 条事件、清理成功；整体目标保持 `not_evaluated`，不代表整题解题成功。
- 本次新增验证：**6 项 CLI 输入检查通过**；静态、格式、类型、编译及差异检查通过。这些结果不能替代受阻的运行验证。
- 本次 T5 专项：**12 passed、4 failed、18 errors**。用户提供的复验结论将失败和错误均指向 Windows 管道或临时目录权限拒绝；不将失败/错误计为通过，也不据此宣告业务逻辑无缺陷。
- 本次真实 Docker 和重新构建也受环境权限阻塞，未完成独立复验。
- 原有 **468 passed、1 skipped** 为历史全套记录，不冒充本次结果；6 项输入检查与专项结果分别记录，不未经核对合并计数。
- 独立复验未修改原项目源码，**166 个输入文件保持一致**，产物均在 `.test/`。一致性结论对应这次独立复验时的输入，不包含此后文档修订。

记录来源为用户本次提供的复验结果；本次仅同步文档与流程图，没有重新运行上述测试、真实 Docker 或构建。后续在允许相关管道、临时目录和 Docker 操作的环境中补齐受阻验证，再更新独立复验状态。
