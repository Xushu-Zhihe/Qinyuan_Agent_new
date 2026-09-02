# Qinyuan_Agent 框架可行性计划

> 状态：第 0 轮、 第 1 轮 A/B、 第 2 轮（统一模型契约与三个 Fake 适配器）均已验证。
>
> 需求依据仅为 `Qinyuan_agent架构图.svg`、`Qinyuan_Agent流程图.svg` 和当前仓库现状。
> 未经用户逐阶段确认，不扩展下一切片，不补全未定义的业务语义。

## 1. 执行规则

- [x] 本轮只评估可行性并记录计划，不搭建具体功能。
- [x] 图中明确表达的组件和流向可以进入计划；图中没有定义的字段、规则、状态和算法不得自行推断。
- [x] 第 1 轮 A 已按用户确认的字段、终态和生命周期约束完成最小实现。
- [x] 第 1 轮 B 已按用户确认的 Turn、Intent、Plan、Execution、Fact、失败分类和证据引用规则完成纯核心实现与 Fake 验证。
- [x] 第 2 轮已实现供应商无关模型契约、统一校验/一次纠正，以及 Fake Codex、Fake Claude Code、Fake DeepSeek；未启动真实 CLI、未访问网络、未接入 MCP、Sandbox 或真实调度器。
- [ ] 每个后续切片开始前，先向用户逐项提问；答案记录到本文件后，才把该切片从“等待确认”改为“可执行”。
- [ ] 每次只实现一条可运行、可观察、可回退的端到端切片。
- [ ] 每个阶段都以实际测试或运行证据验收，不以“代码已写完”代替验证。

状态约定：

- `等待确认`：存在会改变职责、接口、数据或安全边界的问题，禁止实现。
- `可执行`：问题已由用户明确回答，验收条件已写清。
- `进行中`：已获授权且正在实现。
- `已验证`：目标行为和约定检查已实际通过。

## 2. 已核实的仓库现状

- [x] Git 当前只跟踪 `README.md`；`.env.example`、`.gitignore`、`pyproject.toml`、`MCPs/`、`Sandbox/` 均为未跟踪内容，不能默认视为已验收基线。
- [x] `pyproject.toml` 声明入口 `qinyuan-agent = "agent:main"`，但仓库中尚无 `agent.py`。
- [x] 已新增纯核心包 `agent_core/`、首轮 Fake 测试 `tests/test_core_round1.py` 和 `uv.lock`；尚无真实模型、MCP、Docker、附件导入或 CLI 长连接实现。
- [x] 现有候选资产包括 Docker Sandbox、MCP Manager、MCP 注册表和多个 MCP Server；它们与图中的 `MCP Manager -> ExecutionEnvironment` 方向接近，但尚未验证为新框架的正式契约。
- [x] 当前只能判断“具备可复用的外围候选实现”，不能判断“框架已经可运行”。

## 3. 从两张图中确认的架构约束

以下内容只复述图中信息，不补充业务含义：

1. 对外入口包含 CLI 和 Python API，统一进入 `AgentApplication`；应用暴露 `start / resume / cancel / result / events`。
2. 核心控制链为 `RunSupervisor -> TurnManager -> StagePlanner -> ContextBuilder -> ProcedureSelector`。
3. 命中且前置条件成立时执行 `ProcedureRunner`；否则进入 `ModelReasoner` 的 LLM Tool-Calling Loop。
4. Procedure 和 LLM 的工具调用都经过 `Scoped ToolGateway -> Policy -> MCP Manager -> Docker Sandbox -> 授权目标`。
5. `ExecutionRecorder` 保存 Artifact 和 `ExecutionRecord`；原始输出不能直接成为 `ResultFact`。
6. Stage 结果需经过后置条件、证据、范围及时效验证，再由 `AtomicCommitter` 原子提交。
7. SQLite 状态至少包含 `TaskRun / Turn / Item/Event / ResultFact / ProcedureFact / ExecutionRecord / Checkpoint`。
8. `GoalEvaluator` 决定继续下一个 Turn 或生成包含最终结果、证据引用和 Artifact 的 `TaskResult`。
9. Deadline 到达时，只能使用已验证的 `ResultFact` 输出 `partial / inconclusive`。
10. Turn 结束需要压缩上下文，但保留 Fact、Procedure 和证据引用。

## 4. 当前阻塞项与图中冲突

