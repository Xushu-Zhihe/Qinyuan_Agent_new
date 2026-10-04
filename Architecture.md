# Qinyuan_Agent 架构说明

## 依据与状态

本文以 `Qinyuan_Agent_dual_llm_flow.svg` 作为当前目标架构，区分已经确认的设计、当前代码实现和仍待冻结的接口。`Qinyuan_Agent_confirmed_flow.svg` 是较早且不完整的架构图，不再代表双 LLM 的目标分工。

2026-09-24 用户确认本项目定位为独立的自动化渗透测试 Agent。T5 分为 CLI/事件模拟检查点和真实单 Intent 文件识别两个阶段，真实模型、真实 Docker 与实际附件链路是最终验收目标；完整 TaskRun 调度及真实单题解题闭环留在 T6。后续独立转换器负责多题目获取、调用 Agent 和平台 flag 提交。该决定已同步至流程图，替代原图中可选 qinyuan-bat 转接入口的旧约定。

2026-10-01 已确认双 LLM 框架的高层职责、Turn/Intent 关系、复盘时机、可信验证、授权影响边界和证据可见原则；高层设计已足以按切片开展实现，但字段、错误、存储和对外协议仍须逐项冻结。该架构描述目标，不代表相应完整链路已经实现或验收。T3 的 `ToolGateway -> Policy -> MCP -> Sandbox` 已依据当时的实现和 `323 passed` 记录验收；Stage、Procedure、RAG 仍延期，不能从图形位置推导其业务规则。

## 双 LLM 目标架构（2026-10-01）

`TaskRun` 接收可信入口提供的任务目标和不可变授权。外层 LLM 在创建 Turn 时提出该 Turn 的目标、完成标准候选以及有序 Intent 候选；这些 Intent 是实现同一 Turn 目标的备选路径，不是必须依次完成的子任务。可信代码校验目标的完成标准是否引用已注册且能力匹配的验证规则，并校验候选是否符合当前 TaskRun 的授权、预算和状态。Turn 注册后，其目标保持不变；需要调整目标时创建下一 Turn 表达。

内层 LLM 只针对可信调度器选中的 Intent 生成 Plan / Action，不创建或重排 Turn 的 Intent 列表，也不能扩大目标、授权、总预算或 Deadline。Intent 之间按可信调度顺序串行执行，不并行争用同一 Sandbox。任一候选路径经证据和独立规则验证达到 Turn 目标，即可完成该 Turn；Fact 已验证、Intent 成功、Turn 完成和 TaskRun 总目标达成是不同层次的结论。

复盘是外层 LLM 基于已注册的 Turn 目标、执行预期、已提交结果和可追溯证据，对“预期与实际发生了什么、证据支持什么、下一步如何处理”作出的有来源判断与策略建议；它不是对话式自我评价、可信验证或状态提交。Turn 开始和结束时复盘；Turn 内仅在可信代码按信号触发或内层 LLM 请求时复盘，且必须等当前 Action 结束并保存结果与证据。外层可建议继续当前 Intent、切换兄弟候选或重规划；可信代码校验其作用域与状态后应用。切换前须停止当前 Intent 的进程；无法确认停止时进入恢复边界。策略性放弃与证据确认目标不匹配分别记录。重规划结束旧 Plan 尚未执行的部分并创建新 Plan，不改写已发生的执行记录。当前 Turn 目标不因复盘建议而改变，目标调整通过新 Turn 表达。

复盘或提案处理失败时，可信侧先刷新已提交状态与证据上下文，再按受限规则重试；确定性拒绝可受限重新生成，并保留原提案及拒绝记录，仍无效则暂停。过期提案丢弃，不得应用。模型调用结果未知时不自动重发，转入恢复边界。复盘输出的受限约束须显式绑定当前 Intent 或当前 Turn；绑定范围结束时失效，不能改变授权、TaskRun 总预算、Deadline 或已注册 Turn 目标。D2 已冻结首个可运行复盘切片；D3 已冻结内部多候选、重规划及下一 Turn 的窄契约，完整预算与真实环境仍按后续轮次验收。

外层 LLM 可选择并参数化已注册的目标验证规则，但不能自行充当可信验证器。验证由受控代码或注册规则适配器依据证据执行；没有能力匹配的规则时，目标保持未完成/未评估，不能根据模型自述置为成功。可信侧拥有状态提交、授权、预算、Deadline 和副作用准入的最终控制权。

