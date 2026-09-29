# Qinyuan_Agent 跨边界协议

## 范围

本文件记录已确认的跨模块协议和未冻结部分。它不把当前 Python 类名自动当成稳定的外部 wire protocol；独立 Agent 的 CLI 与事件、后续转换器调用和其他外部 MCP 集成仍需按实际入口确认与验收。2026-09-24 用户取消 T5 的 qinyuan-bat 专用转接，后续转换器单独确认协议。

## 已冻结协议

### CLI / Python API

CLI 请求最终进入 `AgentApplication`，已确认同步 `qinyuan-agent-new run-intent --request <JSON 文件|->`，其中 `-` 从 stdin 读取。请求包含 TaskRequest、附件索引、Sandbox 精确路径和授权，字段及本机操作者授权信任边界见下方 T5 v1。`TaskRequest` 的已确认字段为 `goal`、`target`、`deadline_seconds` 和可选 `model_backend`；Target 包含 endpoints 或 attachments，支持已验证的 HTTP/HTTPS/TCP endpoint 与 `local_path/url` attachment 引用。stdout 为一个带 `protocol_version` 的最终 JSON，stderr 只输出有界诊断；具体字段和退出码已按 2026-09-25 的确认冻结为 T5 v1。

Python 内部模型调用使用 `ModelAdapter.invoke(ModelRequest, timeout_seconds, CancellationToken)`。适配器返回归一化响应或 `ModelError`；调度层负责截止时间、取消、结构校验和最多一次同适配器纠正。

### T5 两阶段范围（2026-09-24，用户确认）

第一阶段通过模拟模型、真实本机 File MCP、模拟 Docker 和真实持久化检查 CLI 的输入、输出、事件、错误和取消。第二阶段接入真实模型与真实 Docker，使用实际附件验证文件类型工具执行、证据保存、Fact 验证和当前 Intent 结果；第二阶段真实链路通过才达到 T5 的最终验收目标。两阶段的验证记录必须区分，不能以模拟输出证明真实文件类型、隔离或进程终止。

真实模型入口已确认是项目 `.env` 配置的 DeepSeek API（`QINYUAN_LLM_BACKEND=openai`、`QINYUAN_MODEL=deepseek-v4-flash`、`OPENAI_BASE_URL=https://api.deepseek.com`、凭据由 `OPENAI_API_KEY` 提供）；Codex CLI 本轮不接入。配置值在启动时校验，密钥不进入源码、控制面、事件或错误输出。采用 JSON object、thinking disabled、无模型工具、HTTP 总等待最多 180 秒/连接含DNS最多 10 秒并受剩余 Deadline 限制；网络错误、429、5xx 不重试。

普通单 Intent 切片明确停止且无待恢复未知副作用时，TaskRun 收尾为 `inconclusive`，原因为 `single_intent_slice_finished`；整体目标判断为 `not_evaluated`。Intent 结果、verified Fact 证据、TaskRun 状态和 Sandbox 清理结果须分开输出。T5 不按 Fact 数量生成 `partial`，也不宣告 `achieved`。取消/Deadline 保留既有终态，未知副作用先走显式恢复；清理失败单列，不改写任务结论。收尾与清理的原子边界沿用 Controller 已有规则，CLI 错误/恢复格式见 T5 v1。

可选 `--events` 将已提交的 SQLite 控制事件导出至 JSONL，允许按 TaskRun ID 和序号只读重放；实时导出失败不回滚已提交控制状态，须报告导出错误。T5 事件字段、2秒导出超时、退出码、resume/cancel 及 DeepSeek 传输已冻结，见下方 T5 v1；通用订阅服务仍待确认。真实模型接入不改变模型只能提出候选/动作、工具必须经过 Policy、Fact 必须先验证后原子提交的边界。

T5 不将 Intent 满足映射为整题成功或找到 flag。多 Intent 尝试、失败回溯、任务目标判断与最终 TaskResult 继续留在 T6 的真实单题闭环；平台取题和 flag 提交由后续转换器另行确认。