- [x] **系统所有权已由用户确认**：本仓库单独实现 Agent；`qinyuan-bat` 只做最基础的调用。完成后最多增加一个转接层做适配，不在两个系统之间重复建设调度器。
- [x] **既有代码归属已由用户确认**：`MCPs/`、`Sandbox/` 和 `pyproject.toml` 可以修改复用；用户已亲自验证 `MCPs/`、`Sandbox/`，暂按可信的外围基础处理。它们仍需在新框架契约下做集成验证，不能因为“可信”跳过契约测试。
- [x] **调用入口已由用户确认**：首期只通过 CLI 调用 Agent。CLI 的输入形式、输出形式、退出码、同步/异步语义尚未定义。
- [x] **首条切片目标已由用户确认**：先验证 CLI 至 MCP/Sandbox/结果的链路能否跑通；暂不指定真实靶场题。若跑通验证确实需要题目或其他实际目标，必须先向用户申请。
- [x] **MCP 可用范围已由用户确认**：实际任务中允许使用全部现有 MCP；这不等于已经授权访问任何目标，目标和范围仍必须随具体任务确认。
- [x] **TaskRun 生命周期已由用户确认**：`TaskRun` 表示一道题目的一次完整解题实例；CLI 接收后立即创建一个全局唯一的 Sandbox，并由该 TaskRun 内所有 Intent 执行轮共同使用，直到完成、取消或 Deadline 后销毁。重新运行同一道题必须创建新的 `TaskRun` 和 Sandbox。
- [x] **Intent 尝试顺序已由用户确认**：同一轮由 LLM 生成的多个 Intent 按 LLM 返回的可能性顺序逐个尝试；调度模型采用深度优先，一条路径继续到不能再推进后再回到同层的下一条路径。首期不做并行 Intent Worker。
- [x] **首期 Intent 元数据已由用户确认**：Intent 可携带由生成它的 LLM 提供的可选 `intent_type`；首期调度器不依据该字段改变行为，未识别类型未来默认按 `direct` 处理。
- [x] **RAG 分支已明确延期**：首期只实现 `direct` 方案生成和执行，不实现 RAG 查询、`retrieve_first` LLM、RAG 匹配阈值、RAG 候选仲裁或双 LLM 并行方案生成。未来若启用 RAG，检索内容只作为参考资料，由 LLM 判断其适用步骤，框架不强行拆解或合并后续路径。
- [x] **图义已澄清一部分**：`ProcedureCandidate -> ProcedureValidate` 是将真实 Trace 编译为可复用 Procedure 前的步骤、参数、断言和 Trace 校验；它不是渗透执行中的某个工具步骤。
- [x] **失败分析原则已由用户确认**：不把失败、超时视为同一种结果。每次必须先用记录判断是本机/MCP/Sandbox 链路问题，还是目标任务导致的失败；前者应保留重新申请/调用 MCP 的路径，后者才讨论换工具或换策略。具体分类字段、重试次数和切换规则仍未定义。
- [x] **命令生成失败的 Plan 证据引用已冻结并验证**：使用不可变 `PlanValidationRecord`，字段为 `plan_validation_id / plan_id / task_run_id / step_index / attempt / status / failure_type / short_reason / observed_at`。每次 Plan 参数校验或请求 MCP 生成 command 都创建一条记录；command 尚未生成的失败以 `plan_validation_id` 作为 `FailureSummary.evidence_ref`，不创建 Execution。
- [x] **Plan 校验读取边界已冻结**：`PlanValidationEvidencePort.lookup(plan_validation_id) -> PlanValidationEvidence | None` 只负责 Plan 校验/command 生成阶段的记录；核心通过它验证引用存在、TaskRun 归属、失败分类和时间。`ExecutionEvidencePort` 只负责已生成 command 的 Execution 证据，两者不混用。
- [x] **Plan 校验重试作用域已冻结并验证**：`attempt` 按 `(plan_id, step_index)` 独立从 `1` 编号；具体步骤使用其索引，步骤解析前的 Plan 级校验使用 `step_index = null` 并按 `plan_id` 独立计数。只有首次 `environment_fault` 可在相同作用域进入第 2 次尝试。
- [x] **Plan 校验记录的写入归属已冻结并验证**：MCP/命令生成外层创建并保存记录，再将 `plan_validation_id` 和不可变结果交给 `TurnManager`。调度器不创建或修改底层证据，只校验、写入失败摘要和推进状态。执行阶段的环境重试不再次请求 MCP，复用第一次成功的 `PlanValidationRecord` 与实际 command。
- [ ] **运行失败的具体分支未确认**：渗透执行中某一步未通过，应走 `Status -> Mismatch`，但后续是回到同一 Stage 的 LLM 探索、重新规划下一 Stage，还是终止，仍需用户确定。
- [ ] **Procedure 校验失败的提交语义未确认**：`ProcedureValidate` 的“通过”和“不通过”两条边都指向 `Commit`。需要确认“不通过”是否只提交其他已通过候选并丢弃 Procedure Candidate，还是记录短失败/预算消耗后不提交该 Item。
- [ ] **失败策略记忆未确认**：用户明确要求后续 LLM 在较少上下文下不再采用相同策略；现有候选是“精确命令匹配”，但“相同策略”的判定、记录范围、压缩格式、有效期和真正执行拦截的位置尚未定义。
- [ ] **提交粒度未确认**：`ResultFact Candidate` 和 `ProcedureFact Candidate` 分别验证后如何汇合到一次 `completed Item` 原子提交，图中没有说明。
- [ ] **补充图的术语和流程未确认**：补充图使用 `Process_Fact`，原 SVG 使用 `ProcedureFact`；补充图还表达“每轮新开 LLM、Intent 先查 RAG、Skill.md 注入、LLM 可查看本次工作目录”。这些是设计参考，不自动视为已确认需求。

第 1 轮 A 之外的问题仍按所属切片保持 `等待确认`。

### 4.1 已确认的首期执行模型

首期只实现以下控制流：

```text
CLI -> TaskRun -> 单个 Sandbox
              -> Intent 执行轮
              -> 按可能性顺序逐个尝试 Intent（深度优先）
              -> direct LLM 生成方案
              -> MCP -> 同一 Sandbox
              -> 结果转为 Fact -> 下一轮 Intent
```

以下行为暂不实现：

- direct LLM 与 retrieve_first LLM 并行生成方案；
- 两个 LLM 并行操作同一 Sandbox；
- 将 direct 和 RAG 候选建模为两个独立 Intent；
- RAG 对 Intent 的局部步骤注入、匹配算法和候选保留/清理规则。

未来 RAG 若重新进入范围，需要单独确认：调度器如何判断是否检索、如何定义高匹配、retrieve_first 失败后何时执行已保存的 direct 候选，以及 RAG 成功后 direct 候选是否保留。

## 5. 阶段 0：确认产品边界与首条切片

**状态：已确认**

已确认：

