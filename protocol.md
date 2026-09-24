# Qinyuan_Agent 跨边界协议

## 范围

本文件记录已确认的跨模块协议和未冻结部分。它不把当前 Python 类名自动当成稳定的外部 wire protocol；独立 Agent 的 CLI 与事件、后续转换器调用和其他外部 MCP 集成仍需按实际入口确认与验收。2026-09-24 用户取消 T5 的 qinyuan-bat 专用转接，后续转换器单独确认协议。

## 已冻结协议

### CLI / Python API

当前只确认 CLI 是首期入口，且 CLI 请求最终进入 `AgentApplication`。`TaskRequest` 的已确认字段为 `goal`、`target`、`deadline_seconds` 和可选 `model_backend`；Target 包含 endpoints 或 attachments，支持已验证的 HTTP/HTTPS/TCP endpoint 与 `local_path/url` attachment 引用。未冻结命令名、输入载体、输出格式、事件流和退出码。

Python 内部模型调用使用 `ModelAdapter.invoke(ModelRequest, timeout_seconds, CancellationToken)`。适配器返回归一化响应或 `ModelError`；调度层负责截止时间、取消、结构校验和最多一次同适配器纠正。

### 模型适配器

`ModelRequestType` 只有 `intent_generation` 和 `plan_generation`。Intent 请求返回有序 `IntentCandidate`；Plan 请求返回一个 `PlanCandidate`。模型不返回领域对象 ID。错误类型为 `authentication_failed`、`timed_out`、`cancelled`、`empty_response`、`refused`、`invalid_response` 和 `provider_failure`。

单次模型调用的上限是 `min(600, TaskRun 剩余秒数)`；剩余时间不大于 0 时不调用适配器。结构非法或结构化空数组最多由同一适配器纠正一次；超时、取消、认证失败、明确拒答和供应商故障不伪装成普通任务失败。

### MCP / Sandbox

流程协议为 `ToolGateway -> Policy -> MCP Manager -> Sandbox`。ToolGateway 只能收到当前任务的受限工具上下文；Policy 在任何副作用前检查授权目标范围。MCP Manager 生成实际 command，Sandbox 执行该 command，并返回带状态、退出码、stdout/stderr 和截断标记的执行结果。现有 MCP Client 的执行状态包括 `completed`、`failed` 和 `outcome_unknown`；TaskRun 级 Sandbox 在所有 Intent 轮中复用。

环境故障必须保留重新申请/调用 MCP 的路径；目标任务失败才进入已确认的 `target_mismatch` 策略失败摘要。MCP Server 未生成命令、Docker/宿主机中断和 `outcome_unknown` 不能被当作策略失败。具体重试、重启和错误 wire shape 仍待确认。

### 持久化

SQLite 是控制面唯一事实来源。一次变更通过 `ControlMutation` 原子提交状态、一个 `RuntimeEvent` 和一个 `Checkpoint`。调用方提供 UUID4 `mutation_id`；同 mutation 同指纹返回首次结果，不同指纹报幂等冲突，陈旧 checkpoint 报陈旧写入。计划校验证据先单独追加，再由第二次变更引用；执行证据和 Plan 校验证据使用不同只读端口。

数据库只保存已确认的控制面对象和有界短摘要，不保存 command、原始 stdout/stderr、附件内容、Token 或模型原始响应。崩溃恢复保留未知的 running Execution，不重放副作用，并将相关父级状态原子收敛为 `inconclusive`。

#### T1 提交边界与失败语义