### 模型适配器

`ModelRequestType` 包含 `intent_generation`、`plan_generation` 和 T4 增加的 `intent_action`。Intent 请求返回有序 `IntentCandidate`；Plan 请求返回一个 `PlanCandidate`。模型不返回领域对象 ID。错误类型为 `authentication_failed`、`timed_out`、`cancelled`、`empty_response`、`refused`、`invalid_response` 和 `provider_failure`。

核心调用上限是 `min(600, TaskRun 剩余秒数)`，T5 DeepSeek 适配器进一步限制至配置值且不超过180秒；剩余时间不大于 0 时不调用适配器。结构非法或结构化空数组最多由同一适配器纠正一次；超时、取消、认证失败、明确拒答和供应商故障不伪装成普通任务失败。

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

当前持久化事件类型冻结为 `task_run_started`、`completion_confirmation_requested`、`task_run_finished` 和 `control_state_committed`。事件 details 按操作白名单校验；基础字段为 `operation`、`entity_ids`、`result`，T4 绑定、Fact 提交和 Intent 完成增加各自已确认的 import_id、候选或支持 Fact 引用。序号由存储层生成。T5 的 JSONL 导出/只读分页重放及 2 秒写入超时已冻结；通用订阅服务仍未定义。

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

当前控制面 schema 版本为 6；`protocol.md` 本身不是外部版本协商协议。T5 CLI 和导出事件使用 protocol_version=1；Python API、其他 MCP 及后续兼容窗口另行确认；后续转换器与题目平台的版本协议另行确认。T5 不要求兼容 qinyuan-bat wire protocol。

错误必须保留发生阶段、结构化类别、证据引用和短原因。模型错误、Policy 拒绝、参数错误、环境故障、目标不匹配和未知结果分别处理；不能把所有错误压成普通任务失败。

取消要求停止正在进行的副作用并完成可观察清理；T2 的正常路径先提交并重载终态，再关闭准入和清理，提交失败不触发紧急强停。当前已确认的控制面终态是 `cancelled`，Deadline 到达时是 `deadline_exceeded`，只允许依据已验证 Fact 形成 `partial/inconclusive` 结果。T2 stop 和保守恢复清理语义见上节；T5 保留运行目录和证据、不自动删除；跨进程取消与事件顺序见下节。紧急强制清理仍不提供。

## 待确认协议问题

- T6 完整 TaskRun 结果和多 Intent 调度；T5 v1 不覆盖这些能力。
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


## T6 直接脚本执行切片（2026-09-25，已确认边界）

通用脚本不经过 MCP。可信侧在 `T6CommandGateway` 中校验当前 TaskRun、Plan 步、`sandbox.command` 授权、工作目录和预算，然后由同一 TaskRun 的 Sandbox 直接执行唯一 argv `bash -c <script>`。专用工具仍可使用各自的 MCP Manager；MCP 不能为通用脚本增加权限或改写 argv。

请求限制为脚本文本 64 KiB、stdin 1 MiB、stdout/stderr 各 64 KiB，工作目录为 `/workspace/work` 或其真实子目录，单步有效时限为请求值、60 秒步骤预算和 TaskRun 剩余时限的最小值，且最多 300 秒。附件原件保持只读；只有首次摘要校验通过的 `/workspace/work` 副本可执行，后续被修改或新生成的文件在证据中区别记录。

Sandbox 的正常结束以可信侧观察到进程停止和完整输出流为准，退出码为零或非零都记录为 `completed`，退出码和有界输出交给后置条件验证器。不能用脚本自行返回的退出码模拟超时。可信侧确认的超时在清理所有模型进程后记录为 `step_timeout`，同一 Plan 步最多允许一次改命令重试；清理无法确认、派发后结果未知或证据提交中断时记录 `outcome_unknown`，停止新的派发并等待显式恢复。预算在派发前耗尽也消耗这一次超时重试，但不创建 Docker exec。