- 本仓库独立拥有 Agent；`qinyuan-bat` 只做基础调用，最多通过一个适配层接入。
- `MCPs/`、`Sandbox/`、`pyproject.toml` 可修改复用；`MCPs/`、`Sandbox/` 是用户已验证的外围基础。
- 首期只通过 CLI 调用；先验证 CLI 至 MCP/Sandbox/结果的链路是否可跑通。
- 暂不指定实际靶场题；如验证需要真实目标，先向用户申请。实际任务可使用全部现有 MCP，但目标授权仍按任务确认。
- 失败、超时必须基于当次记录区分“本机/MCP/Sandbox 链路问题”与“目标任务问题”；前者保留重新调用 MCP 的路径，不能被当作策略失败。
- `TaskRun` 覆盖一次完整解题实例；同一 TaskRun 的所有 Intent 执行轮共用其 Sandbox。
- Intent 轮内按 LLM 给出的可能性顺序逐个尝试，采用深度优先回溯；首期不并行执行 Intent。
- 首期只执行 direct 方案；可保留可选 `intent_type` 元数据，但暂不用于调度。

仍必须逐项确认：

1. **CLI 契约**：首个 CLI 命令如何接收任务输入（命令行参数、JSON 文件、stdin 或其他）？结果应输出人读文本、JSON、两者，还是写入文件？首期命令结束后只返回最终结果，还是也需要实时事件？请给出退出码规则，或确认先在计划中保留该问题。
2. **跑通验证的边界**：即使不使用靶场题，端到端验证也需要一个明确 MCP Tool 和输入。是否允许先用一个不访问外部目标的本地 Sandbox 命令来验证链路？若不允许，请在需要时由你指定靶场题或测试目标；在那之前不执行真实运行。
3. **MCP/Sandbox 故障处理**：用户已要求按证据具体分析。首次实现前仍须定义：哪些记录足以判定为 MCP Server、Sandbox/Docker、宿主机配置或目标任务问题；遇到各类问题时是重新调用、重启/重建、换 MCP，还是直接向用户报告。此项暂不预设通用重试次数。
4. **Procedure 执行失败后的路径**：渗透过程中某个工具步骤未通过时，是否走 `Status -> Mismatch` 后在同一 Stage 继续让 LLM 选择，还是记录失败后重新规划下一 Stage，或直接结束？请分别说明工具返回非零、超时、权限拒绝和后置条件不成立时的期望路径。
5. **Procedure 校验失败的路径**：`ProcedureValidate` 校验的是候选 Procedure 的步骤、参数、断言与真实 Trace。校验不通过时，是否仅丢弃 Procedure Candidate、保留 ResultFact Candidate，还是整个 Item 都不能提交？是否写入失败记忆？
6. **补充图的设计是否采用**：是否确认“每轮新开 LLM，使用本轮 Intent 查询 RAG，只注入相关 Fact/失败摘要/Skill 规则”的模式？补充图中的 `Process_Fact` 是否就是原图的 `ProcedureFact`？LLM 是否允许读取本次工作目录；若允许，哪些目录、权限和文件类型可读？

### 5.1 候选：精确命令去重（仅可行性结论，未获实现授权）

用户提出“既然 MCP Client 的函数固定，是否匹配相同 commands 并直接反馈 LLM”的思路。结论：**可以实现，但应匹配实际执行命令，不能只匹配 MCP 函数参数。**

- 现有 MCP Client 接收 `qualified_name` 和参数对象后，由 MCP Server 返回实际 `sandbox_command`；执行结果已经包含 `qualified_name`、`executable`、`arguments`、状态、退出码、超时标志和输出。
- 同一个 MCP Tool + 相同参数通常可能得到相同 command，但接口不承诺必然如此；MCP Server 可以按环境或服务端逻辑生成不同 command。因此指纹应在命令计划生成后建立，而不是在 LLM 发起 Tool Call 时猜测。
- 最小候选是：本任务范围内，如果同一实际 command 已经得到**被确认为目标任务不适用的失败证据**，则在执行前阻止完全相同的 command，并向 LLM 返回简短的结构化 `duplicate_attempt` 原因和证据引用。
- `MCP Server 未在期限内生成命令`、宿主机/Sandbox 中断、Docker 故障和 `outcome_unknown` 不能加入“此路径走不通”的集合；它们只形成运行环境故障记录，保留重新申请/调用 MCP 的路径。
- 仅精确匹配无法识别“改了一个参数但本质相同”或“不同命令组合却同一策略”。语义相似策略识别属于后续独立问题，不能在首期假定 LLM 自己能可靠判断。
- 这项机制单独节省的 prompt 文本有限；它的直接价值首先是避免重复执行、重复的 MCP 输出和无效耗时。若每轮新开 LLM 并且只检索短摘要与证据引用，而不重复注入完整 stdout/stderr，才会产生明显的上下文节省。

开始实现这个候选前仍必须确认：

1. 命令指纹是否只使用 `executable + arguments`，还是还包含 MCP Tool 名、目标/Scope、TaskRun、当前 Intent 或其他字段？
2. 哪些具体状态或证据可以证明“目标任务不适用”，从而允许加入去重集合？非零退出码本身是否足够？
3. 命中后是硬拦截、提示 LLM 重新选工具，还是允许用户显式强制重试？
4. 去重集合只在当前 Turn、当前 TaskRun，还是允许跨任务保存？何时失效？

确认后只产出：

- 一段明确的系统职责边界。
- 一条具体的首期纵向链路及不做事项。
- 可观察的成功、拒绝、超时/取消验收条件。
- 经用户确认的图中冲突解释。

验收证据：用户确认的答案已记录，且后续模块均能追溯到首条链路；仍未定义的能力明确列为不做。

## 6. 阶段 1：冻结核心契约，不接真实外部实现

**状态：已完成其中已确认的第 1 轮 A、B 契约；其余事项等待确认**

开始前必须询问：

1. `TaskRequest` 中 `Goal / Target / Scope / Deadline` 的字段、必填项、格式和校验错误分别是什么？
2. `Budget` 包含哪些维度：Turn、Stage、LLM 调用、工具调用、时间、Token、费用或其他指标？各自优先级是什么？
3. `TaskRun / Turn / Stage / Item / Event` 的身份标识、状态枚举和允许的状态迁移是什么？
4. Goal 的“已达成”由确定性规则、用户确认、平台回执、LLM 判断还是组合规则决定？
5. `partial / inconclusive / failed / cancelled / rejected` 的外部语义和区分标准是什么？
6. 单进程、并发任务和多用户隔离是否属于首期范围？