- `TurnWorkingCopy` 从已提交快照计算 typed `TurnOperation`；Controller 统一生成 `ControlMutation`，通过 `ControlStateRepository.validate_candidate` 复核**本次全部变更后的候选状态**，包括新增/更新的 command、output、Execution 与 Fact 引用。外部证据只由外层只读端口复核，SQLite 不依赖证据服务。
- SQLite 在事务中先检查修改前的已有对象归属及不可变父级，再按依赖顺序应用变更、验证最终关联。使用已有 ID 更换父级、跨 TaskRun 引用父对象/Fact 来源/失败证据均拒绝；同一变更中新建父子对象按最终关联校验。
- 幂等重放与新提交分开：同 mutation、同指纹由 Store 返回首次事件和 Checkpoint；不调用外部证据、不重载缓存、不调用 Sandbox。不同指纹仍由 Store 拒绝。缓存失效后只允许这条已提交 mutation 的重放路径，新写入继续拒绝。
- 提交前复核发现证据缺失或查询异常时，不产生新的状态写入、mutation、event、checkpoint；Controller 缓存保持原状，Sandbox 不清理。修复证据后，同一 mutation 可正常首次提交。
- 数据库事务内失败会回滚；**提交后重载失败不是回滚**。`CommittedStateReloadError` 明确提供 `committed=True`，T1 入口同时携带 `commit` 首次提交结果；缓存不可用，新领域操作停止。证据恢复后可重新加载/绑定，已提交 mutation 重放仍不重新验证证据。
- T1 终态清理只在首次提交成功且重载成功后执行。清理失败继续报告 `SandboxCleanupError`；重放不重试清理，清理仍需按既有生命周期机制单独处置，不将重放作为副作用重试入口。
- 普通操作不得使 running Execution 附着于终态 Plan/Intent/Turn。仅 `recover_running_execution_as_inconclusive` 可产生恢复例外：原有唯一 running Execution 的 ID、Plan、步骤、command 和未知输出状态完全保持，TaskRun 与对应 Plan/Intent/Turn 一起变成 `inconclusive`，失败原因为 `unfinished_execution_detected_during_recovery`；不允许额外状态变更或普通操作冒用该组合。

以上不承诺消除外层复核之后的并发证据失效。只有路径、ID 或 Hash 的独立索引不足以保证证据可用；更强保证需要足够的不可变证据快照或保留契约，尚未在 T1 引入。

### 事件

当前持久化事件类型冻结为 `task_run_started`、`completion_confirmation_requested`、`task_run_finished` 和 `control_state_committed`。事件 details 按操作白名单校验；基础字段为 `operation`、`entity_ids`、`result`，T4 绑定、Fact 提交和 Intent 完成增加各自已确认的 import_id、候选或支持 Fact 引用。序号由存储层生成。事件订阅、实时推送、重放和背压语义尚未冻结。

### T2 生命周期与模拟验收协议

- 每次创建先确定 UUID4 TaskRun ID，容器带 `qinyuan.managed=true`、`qinyuan.component=task-run-sandbox`、`qinyuan.task_run_id` 和配置 Hash。配置固定为 `network_mode=none`，不连接其他网络、不发布端口、不挂载宿主目录或 Docker socket，保留内部回环网络。只读根目录和 tmpfs 工作目录不提供停止后的数据保留保证。
- `create` 和 `start` 均不自动重发。超时后按所属标签查询；只有唯一容器、精确 ID、正确归属、运行状态、镜像和实际配置均可确认时才能绑定成功。不明确则报告 `outcome_unknown`，不创建第二个容器；确定失败报告 `creation_failed`，均不产生缺少 sandbox_id 的持久化 TaskRun。清理错误保留原始错误、清理错误和可用 ID，不吞并为策略失败。
- 活动恢复只读检查数据库中的精确 Sandbox ID、TaskRun 归属、运行状态、Hash 和实际配置；缺失、停止、归属或配置不符、查询失败均阻止执行，不自动创建、启动、重建或重放。终态恢复可只读取数据库结果，无需仍存在的容器，且保持禁止执行。
- 正常取消/Deadline 保持 T1 原子边界：候选/证据复核 -> 终态事务提交 -> 成功重载 -> 关闭执行准入 -> 关闭输出流 -> stop -> 查询停止状态 -> remove -> 查询确认不存在。复核/事务失败无 Sandbox 副作用；提交后重载失败保留已提交结果并使缓存失效，不进行清理。没有新增后台 Deadline 调度器，测试使用显式取消和 Fake Clock。
- 提交后自动重载失败，读取或证据恢复后由仍持有 Registry 的当前 Controller 显式调用 `reload_state()`。完整状态和证据复核成功后才原子替换内存快照并解除缓存失效，之后可分别调用 `cleanup_sandbox()` 和 `close()`。重载本身不写库、不调用 Sandbox、不推进时间，不释放/抢占 Registry；仍不可读、证据失效或快照归属/资源 ID/Checkpoint 校验失败时，保留旧快照但继续禁止使用。业务 mutation 重放不调用此入口，不能隐式恢复缓存或重试清理。
- `stop(timeout=2)` 仅表示最多 2 秒优雅退出时间，随后 Docker 可以强制终止；控制请求超时仍独立存在，不保证整个清理过程 2 秒完成。一次 stop 结果不明后不再发出重叠 stop。显式重试先检查，仅已停止才删除；重启后的终态资源仍运行则报告未知。本轮没有紧急强停或自动清理器。
- 已提交 mutation 的重放只返回首次结果，不重载证据、不访问 Docker；重复取消使用新 mutation ID 时也读取首次终态结果，不登记新 ID、不新建事件/Checkpoint。清理失败后只使用独立 `cleanup_sandbox()`；已不存在算成功，归属不符拒绝，无法确认算未知。该规则替代历史生命周期测试中“重放自动重试销毁”的行为。
- Fake Docker 的固定脚本写入带 TaskRun ID 的标记、由下一 Intent 读取并断言，且校验两次 Sandbox ID 相同、另一 TaskRun 标记为空；这仅证明模拟调用和状态传递。等待脚本模拟取消/Deadline 分支，不证明真实进程已终止。Python 可以启动其他程序，因此 `python3` 允许列表不构成脚本权限边界；测试只发出三类固定脚本，T3 才接入 Policy/MCP。