每次请求先保存不可覆盖收据和命令摘要，再保存字节证据与 `ExecutionRecord`。精确完成标记只读重放且不调用 Sandbox；残留收据、未知结果或完成标记缺失不得自动重派。

通用 Intent 的可验证短 Plan 在首次 Execution 前固定非空 `IntentPostconditions`，包含输出原文条件与工作区文件预期 SHA-256 条件；条件全部满足才可进入通用 Fact 提交。探索型短 Plan 不声明可验证条件，只保存有界执行证据，不产生 Fact。输出确实包含原文即该观察成立；若未命中且该流截断，则无法判断。文件摘要表示该 Plan 结束时的工作区文件内容，由可信固定程序在 Sandbox 内读取普通文件后记录；缺失或不安全文件为明确不满足，Docker 结果未知为无法判断。首次文件摘要观察不可覆盖；未完成观察不自动重新读取。

验证器以只读命令收据、完成标记、Execution 和首次文件摘要观察签发独立 `T6ObservationProof`；证明缺失/损坏、归属不符、执行未确认或观察未知时拒绝提交。schema 9 按序保存同一 Intent 的多个短 Plan，只有上一 Plan 已完成才可登记下一 Plan；后置条件和文件摘要证据按 Plan 隔离，旧 Intent 键的历史证据仍可读取。Controller 与仓库再次复核证明，可原子提交 verified 观察 Fact 并让 Intent 保持 running；T6-2 的同事务 Fact/Intent 完成入口保留兼容。相同 mutation 精确重放不重新读取 Sandbox。该 Fact 只证明声明过的原文出现或文件摘要成立，不证明漏洞推断、候选 flag 的正确性或整个 TaskRun 成功。缺少独立答案校验器时，暂不把 TaskRun 置为 `achieved`；候选 flag 保持 pending，调度后续轮次直到总时限或取消。v2 CLI 和工作台真实模式均已接入；工作台只读取状态与证据。

T6-5 模型动作字段：`probe` 含 `script`、可选 `stdin_base64`、`working_directory`、`timeout_seconds`、`conditions`，空条件为探索型；每个短 Plan 仅第 0 步，条件为输出原文或文件 SHA-256，复用既定 64 KiB 脚本、1 MiB stdin、32 条条件和单步时间上限。`complete_intent` 含非空 `fact_ids`，必须包含当前 Intent 最新可验证短 Plan 的 verified observation Fact；`abandon_intent` 含已完成 `execution_id` 与最多 256 字的 `short_reason`；`candidate_flag` 含非空且最多 4096 字节的 `value` 与已完成 `execution_id`。未知字段拒绝。pending 候选逐条保留原文、来源与时间，展示时按原文合并去重来源，不校验或提交平台。schema 10 的动作调用先登记唯一 ID、Intent、Checkpoint、上下文摘要和固定资源 ID；完整结果先存不可覆盖证据文件，再向控制库提交结果种类与 SHA-256。只有开始记录则停止等待显式恢复；有完整证据而无完成记录时只补记同一结果；已完成但未应用时只读复用，不重问模型或重派命令。每 Intent 动作调用不设次数上限，一次结构纠正单独记账。v2 Runner/CLI 和工作台真实模式已接线，真实 Docker/模型仍由用户手工验收。

T6-5 的 `model_error`/`invalid_structure` 动作证据仅可附加代码定义的 `validation_code`（如 `provider_json_invalid`、`action_field_missing`），不保存原始模型回复、提示词、字段值、密钥、脚本或附件内容。CLI 记录位于 `<state-dir>/<task_run_id>/actions/model-action-<model_request_id>.json`；使用默认数据目录的工作台时，对应路径为 `workbench_data/runs/<workbench_run_id>/agent-state/<task_run_id>/actions/model-action-<model_request_id>.json`。该字段仅新运行可产生，旧的三字段错误记录仍可读取；出现字段时必须是固定枚举值。