确认后计划产出：

- 纯业务契约和状态迁移，不依赖 CLI、SQLite、Docker、MCP 或具体 LLM SDK。
- 输入校验、非法状态迁移和 Deadline/Budget 边界测试。
- 外围端口仅覆盖首条切片实际需要的存储、模型、工具、时钟和事件能力。

验收证据：契约测试能表达首条链路及其拒绝、超时/取消路径；核心模块不导入外围 SDK。

## 7. 阶段 2：定义 SQLite 状态所有权与原子提交

**状态：等待确认**

开始前必须询问：

1. SQLite 是唯一正式存储，还是首期适配器？数据库文件由谁创建、放在哪里、保留多久？
2. `ResultFact` 的内容、范围、时效、可信状态、证据引用和去重规则是什么？
3. `ProcedureFact` 的参数、前置条件、步骤、断言、适用范围、版本和失效规则是什么？
4. `ExecutionRecord` 与 Artifact 保存哪些字段？stdout/stderr、文件、Hash 的保留、截断、脱敏和清理规则是什么？
5. Checkpoint 在哪些时机写入，恢复后哪些动作允许重放，如何保证副作用不重复？
6. 原子提交包含哪些对象？Fact 或 Procedure 校验失败时，Item、Event、Artifact 和其他已通过候选分别如何处理？
7. 是否需要 schema 迁移、崩溃恢复和跨版本恢复？

确认后计划产出：

- 核心定义存储契约，SQLite 适配器拥有 schema 和事务。
- 一次 completed Item 的原子写入，以及并发/崩溃下不产生半提交的集成测试。
- 最小上下文读取只返回已授权且与当前 Stage 相关的 Fact、Procedure 和短失败摘要。

验收证据：事务回滚、重开数据库、重复提交、恢复和最小上下文查询均有自动化测试。

## 8. 阶段 3：打通受控工具执行链

**状态：等待确认**

开始前必须询问：

1. `Scope / ROE / Rate / RiskPolicy` 的具体规则、优先级和拒绝原因格式是什么？
2. Target 如何规范化和匹配：IP、CIDR、域名、URL、容器名、本地文件或其他类型？重定向和解析后的新目标如何处理？
3. 首期允许哪个 MCP Server 和 Tool？是否允许网络访问、下载、Shell、提权或写文件？
4. ToolGateway 按任务、Stage、Procedure 还是单次调用下发权限？
5. 每个 TaskRun、Turn 或调用是否独占 Sandbox？取消、崩溃、恢复和结束后何时销毁？
6. MCP Server 在宿主机还是 Sandbox 内运行？只有命令在 Sandbox 内执行，还是整个 Server 也必须隔离？
7. 审计日志、原始输出和 Artifact 中哪些数据必须脱敏或禁止发送给 LLM？

确认后计划产出：

- 一条 `ToolRequest -> PolicyDecision -> MCP -> Sandbox -> ExecutionRecord` 链路。
- ToolGateway 只接受显式任务权限子集；Policy 拒绝发生在任何副作用之前。
- 使用一个用户确认的工具做真实集成，其余工具不纳入首期。

验收证据：允许目标成功；越界目标、禁用工具、超时和取消均被拒绝或终止，并保留可审计记录且没有越权副作用。

## 9. 阶段 4：打通一个有界 Stage

**状态：等待确认**

开始前必须询问：

1. StagePlanner 是确定性程序、LLM 规划还是混合实现？Stage 的前置条件、后置条件和时间片如何表达？
2. ContextBuilder 的相关性规则、最大上下文和“最近一次短失败摘要”格式是什么？
3. ProcedureSelector 如何判断命中、前置条件成立和不适用？多条 Procedure 同时命中如何排序？
4. StageEvaluator 如何验证后置条件，哪些证据可以把 Candidate 升级为 Verified ResultFact？
5. 短失败如何分类和去重？哪些失败消耗预算，哪些失败应立即终止任务？这套失败记录如何让后续 LLM 避免重复策略，同时不把完整历史塞入上下文？
6. GoalEvaluator 的输入、确定性要求和误判处理是什么？

确认后计划产出：

- 首期只实现一类 Stage 和一个明确后置条件。
- 使用内存 Fake Model/Tool 先验证控制流，再接阶段 3 已验证的真实 ToolGateway。
- 覆盖 Procedure 命中、LLM 探索、权限拒绝、后置条件失败、预算耗尽和 Goal 完成路径。

验收证据：同一输入和 Fake 依赖下状态迁移确定；真实工具链能生成经过验证并原子提交的 ResultFact。

## 10. 阶段 5：定义 ModelReasoner、Skills 与 Procedure 学习边界

**状态：等待确认**

开始前必须询问：

1. 首期模型提供方、协议、模型名、超时、重试和费用/Token 限制是什么？
2. Tool-Calling Loop 的最大轮数、停止条件、结构化输出和协议错误修正策略是什么？
3. Skills/Rules 的来源、格式、加载顺序、版本和是否允许运行代码是什么？
4. Procedure Candidate 是自动生成、人工提供还是两者都有？验证通过后可直接复用，还是必须人工批准？
5. Procedure 可以跨任务、目标、用户或项目复用吗？如何撤销、过期和防止污染？
6. 哪些数据允许进入模型上下文，哪些必须只留在控制面和 Artifact？

确认后计划产出：

- 模型协议适配器与核心控制流分离。
- LLM 只能提出工具调用或候选内容，不能直接提交 Fact、绕过 Policy 或修改存储。
- Procedure 编译和验证独立于普通 ResultFact 验证。