## 后续独立转换器（职责已确认，协议未冻结）

转换器负责自动获取多道题目、调用 `qinyuan-agent-new` 和向题目平台提交 flag。这一职责划分不指定 CLI、Python API、HTTP 或其他传输方式，也不继承 qinyuan-bat 的 stdin/stdout 格式。

Agent 请求/结果协议与题目平台的获取/提交协议分属不同边界；Agent 的结果不等同于平台接受 flag 的回执。题目与附件映射、任务关联、flag 交付字段、鉴权、版本、错误、重试、提交去重及回执状态均待转换器设计阶段逐项确认。转换器协议不属于 T5 验收范围，不阻塞独立 Agent 的 CLI 契约确认。

## 版本、错误与取消

当前控制面 schema 版本为 6；`protocol.md` 本身不是外部版本协商协议。公共 CLI、Python API、事件和 MCP 命令协议的版本号、兼容窗口和迁移策略尚未确认；后续转换器与题目平台的版本协议另行确认。T5 不要求兼容 qinyuan-bat wire protocol。

错误必须保留发生阶段、结构化类别、证据引用和短原因。模型错误、Policy 拒绝、参数错误、环境故障、目标不匹配和未知结果分别处理；不能把所有错误压成普通任务失败。

取消要求停止正在进行的副作用并完成可观察清理；T2 的正常路径先提交并重载终态，再关闭准入和清理，提交失败不触发紧急强停。当前已确认的控制面终态是 `cancelled`，Deadline 到达时是 `deadline_exceeded`，只允许依据已验证 Fact 形成 `partial/inconclusive` 结果。T2 stop 和保守恢复清理语义见上节；紧急强制清理、外部事件顺序、Artifact 保留和跨进程取消指令仍待确认。

## 待确认协议问题

- 独立 Agent 的 CLI 输入、输出、同步/异步、实时事件、退出码及外部 `resume/cancel` 语义。
- Python API 的异步模型和长期兼容承诺。
- 后续独立转换器的 Agent 调用协议、题目/附件映射、flag 交付，以及题目平台获取/提交的错误、幂等与版本边界（不属于 T5）。
- ToolRequest、PolicyDecision、MCPCommand、SandboxExecutionRequest、ExecutionRecord、TaskResult 的字段和序列化格式。
- Stage/Procedure/RAG 的协议、失败分支、候选提交粒度和策略记忆。
- 目标范围、网络、写文件、工具权限、MCP Server 运行位置和真实目标授权。

## T3 已确认文件工具协议（2026-09-10）

本节冻结首个 File Tool 的范围；上文的通用/远程工具协议待确认项不再阻塞本切片。用户确认真实本机 MCP stdio 通信与 Fake Docker 验收；不启动真实 Docker、不访问真实目标。