下一次 T6 模型动作的只读执行观察按已提交 Plan sequence 和 Execution 登记顺序选最近最多 8 条，完整结构化观察的 JSON 总量最多 16 KiB，从最新记录向前分配。stdout/stderr 以 UTF-8 展示，非法字节和不可见控制字节用 `\xNN` 转义；每条保留 Execution ID、控制状态、退出码、原始双流截断标记，另用 `context_clipped` 标出上下文裁剪。观察仍是未验证数据；命令完成收据缺失、归属不符或有未结束 Execution 时停止建立上下文，不能让模型据缺失证据继续派发。构建器已接入 T6 模型请求与 Runner。

pending 候选的下一次模型上下文另限当前 Intent 最近最多 8 条，结构化 JSON 总量最多 16 KiB。按最新优先保留完整原文；超过预算的记录只给 candidate_id、来源 Execution ID 和原文 SHA-256，`value=null` 明确表示原文未提供，并以 `history_clipped` 标识省略。每条都标记 `pending`，读取时重新核对来源 Execution 与完成收据。该限制只作用于模型上下文，不删除原始候选台账，不验证候选正确性。

T6 模型动作通道与 T5 分开：T6 HTTP/JSON 响应最多 2 MiB，输出 token 默认 8192，可由本机 `QINYUAN_T6_MAX_OUTPUT_TOKENS` 在 1～32768 内配置；T5 仍为 64 KiB 与 1024 token。T6 动作先经严格字段解析，再存独立证据及 schema 10 摘要；一次结构纠正独立记账。模型调用开始但无完整结果时要求显式恢复，保存结果后不重新调用同一请求。v2 运行声明持久化输出 token 配置，真实模式恢复时复核一致性。

### T6-5 v2 CLI（本地附件）

`run-task --request <JSON文件|-> --mode simulated|real --state-dir <目录> [--fixture <JSON文件>] [--events <JSONL文件>]` 与 T5 `run-intent` 分离。v2 请求仅含 `protocol_version=2`、`task_request` 和 `authorization`；授权精确为 `{"allowed_tools":["sandbox.command"],"allowed_target_urls":[]}`。本批要求 1～16 个本地附件，总导入字节数最多 64 MiB，拒绝 endpoint 和额外 URL；真实 Sandbox 在没有题目目标时禁网。样例请求：

```json
{"protocol_version":2,"task_request":{"goal":"检查附件并记录可验证观察","target":{"attachments":[{"kind":"local_path","path":"sample.bin"}]},"deadline_seconds":3600,"model_backend":"deepseek_openai"},"authorization":{"allowed_tools":["sandbox.command"],"allowed_target_urls":[]}}
```

附件路径相对于启动 CLI 时的工作目录。首次导入后，原件留在只读 `/workspace/input`，可信侧复制并校验 `/workspace/work`；随后模型可以修改或生成工作区程序。`simulated` 需要独立夹具 `turns/actions/commands`，每条 command 可含命令后完整 `work_files` 快照；只有夹具允许 `$latest_verified_fact` 和 `$latest_completed_execution` 占位符。夹具耗尽返回暂停，可保持既有序列前缀后追加并用 `resume --authorization <JSON文件> --fixture <扩展夹具>` 显式恢复。

`status` 只读控制状态与证据，`replay` 只读事件；`resume` 必须提供与初始请求完全相同的授权 JSON。真实导入有未决声明时仅 `resume --verify-import` 做 Sandbox 读回校验，不重新上传，也不自动继续模型。暂停保留 TaskRun、Sandbox 和证据；`cancel` 才结束并清理。TaskResult 的 `overall_goal.outcome` 在无独立答案校验器时固定为 `not_evaluated`，候选 flag 仅为 pending。退出码：暂停/夹具耗尽/模型错误为 4，取消且无历史未知结果、清理确认时为 130，Deadline 为 3，未知副作用、损坏证据或清理未确认是 5；不以 0 宣告整题成功。真实 Docker、真实模型和目标网络的端到端验收不由模拟测试证明。