`Process_Fact` 是外层 LLM 对当前 TaskRun 执行过程的带来源总结，与经过验证的 `ResultFact` 分开保存。只有成功复盘发现新增执行信息时才提炼；提炼失败不单独阻断执行。历史版本保留，适用性依据对象版本与验证规则判断，冲突显式呈现。总结引用证据但不替代证据，也不能据此宣告目标成功或执行脚本。

证据是可追溯、可查看的正式产物，不只存在于日志。Fact 和 `Process_Fact` 均应能引用证据，用户能够从面向用户的证据视图查看来源、关联操作、验证状态及可展示的证据内容。普通日志、事件和 `Process_Fact` 对凭据等敏感值脱敏；明文只在受保护的敏感证据视图中按需查看，具体存储、访问控制和导出协议待后续冻结。

工作区按 Intent 隔离；同一 TaskRun 内跨 Intent 或 Turn 共享产物必须显式引用来源，并保留原验证状态，不因复制而升级为 verified Fact。跨 TaskRun 复用须显式导入并保留来源；旧 TaskRun 的授权和验证结论不继承，须在新 TaskRun 的授权及验证规则下重新判断。TaskRun 总预算统一累计，操作另有局部限额，调用账目跨 Turn、Intent 切换和恢复持续累计。Agent 正常执行时不强制用户介入；授权范围内无安全可行路径时，保存证据、报告未完成并停止。

## 当前实现与分阶段验收边界

2026-10-02 D0 已完成基线核对及 D1 最小契约确认，见 [D0_baseline_and_D1_contract.md](D0_baseline_and_D1_contract.md)。D1 内部实现现由 `D1Application` 装配 `D1SingleTurnRunner`、两个独立配置的模型角色、导入/副本准备、固定摘要观察、受限命令 Gateway、证据复核器及 Controller；验收状态以本轮 TODO 记录为准，源码接线不等同测试通过。链路只证明一个本地附件在独立 Intent 目录内的副本于首次可信摘要观察时与绑定导入收据一致：外层提出规则/单候选，可信侧固定摘要，内层仅提出固定检查。Fact、Intent、Turn 只读复核同一份保存证据，不重复读文件，不证明后来版本；TaskRun 整题仍为 `not_evaluated`。成功后 Turn 为 `completed`，TaskRun 保持 `running`，派发停止，Sandbox 与证据保留，不自动创建下一 Turn。旧运行保留原链路；D1 为本地 Fake 模型/Fake Sandbox 验收，真实模型、真实 Docker、目标网络与 D7 公共 CLI/工作台接入仍未验收。

D2 已在 D1 内部链上接入可恢复复盘；`D2Application` 只装配显式标记的新 D2 TaskRun，不改变 D1 或公共入口。Turn 首复盘在 Intent 选定后、首个内层动作前；Action 复盘由内层请求、可信 `not_satisfied` 或可信动作拒绝触发；尾复盘在可信验证并提交 `Turn.completed` 后只读执行。每个复盘归属最多两次外层调用，TaskRun 内最多八次；调用开始先持久记账，失败/未知不重发，已知响应应用中断时复用原响应和 Controller mutation。SQLite schema 12 原子保存 D2 身份、复盘应用、generation、约束版本及暂停门；原始响应和已保存来源版本摘要留在证据台账。`allowed_action_kinds` 取许可交集，`max_action_timeout_seconds` 仅收紧工具动作上限；确定性拒绝仍然拒绝，不制造 Execution。内部 Fake 模型/Fake Sandbox 与恢复验收见 [D2_acceptance.md](D2_acceptance.md)；真实模型、Docker、目标网络和公共 CLI/工作台仍未验收。

D3 只装配显式新建的内部 `D3Application` TaskRun：启动时冻结并导入一至两个授权本地附件，每 Turn 最多两个同目标候选、全运行最多两个 Turn。可信调度器串行选择 Intent；内层只能提固定的两步大小/SHA-256 或单步 SHA-256 Plan，可信侧准备不可覆盖的版本化副本、固定程序与证据。新观察按独立证明、verified Fact、内层 `complete_intent` 分层完成；跨 Plan 复用只保存对原证明和 Fact 的支持引用。成功候选与未尝试兄弟的 `skipped` 在一个 Controller 事务提交，Turn 完成后只读尾复盘、停止本切片派发，TaskRun 仍为 `running/not_evaluated`。