- 工具名 `file.inspect_file`，只允许 TaskRun 授权列表内 `/workspace/input/` 下的绝对普通文件路径。拒绝相对路径、空路径段、`.`/`..`、控制字符、反斜线及越界路径。受控脚本在执行时拒绝任一级符号链接和非普通文件；测试文件由 Fake Docker 夹具预置。
- MCP 子进程用本次 Python 解释器运行 `MCPs.File_Mcp.server`，启动和单次调用各最多 10 秒，整个计划阶段最多 20 秒，均受 TaskRun 剩余预算限制。只传必要系统/临时目录环境变量，不读取目标文件，不自动重启。返回结构必须含 `sandbox_command`、`request`，command 字段严格为 tool/arguments/timeout_seconds；实际命令再次经 Policy 精确匹配。
- 唯一命令为 `python3 -I -c <固定脚本> <授权路径>`。脚本只打开目标文件并调用 `/usr/bin/file`，不调用 shell、不写目标文件、不联网。打开的目录和文件描述符用于防止检查后的路径替换；这不保证对同一 inode 的并发写入得到一致内容快照。
- 单次执行默认 30 秒，上限 60 秒；执行前下调至剩余整秒预算，少于一秒不发出命令。Docker 只有明确 `Running=False` 才确认执行停止；仅有退出码不足以确认。单次超时或输出中断不自动重放；无法确认停止时关闭准入，并从持久化执行证据阻止新 Gateway 在恢复后继续执行。
- 用户确认 `ExecutionRecord.effective_timeout_seconds` 保存传给 Sandbox 执行端口的实际秒数；它与原 `MCPCommand.timeout_seconds` 请求上限分别保留。例如请求 30 秒而剩余 12.9 秒，派发与审计均为 12 秒。未派发则为空，已派发但结果未知或执行链异常时仍保存本次上限；该字段不证明底层进程已启动或停止。历史记录缺少该字段时读取为空，不从原请求猜测实际值。
- 先保存独立 command 证据，再分别提交 PlanValidation、pending Execution、running Execution，之后才能派发。输出写入成功后，再由 Controller 提交成功/失败状态。请求 ID 同时作为本切片 Execution ID/output_ref；命令生成/Policy 失败不创建 Execution，仍保存拒绝审计和 PlanValidation。
- 确定环境故障由调用方使用新 request_id 显式重试，至多一次。命令生成重试追加第二次 PlanValidation；实际执行重试复用第一次 command_ref 和 PlanValidation。重试请求试图改动命令或缺少授权时，只记录并返回拒绝，不消费既有重试凭据。Gateway/MCP 不自行重试、重建或选择下一 Intent。
- 相同 request_id 的请求及授权指纹必须一致；有完整完成标记时返回首次结果，不调用 MCP/Docker、不提交控制面。只留开始收据或输出已落盘但提交/完成标记失败时，抛 `ToolRecoveryRequired`，不猜测并重派。修复存储或证据后沿 T1/T2 的显式 Controller 恢复入口处理，不提供自动业务续跑器。
- 取消/总 Deadline 由 Controller 先提交并重载终态，再关闭准入、中断和清理；前置验证/事务失败没有停止/删除副作用。独立执行审计随后记录已收到的输出或未知结果，不补写终态控制面，所以 cancelled/timed_out Execution 可以没有 output_ref。Controller 提交/重载/清理失败保留原异常，同时尽力保存独立的未知执行记录；审计再次失败以异常说明保留，不能报成成功。
- 证据目录由调用方提供，控制面 SQLite 不含实际命令和原始输出。文件以 SHA-256 校验内容完整性，独占请求收据和不可覆盖文件避免请求并发重派；Hash 不代表认证或抵抗拥有目录写权限的攻击者。输出先替换显式配置的敏感值，再各截断至 64 KiB；上游截断时也处理可能残留的敏感值尾部前缀。不承诺自动识别未配置的秘密。证据保留到调用方显式清理，没有自动过期；本地文件格式不作为公共 wire version。