T6-6 扩展 v2 目标输入：`target.endpoints` 可省略或恰有一条显式端口的 `http://`、`https://`、`tcp://` URL；填写时 `authorization.allowed_target_urls` 必须恰为该 URL 的单元素数组，未填写时必须为空数组。可用 0～16 个本地附件，但零附件时必须有目标。目标 IP 仅允许公网；域名解析在转发容器每次连接时重新检查公网结果。真实模式在启动 TaskRun 前创建专属外联和内部网络及固定转发容器，Sandbox 仅接内部网络；无目标仍为 `network_mode=none`。目标网络创建结果未知时返回恢复要求，不宣称无需清理。该代码链路尚未经过真实环境验收。

T6-3 的内部 Intent 生成协议：可信侧先选定注册 mutation 和轮目标，并用已提交 Checkpoint、Fact、目标失败摘要计算上下文摘要。每次 direct 模型调用先写入 schema 8 轮级台账；一次结构纠正分别写入开始与完成记录。完成记录在同一事务持久化有序候选或错误，注册 mutation 与确定性 Turn ID 已在开始记录固定。只有完成候选且 TaskRun、Checkpoint、上下文、取消和 Deadline 仍有效，才能用该 mutation 注册 Turn/Intent；注册与事件、Checkpoint 是控制面的独立原子事务。注册前崩溃可只读重用已完成候选；无完成记录则停止并等待显式恢复，不再次调用模型。候选不设数量上限，首期 `intent_type` 仅为元数据。该协议由 T6-5 CLI 和工作台真实模式使用，不把候选生成本身误报为整题成功。

## T6-4 深度优先调度（2026-09-26）

`RunSupervisor` 在每次派发前读取最新控制快照，按 Turn sequence 和 Intent priority 选择唯一目标，并先提交 `Intent.running`。执行器只通过 `IntentRunnerPort.run(intent_id)` 返回带归属的 `IntentRunResult`；调度器不解析模型自由文本，也不把 Bash 退出码直接当作目标结论。

执行结果分类固定为五类：`succeeded` 必须已经有 verified Intent 状态，随后完成 Turn；`target_mismatch` 必须带已完成 Plan 和 Execution 证据，写入 `TARGET_MISMATCH` 失败摘要后继续兄弟候选；同一 Turn 的候选全部失败时调用显式 Turn 生成器，生成独立 sequence，模型上下文只读取已提交 Fact 和分类失败摘要，不维护 `parent_turn_id`；`inconclusive` 只用于 Execution 已结束但后置条件无法判断，将当前 Intent/Turn 设为 inconclusive 并停止新派发；`recovery_required` 用于仍有 running Execution、未完成工具收据或已派发命令的 `outcome_unknown`，保留 Plan/Intent/Turn 的 running 恢复锚点并禁止继续派发，等待显式处理；`model_error` 将错误原因写入审计/失败摘要，Intent/Turn/TaskRun 均保持原状态，停止调度且不尝试兄弟候选。另有独立的动作分类 `action_rejected`，用于可信侧确定性拒绝结构合法但不可应用的 `complete_intent`；归属不符或缺少执行证据均进入恢复边界，不自动重派。

T6 命令已派发但输出流未确认完整时，即使观察到退出码 0，也记录 `outcome_unknown` 和对应失败摘要；Execution 记为 failed，Plan/Intent/Turn 保持 running 作为恢复锚点，调度停止且不自动重派。旧运行中已提交的 `outcome_unknown` 不因代码更新而补认成功或改写状态；取消并清理是显式终结路径，不恢复丢失的输出。显式 `cancel` 可将持久化的运行中资源以禁止执行模式挂载，先提交终态再逐项核对归属并清理；普通 `resume` 仍须通过活动资源配置校验。清理无法确认时保留 `not_confirmed`，不得宣称取消已完成清理。T4 的历史失败分类不随此 T6 特例改变。