D3 的 `switch_sibling_intent` 须先保存有来源的外层建议及可信接受，再确认 Action 证据、无未决结果和按 Intent 停进程收据；停止未知不启动兄弟。策略放弃与独立的 Intent 级 `target_mismatch` 分开，固定 `attachment_copy_sha256` v1 不能否定整个 Intent，已读回副本的大小或摘要后来变化均是完整性异常而非普通 `not_satisfied`。主动调整目标先校验下一 Turn 提案并确认停止，再原子提交旧 Turn `superseded`、剩余候选 `skipped` 和新 Turn 注册。显式共享按精确来源版本复制、读回并保存清单；接收副本不自动成为 Fact。副本、共享或停止的原调用结果丢失时，只读核查不补签原操作收据、不重派。D3 的 Fake 验收与保留边界见 [D3_acceptance.md](D3_acceptance.md)。

以下首期控制链与后续 T1-T6 小节描述的是既有应用链路和阶段验收记录；其中的 direct `ModelReasoner` 不代表 D1 双角色实现，也不应与 D1 内部链路混同。历史验收数字不构成 D1 或新目标架构的验收。

D1 的确认范围已完成内部模拟验收：D1/旧模型专项 98 项、共享控制面/旧链回归 246 项通过，证据见 [D1_acceptance.md](D1_acceptance.md)。模型共享供应商无关传输端口但固定独立角色配置；请求原 envelope、拒绝来源、应用记录和历史证明分别保存并在恢复时核对。证据复核在 Controller attach 前也能通过保存快照只读执行；结果复核失败保留历史状态并明确恢复要求。流程仍对应 SVG 的 D1 主路径，无新增职责或控制流，未改图。

## 首期控制链（当前代码路径）

```text
CLI
  -> TaskRun
  -> 一个 TaskRun 独占的 Sandbox
  -> Intent 执行轮（按模型给出的优先级，深度优先、顺序执行）
  -> direct ModelReasoner
  -> Scoped ToolGateway
  -> Policy
  -> 专用 MCP Manager 或直接 Bash
  -> 同一个 Sandbox 执行实际 command
  -> ExecutionRecorder
  -> FactVerifier
  -> AtomicCommitter
  -> GoalEvaluator
  -> TaskResult 或下一轮 Intent
```

CLI 和 Python API 的应用入口统一归属 `AgentApplication`，首期实际入口仍只确认 CLI。本仓库拥有 TaskRun 内的唯一调度权，内部调度、重试、状态机和业务判断由 Agent 负责；外部调用方通过 protocol.md 中 T5 v1 公共入口调用 Agent，不复制其内部控制逻辑。T5 不实现 qinyuan-bat 专用转接。

`RunSupervisor` 持有一次 TaskRun 的整体循环。`TaskRun` 是一道题目的一次完整解题实例，接收请求后创建全局唯一 Sandbox；该 Sandbox 被同一 TaskRun 的所有 Intent 轮复用，在完成、取消或 Deadline 后销毁。新的运行必须创建新的 TaskRun 和 Sandbox。

`TurnManager` 管理 `Turn -> Intent -> Plan -> Execution -> Fact` 的核心状态。一个 Turn 内的 Intent 依模型返回的可能性顺序逐个尝试，采用深度优先回溯，首期不并行操作同一 Sandbox。现有 `agent_core` 已实现这部分领域状态、失败分类、证据引用和持久化控制面。

`ModelReasoner` 生成已确认的 Intent、Plan 或单项 Intent 动作候选。模型不能直接执行命令、写入控制面或提交 Fact；模型调用经过现有供应商无关 `ModelAdapter`、截止时间、取消、结构校验和一次纠正流程。首期只实现 direct 路径。

工具调用沿单向边界流动：专用 MCP 工具使用 `ToolGateway -> Policy -> MCP Manager -> Sandbox`；通用脚本使用 `T6CommandGateway -> Policy -> Sandbox`，由可信侧构造唯一 `bash -c` argv，绕过 MCP。Policy 必须在产生副作用前完成权限和目标范围检查。两条路径都在 TaskRun 的同一 Sandbox 中执行，环境故障、Sandbox/Docker 故障和目标任务失败必须由执行记录区分，环境故障不能直接加入策略失败集合。