验收证据：模型超时、无效工具调用、协议错误和循环上限均产生明确状态；未验证 Candidate 永远不会进入可复用状态。

## 11. 阶段 6：补齐 start / resume / cancel / result / events

**状态：等待确认**

开始前必须询问：

1. CLI 的命令、参数、stdin/stdout/stderr 协议和退出码是什么？
2. Python API 是同步、异步还是两者都需要？公开对象是否允许长期兼容承诺？
3. `qinyuan-bat` 的基础调用具体采用哪一种已确认入口？唯一转接层的输入、输出、错误和版本兼容边界是什么？转接层是否只做协议转换，明确禁止承载调度、重试、状态或业务判断？
4. Event 的类型、顺序、持久化、重放、订阅和背压规则是什么？
5. cancel 是协作取消还是强制终止？取消后的 Sandbox、Checkpoint、Artifact 和结果状态如何处理？
6. resume 接受 TaskRun ID、Checkpoint ID 还是请求幂等键？哪些终态禁止恢复？
7. 进程重启后是否自动恢复，还是必须显式调用 resume？

确认后计划产出：

- CLI 和 Python API 都只依赖同一个 `AgentApplication` 入口。
- 先完成首条链路需要的最小命令和事件，不提前建设服务端或 UI。
- 恢复和取消通过持久化状态及真实 Sandbox 中断进行验证。

验收证据：独立进程可启动任务、观察事件、读取结果；取消和重启恢复不产生重复副作用或丢失已提交事实。

## 12. 阶段 7：首条纵向链路验收

**状态：等待阶段 0-6 逐项确认**

计划验收矩阵最终必须包含：

- [ ] 合法请求完成首条真实任务，返回 Verified ResultFact、证据引用和 Artifact。
- [ ] 非法输入或越权目标在产生副作用前被拒绝。
- [ ] Procedure 命中路径和 LLM 探索路径的预期行为已由用户确认并验证。
- [ ] 工具失败、后置条件失败和预算耗尽产生用户确认的状态。
- [ ] Deadline、取消和进程重启只输出已验证事实，并能解释未完成部分。
- [ ] 原子提交、幂等恢复、日志脱敏和 Artifact Hash 有可重复测试证据。
- [ ] 项目级测试、格式、静态检查、类型检查、构建和 `git diff --check` 通过。

首条链路通过前不扩展更多 MCP、更多 Stage 类型、多模型、多数据库、Web API 或 UI。

## 13. 阶段 8：后续扩展候选（不属于首期承诺）

只有首条链路已验证且用户再次确认后，才评估：

- 多 MCP / 多工具的权限矩阵和契约测试。
- 多任务并发、公平调度和资源配额。
- Procedure 版本治理、人工审批和跨任务复用。
- 远程 MCP、其他 Sandbox Runtime、其他模型协议或数据库。
- 服务 API、Web UI、分布式执行和生产部署。

这些项目不是当前需求，不能据此预建抽象。

## 14. 当前实施边界

第 0 轮已经确认。第 1 轮 A 实现了用户明确授权的 `TaskRequest`、
`TaskRun`、Deadline、完成确认、模型失败终态和 Sandbox 生命周期端口。第 1 轮 B
实现了纯内存的 `Turn / Intent / Plan / Execution / Fact` 状态机、固定 Intent
列表、按 priority 的深度优先回溯、失败分类、单次环境故障重试、失败摘要，以及分离的只读
`ExecutionEvidencePort` / `PlanValidationEvidencePort`。这些实现均只使用 Fake 依赖，不调用真实模型、MCP、Docker、CLI、SQLite、目标或附件。

## 15. 多轮搭建计划（按流程图执行）

**总原则：** 每一轮只推进一个可验证的纵向切片；该轮开始前先向用户询问所有会改变接口、状态、权限、外部副作用或验收方式的问题。问题未回答前，该轮保持“等待确认”，不以合理猜测替代确认。

### 第 0 轮：冻结边界与适配目标（当前轮）

**状态：已确认。**

已确定的边界：

- Agent 核心独立位于本仓库；`qinyuan-bat` 最多增加一个协议转接层，不复制调度器、状态机或重试逻辑。
- 入口先做 CLI；CLI 收到一次题目运行请求后立即创建一个 `TaskRun` 和一个全局唯一 Sandbox。
- 一个 `TaskRun` 表示一道题的一次完整解题实例；所有 Intent 执行轮共用该 Sandbox，完成、取消或 Deadline 后销毁。
- 同一 Intent 轮内按 LLM 返回的可能性顺序逐个尝试，采用深度优先回溯；首期不并行 Intent，也不并行操作 Sandbox。
- 首期只实现 direct 方案生成；保留可选 `intent_type` 元数据，但首期不依据它调度；RAG 和双 LLM 候选暂不实现。
- MCP、Sandbox、SQLite、失败分类、Fact 和事件的具体字段与状态迁移，必须在对应轮次开始前单独确认。

适配目标：

- 框架核心只依赖一个与供应商无关的模型边界，计划提供 Codex、Claude Code、DeepSeek 三种适配器。
- 适配器负责外部调用协议、认证配置、响应解析、工具调用/结构化输出转换、超时和供应商错误映射；调度器不直接读取供应商 SDK 或 CLI 输出。
- 三个适配器是否实际使用 API、CLI、代理或其他方式尚未确定；在用户确认前不假设 Codex 等同于某个 CLI，也不假设 DeepSeek 一定使用某种兼容协议。
- 三个适配器是否共用 Token、是否允许分别配置模型和预算、是否支持流式事件，均列为适配轮次的确认项。

### 第 1 轮：核心领域契约与状态机（不接真实 LLM、MCP、Docker）

**状态：第 1 轮 A、B 已验证；后续外部适配和持久化等待逐项确认。**

第 1 轮 A 已实现：