有远程目标的 TaskRun 清理成功需同时具备执行 Sandbox 的 `cleanup.json` 与绑定目标转发器、网络的 `target-network-cleanup.json` 成功收据。旧运行若只有前者，整体清理仍显示 `not_confirmed`，需显式 `resume --cleanup-only` 对原绑定资源重试；无远程目标的既有清理收据保持有效。

同一 Intent 已有 `MODEL_ERROR` 失败摘要时，恢复调度只读返回该摘要的有界原因，不再次调用 Runner/模型，也不选择兄弟候选；要继续必须由后续明确的恢复协议定义。

最新 Turn 为 `inconclusive` 时，重入只读返回暂停，不生成新轮。轮级模型错误在调用台账中保留并只读重放；轮级调用未完成时要求显式恢复，不生成新请求 ID。

每次新的 Runner 派发都必须由本次调度新提交 `Intent.running` 开始。再次进入调度器发现已是 running 的 Intent 时，先核对持久化停止/审计信息：已保存且未应用的 `action_rejected` 动作通过显式 `resume` 只读复用并返回相同拒绝，不再次调用模型或派发命令；没有可确认的安全续接条件时返回 `recovery_required`。已提交的模型错误摘要仍只读回放。

本批只接通状态调度和确定性分类测试。通用 Bash、专用 MCP、真实模型、答案校验器、CLI/工作台装配及最终 TaskResult 仍不属于 T6-4 验收。

## T5 v1 CLI 与恢复协议（2026-09-25，逐项确认）

入口为 `qinyuan-agent-new`，保留 `qinyuan-agent` 别名。同步执行，无后台任务服务。

```text
run-intent --request <JSON文件|-> --mode simulated|real --state-dir <目录> [--events <新JSONL文件>]
           [--fixture <模拟JSON>] [--env-file <.env路径>]
status --task-run-id <UUID> --state-dir <目录>
replay --task-run-id <UUID> --state-dir <目录> [--after-sequence 0] [--limit 128]
resume --task-run-id <UUID> --state-dir <目录> [--authorization <JSON文件>]
       [--verify-import | --cleanup-only] [--events <新JSONL文件>]
cancel --task-run-id <UUID> --state-dir <目录>
```

版本化请求示例：

```json
{
  "protocol_version": 1,
  "task_request": {
    "goal": "find flag",
    "target": {"attachments": [{"kind": "local_path", "path": ".test/t5/fixture.txt"}]},
    "deadline_seconds": 120
  },
  "intent": {
    "description": "identify file type",
    "attachment_index": 0,
    "sandbox_path": "/workspace/input/t5-fixture.txt"
  },
  "authorization": {
    "allowed_tools": ["file.inspect_file"],
    "allowed_paths": ["/workspace/input/t5-fixture.txt"]
  }
}
```

只接受一个本地附件；路径相对于调用 cwd，导入时再次检查普通文件、符号链接、范围、16 MiB 上限和摘要。授权必须精确匹配唯一工具与目标路径。本机操作者的 JSON 是 T5 可信输入；未来转换器不继承此信任。`simulated` 必须单独指定夹具，`real` 禁止夹具。通用 TaskRequest 的其他 endpoint/url 能力不由该 CLI 暴露。

真实模式读取 `.env`（不修改环境或全局配置），只允许官方 `https://api.deepseek.com` 或其 `/v1` 路径，拒绝代理端点，不使用 HTTP_PROXY/HTTPS_PROXY、不跳转。请求 `/chat/completions`，JSON object 且 thinking disabled，不发送 tools；仅沿用 T4 一次结构纠正。旧 MAX_RETRIES 配置不启用网络重试。模型上下文/响应各有 64 KiB 适配器上限，输出 token 上限 1024。CLI 请求/授权 JSON 上限 1 MiB，重复键与非有限数值拒绝。这些为有界实现限制，不放宽核心契约。