`ExecutionRecorder` 保存执行记录、stdout/stderr 和 Artifact 引用。原始输出不能直接成为 Fact。`FactVerifier` 依据后置条件、证据、目标范围和时效验证候选 Fact；只有验证后的事实才能由 `AtomicCommitter` 与事件、Checkpoint 一起原子提交。SQLite 控制面是已冻结状态的事实来源，内存只缓存已提交快照。

`GoalEvaluator` 根据已验证 Fact 判断是否继续下一轮或生成最终结果。Deadline 或取消时只能使用已验证的 ResultFact，输出 `partial` 或 `inconclusive` 的具体外部语义仍需确认。

## Agent 与后续独立转换器的边界

| 组成 | 已确认职责 | 尚未确认的部分 |
| --- | --- | --- |
| Qinyuan_Agent_new | 独立自动化渗透；拥有 TaskRun 内的执行、证据、Fact 验证、目标判断和结果生成职责 | 完整 TaskRun 链路留在 T6；T5 v1 CLI/事件/退出/恢复契约已确认并实现 |
| 后续独立转换器 | 自动获取多道题目、调用 `qinyuan-agent-new`、向题目平台提交 flag | 平台与认证、题目/附件映射、调用方式、批次调度/并发/重试、提交去重、回执处理及部署方式 |

转换器是后续独立交付，不属于当前 Agent 内部模块或 T5 验收范围。Agent 不依赖转换器的批次状态或平台协议；转换器也不直接改写 Agent 的控制面或驱动其内部工具链。Agent 结果与平台提交回执属于不同边界，二者的字段和状态映射需在后续协议确认时明确。

## T5 两阶段接入与 T6 边界

第一阶段使用现有模拟模型、真实本机 File MCP、模拟 Docker 和真实持久化验证 CLI 的输入、输出、事件、错误和取消，仅作为开发检查点。第二阶段补齐真实模型适配器及真实 Docker 附件导入，使用实际附件验证工具执行、独立证据保存、Fact 验证和当前 Intent 结果；只有实际通过该链路才达到 T5 的最终验收目标。

两阶段均复用现有 Controller、IntentToolCallingLoop、ToolGateway、Policy 和证据边界。模型与 Docker 适配器位于外围，CLI 的装配归 AgentApplication，不因此引入第二套调度或扩大工具权限。真实模型入口已确认使用项目 `.env` 的 DeepSeek API，Codex CLI 本轮不接入；T5 v1 请求、超时/取消、Docker 验收附件和 CLI 字段已逐项确认，详见 protocol.md。

已确认的 CLI 调用形态为同步 `qinyuan-agent-new run-intent --request <JSON 文件|->`，stdout 为一个版本化最终 JSON，可选 `--events` 导出持久化控制事件 JSONL。普通单 Intent 切片明确停止且无待恢复未知副作用时，TaskRun 以 `inconclusive` 收尾，整体目标记为 `not_evaluated`，收尾原因是 `single_intent_slice_finished`；Intent 判断与 Sandbox 清理结果独立报告。取消/Deadline 保留原终态，未知副作用先显式恢复。字段及错误/退出/恢复协议已冻结为 T5 v1，详见 protocol.md。

文件类型识别成功只支持当前 Intent，不能证明“找到 flag”或完成整题。T6 才补齐多 Intent 尝试、失败回溯、任务目标判断和最终 TaskResult，并验收真实单题解题闭环；平台取题及提交 flag 仍归后续独立转换器。T1～T4 的历史模拟验收不会因本次范围确认升级为真实验收。

2026-09-25 用户确认 T5 的两次历史真实效果复核通过，整体目标仍为 `not_evaluated`；本次完整独立复验因 Windows 管道/临时目录权限受阻，真实 Docker 和重新构建也受阻，不能宣告本次全部通过。历史与本次证据分别记录在 TODO。

## T6 手工测试与后续独立前端

2026-09-25 用户确认 T6 继续以 CLI 验收真实单题闭环，并补齐可供自行复测的请求示例、命令、预期结果及事件/证据检查步骤。具体操作随 T6 契约逐项确认，不将单 Intent 成功视为整题完成。