- `TaskRequest` 的 `goal / target / deadline_seconds / model_backend` 基础契约。
- HTTP、HTTPS、TCP Endpoint 和 `local_path / url` 附件引用的已确认校验。
- CLI 参数与 TaskRequest 内 `model_backend` 的冲突检测，但尚未实现 CLI 本身。
- 全局 UUID `task_run_id`、TaskRun 总 Deadline、180 秒完成确认期。
- `achieved / assumed_achieved / failed / cancelled / deadline_exceeded` 状态迁移。
- Deadline 优先时的 Sandbox `interrupt -> destroy`，以及普通终态的 `destroy`。
- 纯 `SandboxPort` 和 Fake Sandbox；没有连接真实 Docker。

验证记录（2026-08-31）：

- `uv run --cache-dir .uv-cache --no-project --with pytest pytest tests/test_core_round1.py -q`
- 结果：`16 passed`。
- 有一条 pytest 缓存目录权限警告，不影响测试执行或断言结果。
- `agent_core*` 已加入 setuptools 包发现范围；真实 editable 安装和发布构建尚未作为本切片验收项。

第 1 轮 B 已实现并验证：

- `Turn -> Intent -> Plan -> Execution -> Fact` 的最小领域对象、固定状态枚举和非法状态迁移拒绝。
- 同一 Turn 内唯一正整数 priority 的升序选择；当前 Intent 终态后才可选择下一 Intent；`target_mismatch` 回溯下一 Intent。
- `environment_fault` 的命令生成重试一次，以及按 `plan_id + step_index + command_ref` 复用实际 command 的执行重试一次；第二次均转为 `inconclusive`。
- `policy_rejected`、`parameter_error`、`outcome_unknown`、`model_error`、`target_mismatch` 的已确认分流；只有 `target_mismatch` 出现在策略失败摘要中。
- 终态 `Execution.output_ref`、Fact 的 `source_ref`、只读 `ExecutionEvidencePort`、原始 Target 精确归属和有效时间范围的验证。
- 不可变 `PlanValidationRecord` 与独立只读端口；其 TaskRun、Plan、步骤、时间范围、连续 attempt 和一次性消费校验。命令生成失败直接引用该记录；执行重试复用原始成功记录，不产生虚构的命令生成证据。
- Fake 测试验证优先级、深度优先回溯、失败分类、Fact 验证、Turn/TaskRun 终态，以及 Plan 校验证据作用域；执行命令为 `uv run --cache-dir .uv-cache --no-project --with pytest pytest tests -q -p no:cacheprovider`，结果为 `43 passed`。`python -m compileall -q agent_core tests` 同时通过。

本切片仍明确未实现：CLI 长连接、最终 JSON/事件输出、附件导入、真实模型、
真实 MCP、真实 Docker、RAG、SQLite，以及将该状态机接入真实工具执行的控制循环。

目标：用纯 Python 定义并测试 `TaskRun -> Turn -> Intent -> Plan -> Execution -> Fact` 的最小状态迁移。**已完成。**

计划产出：

- `TaskRequest`、`TaskRun`、`Turn`、`Intent`、执行计划、执行结果、`ResultFact`、失败摘要和 `TaskResult` 的最小契约；最终字段以用户确认结果为准。
- 状态迁移校验、Deadline/Budget 边界、深度优先 Intent 栈和回溯行为的单元测试。
- LLM、MCP、Sandbox、SQLite 均使用显式端口或 Fake，不在本轮产生外部副作用。

本轮验收：同一组 Fake 输入必须得到确定的 Intent 顺序、回溯顺序和终态；非法状态迁移必须可观察地拒绝。**已通过。**

开始前必须确认：`TaskRequest` 字段和校验、状态枚举、预算维度、目标完成判定、失败/取消/超时的外部语义，以及是否只支持单进程单 TaskRun。

### 第 2 轮：模型边界与 Codex / Claude Code / DeepSeek 适配器

**状态：已验证。**

目标：在不接 Agent 调度和 Sandbox 的前提下，让三个 Fake 供应商通过同一模型端口返回统一的 Intent 或 Plan 候选。

本轮已确认的契约：

- `ModelRequest` 必含 `model_request_id / task_run_id / turn_id / request_type / goal / target / turn_target / verified_facts / failure_summaries`；`request_type` 仅为 `intent_generation` 或 `plan_generation`。模型后端不进入请求，由外层按 TaskRun 选择。
- `PlanGeneration` 请求额外必含当前 Intent 的 `intent_id / description`；`IntentGeneration` 不携带当前 Intent。
- Fact 快照只含 `fact_id / content / source_ref`；集合字段必传、可为空，`content` 按 Python `len` 至多 256 个 Unicode code point，超限拒绝且不截断。`target` 直接复用不可变 `Target`，不展开附件或派生目标。
- `failure_summaries` 字段必传、可为空，只允许 `target_mismatch` 的 `failure_type / short_reason / evidence_ref`，不注入其他失败类别。
- 统一响应仅为 `IntentCandidate[]` 或一个 `PlanCandidate`；模型不返回领域对象 ID。调用上层根据顺序和当前 Intent 创建 `Intent`、`Plan`。
- 原始无内容、明确拒答、认证失败、超时和供应商故障直接映射 `ModelError`，不纠正；结构不合法或结构化空数组由统一协调器使用同一适配器纠正一次。纠正使用新 `model_request_id`，仅带原请求引用和确定性 `short_reason`，不传完整原始响应。
- `ModelError` 固定为 `kind / short_reason / evidence_ref?`；`kind` 仅为 `authentication_failed / timed_out / cancelled / empty_response / refused / invalid_response / provider_failure`。若存在 `evidence_ref`，协调器通过只读引用端口确认其可查询。
- 模型端口为 `invoke(request, timeout_seconds, cancellation_token)`；调用超时是 `min(600, TaskRun 剩余秒数)`。`timeout_seconds <= 0` 时协调器不调用适配器并返回 `timed_out`。取消在本轮归一化为 `cancelled`，不修改 TaskRun。
- Fake Codex、Claude Code、DeepSeek 使用各自不同的合成原始夹具格式；格式只用于测试，绝不声明为真实供应商协议。