每次 TaskRun 使用 `state-dir/UUID/`，包含 `control.sqlite`、不可覆盖的 `run.json`、启动声明、独立 tools/imports/proofs 证据、slice/recovery 结果、cleanup 记录和操作报告。运行声明不含密钥；OS 文件锁覆盖整个写操作。状态、终态 resume、重复 cancel 和 replay 不迁移数据库，不连接模型或 Docker。只读 SQLite 使用一致快照。记录与附件不会自动过期或自动删除。

stdout 每次操作输出一个 `protocol_version=1` JSON（`--help` 为帮助文字例外），包含 `task_run_id`、`mode`、`intent`、`overall_goal`、`task_run`、`cleanup`、`recovery`、`errors`、`exit_code`，使用 events 时附 `event_export`。Intent 内分列实际领域 status、evaluation、slice_result 和 Facts；model stop 可以使切片结束而 Intent 领域状态仍 running，不伪造 Intent 成功或失败。错误使用有界 code/category/phase，原始 SDK 异常、密钥不输出。子进程 stdout/stderr 不直接穿透主进程。

| 退出码 | 含义 |
| --- | --- |
| 0 | 切片明确停止且清理成功；不表示整题成功，也不保证 Intent 满足 |
| 2 | 输入/配置拒绝，未启动新的 TaskRun |
| 3 | 明确运行错误、Deadline 或事件导出错误 |
| 4 | 结果未知/恢复必需，保留 TaskRun ID 和未知标记 |
| 5 | 终态已提交但清理失败或尚未确认 |
| 130 | 用户取消，外部执行结果无未知且清理成功；未完成模型响应允许丢弃 |

优先级：终态清理未确认为 5；工具/导入结果未知为 4（即使取消并清理成功）；仅模型响应未完成且取消清理成功为 130；其余明确错误/导出失败为 3。普通确定切片收尾只产生 inconclusive + single_intent_slice_finished，整体目标 not_evaluated，不计算 partial/achieved。

事件导出使用专用可终止进程，从 SQLite Checkpoint 水位内读取已提交事件。JSONL 字段为 protocol_version、task_run_id、sequence、type、timestamp、status、details。新文件独占创建、不覆盖；每次写入及 flush/fsync 总等待 2 秒，超时立即终止导出，SQLite 不回滚。JSONL 可能保留未确认尾行，重放以 SQLite 为准。replay 返回单个 JSON 的 events 数组及 next_sequence，after_sequence 为排他游标，limit=1..512。

resume 只使用持久化 ID，不接受新的 TaskRequest。继续新工具派发必须提供当前授权文件（与上述 authorization 对象同形）。未知模型或工具结果直接返回 4、不派发；未知导入只在 `--verify-import` 时读回原路径并核对原摘要，不重新写入，也不依赖来源文件仍在。仅核验无需授权；核验后继续执行需提供 authorization。终态 resume 为只读；`--cleanup-only` 才重试清理且不要求授权。已确认不存在的容器可完成清理；重启后仍运行且旧 stop 状态无法确认时保持 5，不重复 stop。

外部 cancel 写入该运行目录的取消请求，由持锁工作进程执行 Controller 的终态提交与清理；无人运行时由 cancel 取得锁并接管取消。最多等待 5 秒，尚未确认则返回 4/5。Ctrl+C/Windows Ctrl+Break 协作取消，主进程在约 4.1 秒边界终止本次 worker 及其后代，给最终只读结果和输出留出时间。Windows Job Object 在允许 worker 开始外部工作前绑定，覆盖模型/MCP 子进程；迟到模型响应不派发工具或写 Fact。该保证依赖本机进程终止和状态盘可响应，无法确认时不宣称清理成功。

真实文件检查使用 `file -E`（镜像未支持旧 `--error` 长选项）。已有 T3/T4 旧固定脚本仅允许作为历史证据核对，新派发只接受修正后的精确脚本；不重写历史收据。