前端单独安排在 T6 闭环稳定之后，不属于 T6 完成条件；T7 的 Stage、Procedure、RAG 继续延期。题目/附件提交、进度、执行过程与证据查看、取消/恢复是前端待讨论的功能范围，页面、部署、访问权限和调用协议均未冻结。前端通过确认后的应用边界操作 Agent，沿用既有调度、Policy、证据和生命周期所有权，不直接改写控制面；本次不新增服务端入口或传输协议。

## 模块边界与依赖方向

T4 于 2026-09-13 确认单 Intent 切片边界：Fact 的证据有效、Intent 目标满足和 TaskRun 目标满足是三个不同判断。例如“附件的工具识别结果为 ASCII text”不能证明“找到 flag”。T4 返回当前 Intent 已满足/未满足/无法确定的判断，不因 Intent 满足就调用 TaskRun 完成确认或宣告任务成功；完成确认、TaskRun 终态和最终 TaskResult 由上层按已有契约处理。上文 GoalEvaluator 到 TaskResult 的链路是整体设计目标，不是 T4 接管上层调度的授权。取消/Deadline 保留 Controller 的生命周期职责。

首个候选固定为 `FileTypeFactCandidate(path, observed_type, execution_id)`；只接受成功、停止已确认、双流未截断且 stderr 为空的单行工具输出，最多移除一个末尾换行，类型文本逐字匹配且不归纳源码语言。独立证明文件和专用 Controller 操作已实现，schema 4 为 Fact 增加不可变、可空的 `verification_ref`；旧 Fact 保持 NULL。只读 Intent 评估器返回三态及支持 Fact ID，Controller 重新评估后仅提交当前 Intent.succeeded。T4 验收使用 Fake Model、真实本机 File MCP、Fake Docker 和真实持久化；Fake Docker 的夹具输出只验证程序规则，不能证明真实文件类型。首期 Intent 目标仅支持 `file_type`；`agent_core/intent_loop.py` 已接通单 Intent 循环，失败与恢复契约见 `T4_proposal.md`，实际验证记录见 TODO。

核心领域依赖类型和端口，不依赖 CLI、SQLite、Docker、MCP SDK 或模型 SDK。外围适配器实现核心端口，系统组装集中在应用入口。

| 模块 | 一句话职责 | 允许拥有的状态 | 不跨边界做的事 |
| --- | --- | --- | --- |
| AgentApplication | 组装入口并提供应用操作 | 应用级句柄 | 不实现第二套调度器 |
| RunSupervisor | 驱动一次 TaskRun 循环 | 当前 TaskRun 控制权 | 不直接写 SQLite 或绕过 Policy |
| TurnManager | 推进 Turn/Intent/Plan/Execution/Fact | 领域工作副本 | 不创建底层证据、不直接提交数据库 |
| ModelReasoner | 生成规范候选 | 单次模型调用上下文 | 不执行工具和副作用 |
| ContextBuilder | 提供有界上下文 | 查询结果 | 不暴露完整原始输出 |
| ToolGateway | 接收受限工具请求 | 单次调用权限上下文 | 不接受未授权调用 |
| Policy | 判断调用是否允许 | 任务范围和授权策略 | 不执行命令 |
| MCP Manager | 发现工具并生成实际 command | MCP 连接和工具清单 | 不拥有 TaskRun 状态 |
| Sandbox Runtime | 创建、执行、中断、销毁沙盒 | Sandbox 生命周期 | 不决定目标任务是否达成 |
| ExecutionRecorder | 保存执行与 Artifact 证据 | 执行证据索引 | 不把原始输出升级为事实 |
| FactVerifier | 验证候选事实 | 验证判断 | 不改变未经验证的原始证据 |
| AtomicCommitter/ControlStore | 原子保存控制面变更 | SQLite 持久化状态 | 不解析业务语义或自动改终态 |
| GoalEvaluator | 判断目标是否达成 | 评估输入 | 不凭模型文本替代证据 |
| Event | 发布生命周期和控制面事件 | 事件流 | 事件不反向驱动隐藏状态变更 |

StagePlanner、ProcedureSelector、ProcedureRunner 和 RAG 只保留边界。它们的字段、匹配、失败路径、候选提交和检索语义尚未冻结，进入实现前必须单独确认。

## T2 TaskRun Sandbox 生命周期