计划产出：

- 供应商无关的 `ModelRequest`、中间候选对象、`ModelResponse` 与 `ModelError` 契约；本轮不含 Tool Calling。
- 三个合成 Fake 适配器，分别模拟 Codex、Claude Code 与 DeepSeek 的不同测试原始格式；它们只负责协议归一化和错误映射。
- 只读 `DeadlinePort`、`CancellationToken`、`ModelErrorEvidencePort`；协调器统一执行 Deadline、取消、结构校验、错误引用归属检查和最多一次纠正。
- `Fact.content` 在创建与后续赋值时均限制为最多 256 个 Unicode code point，超限拒绝且不截断。

验证记录（2026-09-02）：

- `uv run --cache-dir .uv-cache --no-project --with pytest pytest tests/test_modeling.py -q -p no:cacheprovider`：`22 passed`。
- `uv run --cache-dir .uv-cache --no-project --with pytest pytest tests -q -p no:cacheprovider`：`65 passed`。
- `uv run --cache-dir .uv-cache --no-project python -m compileall -q agent_core tests` 与 `git diff --check -- agent_core tests TODO.md` 通过。
- 本轮仍未实现真实 Codex CLI、Claude Code CLI、DeepSeek 网络调用、凭据读取、模型后端装配、MCP、Sandbox 或调度器接入。

本轮验收：使用同一份模型请求契约，三个适配器都能在 Fake 或用户指定的可用连接上得到统一响应；协议错误、超时和认证错误不能被伪装成普通任务失败。

开始前必须逐项确认：

1. Codex 是通过 Codex CLI、OpenAI/Codex API、代理服务，还是其他入口调用？可执行文件/端点和输出格式是什么？
2. Claude Code 是通过 Claude Code CLI、Anthropic API、代理服务，还是其他入口调用？是否允许由 Agent 启动子进程？
3. DeepSeek 使用哪个端点、模型名和协议？是否已有可用的 OpenAI 兼容接口，还是必须采用其他 SDK/协议？
4. 三者的 API Token 是否分别配置？是否允许不同模型、不同超时、不同预算？密钥从环境变量、配置文件还是其他凭据源读取？
5. 三个模型都负责生成 Intent 和工具调用，还是指定一个模型负责规划、另一个负责执行/评审？
6. 模型是否必须原生支持 Tool Calling？若某个入口不支持，是否允许框架把工具描述作为受控 JSON 协议交给模型再解析？
7. 是否需要流式 token/event？首期是否只接受一次性完整响应？
8. 结构化响应的失败是重试、要求模型纠正，还是直接结束当前 Intent？由谁定义上限？

### 第 3 轮：控制面持久化与原子提交

目标：实现 SQLite 状态所有权，保存 `TaskRun / Turn / Intent / Event / ResultFact / ExecutionRecord / Checkpoint`，但仍可使用 Fake 模型和 Fake 工具。

计划产出：

- schema 与迁移入口；核心业务不直接依赖 SQLite 驱动。
- Fact、失败摘要、证据引用和 Checkpoint 的写入/读取契约。
- 一次 Item 完成的原子提交、重复提交、崩溃恢复和最小上下文查询测试。

本轮验收：事务失败不产生半提交；恢复只使用已提交事实，不重复未知的有副作用动作。

开始前必须确认：数据库路径和保留周期、各实体字段、Artifact/stdout/stderr 的保留与脱敏、Checkpoint 时机、恢复重放规则、Fact 可信状态和提交粒度。

### 第 4 轮：TaskRun 与 Sandbox 生命周期

目标：把第 1-3 轮的控制面接到现有 `SandboxService`，验证“CLI 接收后立即创建、TaskRun 内复用、结束后销毁”。

计划产出：

- TaskRun 级 Sandbox 绑定，不允许同一 TaskRun 更换 Sandbox。
- 完成、取消、Deadline、异常退出时的清理路径和可观察事件。
- 先使用 Fake Runtime/本地无目标命令验证生命周期，再根据用户许可运行 Docker 验证。

本轮验收：每个 TaskRun 恰好拥有自己的 Sandbox；多轮 Intent 不创建第二个 Sandbox；所有终止路径都能确认销毁或明确报告清理失败。

开始前必须确认：首个可运行的本地命令、挂载目录、Sandbox 网络、取消是中断还是销毁、Docker 验证是否现在授权，以及 Sandbox 创建失败时 TaskRun 的状态。

### 第 5 轮：ToolGateway -> Policy -> MCP -> Sandbox

目标：将 direct LLM 提出的一个工具调用接入现有 MCP Client，并把实际 command 放入同一 TaskRun Sandbox 执行。

计划产出：

- 受控工具清单、任务权限子集、范围检查和执行前拒绝。
- MCP Server 启停、工具发现、实际 command 计划、Sandbox 执行和 `ExecutionRecord`。
- 记录环境故障与目标任务失败的不同证据；环境故障不能直接写入策略失败集合。

本轮验收：允许调用有记录；越权、禁用工具、命令计划错误、MCP 超时、Sandbox 超时和取消分别得到明确结果，且越权请求不产生副作用。

开始前必须确认：首期 MCP Server/Tool、目标与 Scope/ROE、网络和写文件权限、MCP Server 运行位置、超时/重试/重启策略、审计输出范围和真实目标授权。

### 第 6 轮：ModelReasoner 与单轮 direct Tool-Calling Loop

目标：把第 2 轮模型适配器和第 5 轮工具链接入一个有界的 direct 推理循环。

计划产出：