| 证据/结果 | FailureType | 后续动作 |
| --- | --- | --- |
| Policy 拒绝、符号链接/非普通文件、实际命令不符 | policy_rejected | 返回拒绝，使用核心既有失败分支；不写策略失败 |
| 文件不存在或 MCP 参数非法 | parameter_error | 使用核心既有失败分支；不写策略失败 |
| MCP 不可用/生成超时、缺少 file、权限/资源环境异常 | environment_fault | 调用方可显式重试一次 |
| file 非零、未知退出、输出中断、单次超时或停止状态不明 | outcome_unknown | 不自动重放；停止不明时拒绝新执行 |
| 正常退出且确认停止 | 无失败 | 保存 Execution 成功与输出证据；不自动成为 Fact |

上述工具失败均不推导 `target_mismatch`。受控脚本出口 40/41/42/43 分别表示文件缺失、路径/对象拒绝、环境故障、file 自身失败；分类依据受控程序的结构化出口，不解析原始 stderr 猜测策略结果。

## T4 已确认范围与目标层级（2026-09-13）

T4 使用 Fake Model、真实本机 File MCP、Fake Docker 和真实持久化完成单 Intent 链路。文件类型 Fact 表示工具对被检查对象的识别结果，Fake Docker 的夹具输出不证明真实文件类型。专用候选固定为 path、observed_type、execution_id；工具必须成功、停止已确认、双流未截断且 stderr 为空，stdout 最多去掉一个末尾换行后须为非空白单行、最多 200 字且无控制字符或 Unicode 行分隔符，候选逐字匹配，不做类型归纳。

T4 返回当前 Intent 目标已满足/未满足/无法确定的判断，不把工具成功或 Intent 满足升级成 TaskRun 目标满足。完成确认、TaskRun 终态和最终 TaskResult 留给上层按已有契约处理。只有整体目标本身也是该文件识别目标时，上层才可能根据相应事实作出任务层判断；不能仅凭目标文字相似推导。取消/Deadline 仍遵守已有 Controller 提交与清理协议。

验证时从可信请求收据取得路径，并同时核对授权、保存的实际命令、Execution 及其关联；收据中的路径不能代替实际派发命令的一致性检查。历史 `target_ref=None` 的证据不得直接改写成已验证目标。T4 使用独立不可覆盖的文件类型证明及专用只读复核端口，旧 T3 lookup 行为不变。

Intent 评估器只读核对持久目标及当前 Intent 已提交的 verified Fact 证明。支持 Fact 优先，返回 `satisfied` 和按提交顺序列出的支持 Fact ID；没有支持 Fact 且当前 Intent 的 Execution 未决或结果未知时返回 `inconclusive`；其余返回 `not_satisfied`。证明缺失、损坏或数据库读取异常直接抛异常。Controller 的 `complete_satisfied_intent` 在提交前重新评估并要求支持 Fact ID 完全一致，然后仅原子写 Intent.succeeded、事件和 Checkpoint；同 mutation 精确重放不重复复核，Turn/TaskRun 不自动完成。

T4 模型调用预算按每个 Intent 独立统计：Plan 生成最多 2 次（含结构纠正），动作最多 8 次（含纠正），首期最多一次成功工具派发；TaskRun 仍受整体 Deadline。schema 5 的 `model_invocations` 在同一 SQLite 文件以独立事务先登记调用、再登记经校验的结构化 Plan/动作或错误类别，不推进控制面事件或 Checkpoint，不保存原始提示、原始响应或凭据。结果类别为 `plan`、`intent_action`、`invalid_structure`、`model_error`；未完成调用要求显式恢复，不自动重新调用。跨重启计数包含已开始调用，旧记录不回填。

目标层级与职责边界已经确认：目标绑定随 TaskRun 保留，Intent 结束不删除；可信调用方提供目标，循环提出 Intent 完成请求，Controller 最终提交，评估器只读。上层先持久化 `Intent.running`，Controller 在首次生成 Plan 前核对归属、运行状态、任务范围和当前授权，再将目标、`control_state_committed` 事件及 Checkpoint 原子提交；未绑定目标时 T4 拒绝启动。重试和恢复核对同一目标。目标采用独立一对一 `intent_goals` 控制面表，schema 2 引入该表，旧库按版本原子迁移，保留旧数据，旧 Intent 不自动补目标（2026-09-23 已确认）。写入签名、不可变校验和同 TaskRun 内的 mutation 幂等已实现。Plan 步骤、循环计数、失败/停止及恢复规则已逐项确认，见 `T4_proposal.md`。已提交操作不得重复产生副作用，未知执行不得重新派发。