`Sandbox/task_run_sandbox.py` 的 `TaskRunSandbox` 在外围实现 `TaskRunSandboxPort`，持久化 Controller 仍是生命周期决策所有者。适配器复用现有 Docker SDK 连接和有界命令输出读取；不调用旧 `SandboxService` 的 agent 映射或 DockerRuntime 的创建/接管路径。T2 连接入口跳过旧 internal 网络准备，创建固定使用 `network_mode=none`，无其他网络、端口发布、宿主挂载或 Docker socket，保留容器内部回环。根文件系统只读；`/workspace` 和 `/tmp` 为 tmpfs，数据只在同一运行容器期间保留，停止后不承诺保存。

创建前生成 TaskRun ID，并写入资源所属标签。只有确认精确容器 ID、TaskRun 归属、运行状态、配置 Hash 与实际配置一致，才允许绑定并提交 TaskRun。创建/启动请求不自动重发；超时后只查询本次标签，只有唯一且可确认的运行容器可被绑定，否则报告创建失败或结果未知。可确认未运行的本次残留资源尝试删除；错误保留原始原因、清理原因和可用容器 ID。失败资源不写入缺少 sandbox_id 的 TaskRun。

活动 TaskRun 的 `attach` 检查数据库中的精确 ID，不经 `create`，不自动启动或重建。终态 `attach` 只绑定禁止执行的资源引用，允许容器已不存在时读取持久化结果；显式清理时再核对真实资源归属。崩溃恢复仍不重放未知 Execution，提交并重载恢复终态后关闭执行准入，资源清理由独立入口处理。

终态提交并成功重载后，先关闭新执行准入，再中断/停止/删除容器。每个容器最多发出一次 stop；`stop(timeout=2)` 是最多 2 秒优雅退出时间，不是清理总耗时。控制请求仍使用现有超时。停止或删除无法确认时报告 `outcome_unknown`，保留持久化终态。mutation 重放及重复取消不访问 Docker，清理重试走 `cleanup_sandbox()`。进程重启后的终态清理不能推断旧 stop 已结束，仍运行则报告未知，仅确认已停止才删除。

本轮验收使用 Fake Docker：验证调用、绑定、控制状态和故障分支。固定 Python 写标记、读标记、等待脚本只存在于测试代码，不通过脚本白名单声称实现 Policy。标记带 TaskRun ID，并验证同 Sandbox ID 复用和另一 TaskRun 看不到标记。真实容器的文件复用、网络隔离和进程终止留待后续 Docker 验收。

## T3 受限文件工具链

`agent_core/tool_gateway.py` 的 `ToolGateway` 只驱动一次显式工具请求，不生成 Intent、不决定下一轮策略。它依赖核心 Protocol 和已提交快照；调度调用方提供 `ToolAuthorization`，模型输入不能赋予权限。`FileToolPolicy` 先验证当前 TaskRun、工具及精确路径授权，再逐项校验 MCP 返回的命令。

`MCPs/file_tool_adapter.py` 的 `FileMCPPlanner` 只启动本地 File MCP 并取得命令，单次调用结束后关闭该进程，不采用旧 MCP Client 的自动执行/中断路径。`Sandbox/tool_runtime.py` 将核心执行类型转换为 T2 TaskRunSandbox 调用，仍复用同一 TaskRun 的 Sandbox，资源生命周期仍由 Controller 拥有。

`agent_core/file_inspection.py` 定义唯一受控脚本：逐级使用目录描述符与 `O_NOFOLLOW` 打开路径，确认普通文件，再以已打开描述符交给 `/usr/bin/file`。这防止符号链接和路径替换改变本次读取对象；不承诺文件内容在其他写入者存在时保持不可变。测试只用 Fake Docker，不宣称验证了 Linux 内核的真实隔离行为。

`agent_adapters/file_tool_recorder.py` 是外层文件证据适配器，保存不可覆盖的请求收据、命令、执行记录及结果完成标记，并提供控制面所需的只读证据接口。控制面仍只由 Controller/SQLite 原子提交。命令先保存证据，再提交 running Execution，最后执行；写入失败或未知运行结果不触发隐式重放。未确认停止的历史 Execution 会阻止后续 Gateway 请求，包括重新创建 Gateway 后的调用。

## 当前已确认的持久化边界