- `ContextBuilder` 只向模型提供当前 Intent 所需的 Fact、工具规格和用户确认的短失败约束。
- 模型只能提出工具调用和候选 Fact，不能绕过 Policy、直接写库或直接结束 TaskRun。
- 单个 Intent 的工具调用上限、协议错误处理、结果回填和失败分类。

本轮验收：Fake Model 能驱动一次成功工具调用；无效工具调用、模型超时、MCP/ Sandbox 故障和目标策略失败会进入不同的可观察分支。

开始前必须确认：模型上下文字段、Tool-Calling Loop 上限、协议纠正方式、每类失败后的动作、GoalEvaluator 的输入和后置条件。

### 第 7 轮：Intent 深度优先调度与跨轮 Fact

目标：实现流程图中的“生成多个有序 Intent -> 逐个深度优先尝试 -> 失败回溯 -> 成功写 Fact -> 生成下一轮 Intent”。

计划产出：

- Intent 优先级和回溯栈；同一轮不并行执行。
- `ResultFact` 验证后再提交；未验证原始输出不能成为 Fact。
- 当前 TaskRun 内的短失败引用和已验证 Fact 供后续轮读取，但不重复注入完整历史。

本轮验收：给定 A/B 两个 Intent 时，系统完整走完 A 的后续路径后才回到 B；失败路径不会被误报为环境故障；成功路径能进入下一轮。

开始前必须确认：Intent 是否允许动态追加、路径耗尽后的终态、同一命令去重字段和作用域、失败摘要格式/有效期、哪些失败可触发回溯。

### 第 8 轮：CLI、事件和 qinyuan-bat 单一转接层

目标：将已验证的 `AgentApplication` 暴露为 CLI；需要时再增加唯一的 qinyuan-bat 协议转接层。

计划产出：

- 首期最小 CLI 输入/输出/退出码和事件行为。
- `start / result / events / cancel` 的最小可用入口；`resume` 只有在恢复语义确认后实现。
- qinyuan-bat 转接层仅做协议转换，不承载调度、重试、状态或业务判断；其协议以 qinyuan-bat 实际源码核对后冻结。

本轮验收：独立进程可启动一次 TaskRun、观察必要事件、获得结构化结果，并在取消/错误时返回明确退出语义；转接层不会产生第二套控制流。

开始前必须确认：CLI 输入形式、输出格式、同步/异步、事件协议、退出码、取消/恢复语义，以及 qinyuan-bat 的确切 stdin/stdout 协议和兼容版本。

### 第 9 轮：无真实目标的端到端验证与真实题目申请

目标：先使用 Fake Model/Tool 或用户允许的本地 Sandbox 命令验证全链路，再决定是否申请真实靶场题。

计划产出：

- `CLI -> TaskRun -> Sandbox -> Intent -> direct Model -> MCP -> ExecutionRecord -> ResultFact -> TaskResult` 的最小端到端测试。
- 成功、权限拒绝、模型超时、MCP 故障、Sandbox 故障、目标策略失败、取消和 Deadline 验收矩阵。
- 只有本地链路证据表明必须使用真实目标时，才向用户申请具体题目、范围和 MCP。

本轮验收：所有已确认的流程边界都有实际日志、状态或测试证据；未验证的目标能力明确列为剩余风险。

### 第 10 轮：RAG（延期，不进入首期）

只有第 9 轮通过且用户再次授权后才评估：调度器按 Intent 规则决定是否检索静态本地知识库、RAG 结果只作为参考、retrieve_first LLM 可拒绝并回到 direct、direct 与 RAG 不建模为两个独立 Intent。高匹配算法、局部步骤适用范围、候选保留/清理和是否并行生成，均需重新逐项确认。

## 16. 下一切片开始前需要确认的问题

第 1 轮 A、B 已验证。下一步尚未获授权，不创建真实模型适配器。若下一切片进入
“统一模型边界与三个 Fake Provider”，需要先确认：

1. 是否先做供应商无关的模型契约与三个 Fake 适配器，暂不启动本机 Codex CLI、Claude Code CLI 或 DeepSeek 网络请求？
2. Codex/Claude Code 在本框架中是否必须作为“只生成 Intent/工具请求的解题 LLM”，禁止它们绕过 Qinyuan 的 ToolGateway 直接执行 Shell、修改文件或访问目标？
3. 每次模型调用是启动全新 CLI 进程，还是在同一 TaskRun 内复用 Codex/Claude 会话？失败路径要求“重开 LLM”时，是新进程、新会话，还是只新建一次请求？
4. 统一响应首期包含哪些内容：自然语言分析、按可能性排序的 Intent、单个工具请求、候选 flag、停止原因？请确认字段和必填性。
5. 工具请求是否采用框架定义的受控 JSON，而不是直接依赖三个模型各自的原生 Tool Calling？如采用 JSON，协议错误是立即 `failed`，还是允许要求同一模型纠正；最多几次？
6. 首期模型响应是否一次性完整返回，不转发 token 流；stderr 只发送阶段/状态事件？
7. 除 TaskRun 总 Deadline 外，单次模型调用是否另设超时？若需要，请给出默认值；取消或 Deadline 到达时是否强制终止 CLI 子进程/HTTP 请求？
8. CLI 与 TaskRequest 都未提供 `model_backend` 时，是拒绝输入，还是采用默认后端？如有默认，请指定。
9. Codex CLI 和 Claude Code CLI 后续真实接入时，是否从 `PATH` 查找 `codex` / `claude`，还是由环境变量提供可执行文件路径？具体命令参数与 JSON 输出格式需在真实接入前再次确认。
10. DeepSeek 的 `base_url`、模型名和 Token 环境变量名仍未定义；是否继续将其保留为阻塞项，直到真实 DeepSeek 适配器实现轮再确认？

若下一步应先补 `Turn / Intent / Fact` 的纯核心契约，而不是模型边界，请先指定该顺序；届时必须先确认这些对象的字段、状态、所有权和失败/回溯语义。