T4 七项绑定规则已确认（2026-09-23）：`TaskRequest.target.attachments[].local_path` 必须通过可信附件导入记录映射到 Sandbox 精确路径，不能按同名推断，URL 不作为首期 `file_type` 目标；绑定业务指纹只含操作类型及四个目标字段，授权不入指纹，幂等作用域沿用现有 TaskRun 内作用域；同 mutation 同目标重放首次提交，不同目标报 `mutation_conflict`；新 mutation 同目标重验当前收据和授权、只返回已有绑定；Intent 结束后仅允许旧 mutation 精确重放；用 `IntentGoalBindingResult` 区分 `created`/`replayed`/`existing`，并冻结六个稳定错误码，首期仅 `file_type`。首次事件保存 import_id，目标表不保存授权快照或凭据；首次绑定和新 mutation 同目标调用核对当前授权覆盖精确路径及 `file.inspect_file`。恢复后新工具派发重验当前授权，不足则停止继续并保留目标；同 mutation 精确重放只读首次结果。现有每次工具请求的独立收据保存当次请求、授权和 Sandbox ID，仅用于请求一致性/审计，不代表未来授权仍有效。

T4 本地导入要求可信上层显式提供 TaskRun ID、已绑定 Sandbox ID、附件索引、启动时规范绝对来源根目录和 `/workspace/input/...` 精确目标路径；导入器拒绝越界、符号链接、非普通文件及目标覆盖。schema 3 在 TaskRun 启动事务中保存不可变的 `source_root`；兼容的 NULL 值仅供非 T4 调用及旧记录保留，不可导入本地附件。单文件上限 16 MiB，来源读取、Sandbox 写入与读回最多共用 60 秒，并受 TaskRun 剩余 Deadline 约束。导入为可信上层准备动作，不走 T3 ToolGateway；正常写入与读回须在同一 Sandbox 准入锁内完成。独立本机证据目录先保存不可覆盖的请求声明，再在读回字节数与 SHA-256 匹配后签发成功收据。声明落盘即占用附件索引和目标路径；成功的同 ID 重放仅读历史结果，不再读取源文件或访问 Sandbox；声明后取消、超时、不匹配或未知执行一律要求显式恢复，恢复仅读回原路径并核对原摘要，不自动再次写入。显式恢复缺失声明返回 `import_not_found`。

## T4 调用与副作用的恢复协议（2026-09-24）

SQLite 当前 schema 6。Plan/动作结果在控制面或工具副作用前先保存不可覆盖的 application 声明；声明引用已审计的 model_request_id，并固定后续业务 ID。声明事务不推进 Checkpoint；Plan、Execution、Fact、Intent 状态继续由 Controller 原子提交。迁移保留 schema 5 模型台账，不补造历史声明。

恢复必须复用原声明 ID。有效历史模型结果没有声明、模型调用没有完成、工具收据未完成时返回 recovery_required，不生成新 ID 猜测重试。已完成工具结果重新核对请求收据、授权快照、命令及控制面后可只读续用；孤立证明重试仍完整复核。提交后重载失败须先显式 reload_state，再按原 ID 续接。

模型 stop 和 candidate_rejected 停止循环，不提交 Intent 终态；model_error 保留模型类别，不据此失败整个 TaskRun。Policy/参数错误返回 tool_failed，保留 Gateway 已提交状态；明确环境故障根据 Gateway 动作最多显式重试一次，执行重试使用原命令；未知执行不重派。工具重试不增加模型调用计数，模型调用上限仍从独立台账恢复。

无效动作使用 IntentActionRejected 的独立代码，停止本次 run，领域状态可能仍 running。依据不可变目标、已持久 Plan/动作及 Execution 归属恢复相同拒绝；同一已执行动作的重放不属于新检查。消费动作前先处理取消/Deadline，Gateway 自身的授权检查不被循环提前 Policy 校验替代。