自动重载失败后，当前 Registry 所有者可调用 `reload_state()`，经 Repository 重新读取并复核完整状态和外部证据，成功后恢复缓存。该入口不写库、不调用 Sandbox；终态清理和释放注册仍分别调用 `cleanup_sandbox()`、`close()`。失败保留缓存失效及现有所有权，业务重放不承担恢复职责。

SQLite 保存 `TaskRun / Turn / Intent / Plan / PlanStep / Execution / Fact / FailureSummary / PlanValidationRecord / RuntimeEvent / Checkpoint`；schema 2 增加一对一 `intent_goals` 表，schema 3 为 TaskRun 增加可空且不可变的 `source_root`，schema 4 为 Fact 增加不可变、可空的 `verification_ref`，schema 5 增加独立的 `model_invocations` 调用台账，schema 6 增加 `intent_plan_applications` 和 `intent_action_applications`，在副作用前固定 Plan、ToolRequest、证明、Fact 与 mutation ID；schema 7 增加目标网络绑定，schema 8 增加轮级 `turn_model_invocations` 台账，schema 9 允许同一 Intent 串行保存多个有序短 Plan，schema 10 增加 T6 模型动作元数据台账，固定上下文摘要与资源 ID，结果只存证据摘要；schema 11 增加 D1 的 `d1_runs`、`d1_turn_bindings`、`d1_turn_proofs` 与 `d1_operations`；schema 12 增加 D2 TaskRun 身份和追加式复盘应用记录；schema 13 增加 D3 运行配置、Turn/Intent/Plan 绑定、Plan 结果、分类结果、操作账目及复盘应用。旧运行不补造 D1/D2/D3 身份或台账；schema 10/11/12/13 可只读查看且不写库。控制状态、一个事件、Checkpoint 与对应复盘应用或 D3 绑定/结果在同一事务提交；原始响应及来源证据留在证据台账。数据库不保存实际 command、stdout/stderr、附件内容、Token、T6 脚本、stdin、候选原文或模型原始响应。

T6-5 外层入口为 `run-task` v2：`TaskApplication` 将受审计的有序 Intent 生成、`RunSupervisor`、`IncrementalIntentRunner`、通用脚本 Gateway、短 Plan 条件验证与候选证据组装在同一 TaskRun 内。模拟和真实模式共用控制状态及证据规则；T6-6 接入单一公网目标的 TaskRun 专属隔离转发，零附件目标题也可启动。`TaskResult` 从只读控制快照与证据重建，区分 verified 观察 Fact、pending 候选、暂停、恢复要求和清理结果；无独立答案校验器时整体目标固定为 `not_evaluated`。工作台已接入该 `run-task --mode real`，只负责启动、取消和只读投影，不拥有或改写控制面状态。真实目标网络待用户验收。

T6 动作模型请求分别提供 TaskRun 已提交的 verified Fact 规划上下文、当前 Intent 可引用的 Fact ID 和最新可验证短 Plan 的支持 Fact ID。任务级 Fact 不自动取得当前 Intent 的完成权限；最新可验证 Plan 没有支持 Fact 时，模型不得提出 `complete_intent`。可信侧仍须在提交前复核 Fact、证明和归属，不能凭模型引用完成 Intent。

T1 新提交先在外层投影并复核整组变更后的候选状态及证据；Store 在事务中检查修改前后的归属、不变父级和运行状态组合。SQLite 不调用证据服务。已提交 mutation 的重放只读取首次提交结果，不查询证据或重复 Sandbox 副作用。提交后重载失败明确报告已提交及缓存失效；更强证据保留保证不属于当前控制面契约，详见 `protocol.md`。

## T4 单 Intent 恢复边界（2026-09-24）

`IntentToolCallingLoop.run` 从已绑定目标的 Intent 开始，采用一个单步骤 Plan，成功 Execution 后完成 Plan，再验证/提交文件类型 Fact，最后请求 Controller 仅完成 Intent。schema 6 声明与模型台账均为独立事务，不推进 Checkpoint；副作用仍经过 Controller 或 ToolGateway。已完成工具调用按原动作 ID 复用，新的重复检查被拒；未知结果不重派。模型适配器在工作线程等待，取消或 Deadline 后台账保留未知，迟到结果不产生副作用。

越界、多步骤和成功检查后的新检查动作分别抛出独立拒绝码；它们停止本次 run，领域状态可能仍 running，上层必须处理，不能自动反复调用。stop、候选不支持、预算耗尽与模型错误也不自行提交 Intent 失败或 TaskRun 完成。Gateway 既有失败状态和 Controller 取消/Deadline 路径保留；明确环境故障最多一次显式重试。


### T5 实现边界（2026-09-25）

`agent.py -> agent_adapters.cli -> AgentApplication` 是本轮应用入口。父进程只负责参数校验、最终输出、事件导出和取消时限；唯一持锁 worker 组装并调用原 Controller/IntentToolCallingLoop/ToolGateway，不另建业务状态机。外部模型 HTTP 和 JSONL 文件写入使用可终止子进程；Windows Job Object 约束 worker 与其模型/MCP 后代。状态读取使用 SQLite 只读一致快照及独立证据验证。

`DeepSeekModelAdapter` 使用官方 HTTPS JSON 接口、不暴露 tools、不重试网络错误；`ImportableTaskRunSandbox` 用固定程序通过 stdin 导入并读回 SHA-256，保留已有沙箱准入和执行锁。所有资源操作仍在外围。`RecordedSandbox` 记录清理成功/未确认，不能替代 TaskRun 或 Intent 结论。模拟夹具、真实配置和验收记录分开保存。T5 不实现完整 TaskResult、多 Intent 或 flag 判断。

### T6-3 有序 Intent 生成边界（2026-09-26）

`OrderedIntentGenerator` 从已提交的有界 Fact/目标失败上下文建立 direct 模型请求。schema 8 的轮级台账在调用前固定模型请求、Checkpoint、上下文摘要、Turn 注册 mutation 与目标；一次结构纠正的调用分别审计。候选及错误在单次完成事务中保存，候选不设置数量上限。保存的成功结果经当前状态与上下文复核后，使用固定 mutation 将有序 Turn/Intent、事件及 Checkpoint 经 Controller 原子注册。结果已保存而注册未完成时只重用结果；调用已开始但结果未保存时停止派发，等待显式恢复。取消、Deadline 或控制状态变化后的旧结果不得注册。该入口已由 T6-5 `run-task` 和工作台真实模式组装；它不负责 TaskRun 的答案判断。

### T6-4 深度优先调度边界（2026-09-26）

`RunSupervisor` 读取已提交快照，按 Turn sequence 和 Intent priority 顺序选择一个 Intent，先经 Controller 原子提交 `running`，再调用 `IntentRunnerPort`。Runner 只返回带 TaskRun/Intent 归属的结构化结果；调度器不执行工具、不解析模型文本、不凭退出码判断目标。

完整证据确认目标不匹配时，当前 Intent/Plan 记为失败并继续同一 Turn 的兄弟候选；候选全部失败后由显式生成器注册下一独立 Turn，生成器读取已提交 Fact 和分类失败摘要，不新增 `parent_turn_id`。结果未知分为两类：仍有 running Execution 或未完成工具收据时返回 `recovery_required`，保持原始 running 状态并禁止继续派发；Execution 已结束但后置条件无法判断时才将 Intent/Turn 标为 `inconclusive`。模型错误保留有界原因并写入失败摘要，Intent/Turn/TaskRun 均保持原状态，停止调度且不尝试兄弟候选。该调度切片已由 T6-5 `run-task` 接入；工作台只显示其持久化状态，GoalEvaluator/独立答案校验器仍未配置。

结构合法但引用无效 Fact、无可验证 Plan 或最新可验证 Plan 缺少支持 Fact 的 `complete_intent` 属于确定性 `action_rejected`，与执行结果未知、证据损坏及目标不匹配分开。停止原因写入运行目录的 `dispatch-stop-*.json` 收据；原动作仍以未应用状态留在审计台账，TaskRun、Turn、Intent 保持 running，本次调度暂停。显式 `resume` 只复用原动作并返回相同拒绝，不增加模型调用或命令派发，也不自动改为 `abandon_intent`；该拒绝不写入目标失败摘要。

已提交的模型错误摘要是该 Intent 的停止标记；恢复时 `RunSupervisor` 只读返回相同原因，不再次调用 Runner/模型，也不选择兄弟候选。

已处于 `inconclusive` 的最新 Turn 也是停止标记；再次调度只读返回暂停结果，不生成新 Turn。轮级模型错误由持久调用台账复用，未应用的成功结果以原固定 ID 注册，不重复调用模型。
