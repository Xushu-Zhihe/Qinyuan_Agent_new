# T6 实施与确认记录

状态：T6 通用脚本第一切片已实现并通过聚焦模拟测试；完整 T6 尚未实现或验收。本文中的未确认部分不是已冻结契约。

最新执行安排：按用户要求分为 T6-1～T6-6 六个代码批次，详见 TODO.md 的“T6 分批交付安排”。T6-2 的通用观察证明与 Fact/Intent 原子提交已完成内部链路；T6-3 的有序 Intent 生成、审计与注册已接通内部入口。后续逐批进入深度优先调度、CLI/结果和真实闭环验收。代码批次不等于 TaskRun 的运行轮次，后者不设置轮数上限，由总 Deadline 与取消终止。

以下早期建议中已经完成确认的事项，以此记录覆盖：① 执行前固定输出原文/文件摘要条件，全部满足才提交观察 Fact 并完成 Intent；② 文件摘要取整个 Plan 结束时的首次可信观察；③ 当前与父轮候选耗尽后继续生成新轮；④ 通用 Bash 直接经过 Gateway/Policy 到 Sandbox；⑤ 候选 flag 暂为 pending，没有独立校验器时不自动判整题成功。这些事项不再重复征求确认。

依据：`Architecture.md`、`interface.md`、`protocol.md`、`restrict.md`、`TODO.md` 的 T6 节及 `Qinyuan_Agent_confirmed_flow.svg`。2026-09-25 对照当前源码核查。

## 1. 已确认范围

- 当前工作台支持题目与附件管理、模型配置，并可通过真实 Agent 子进程执行 T5 单附件文件识别；该桥接不是 T6 解题闭环，不连接远程题目。
- CLI 真实单题解题闭环仍需在工具、目标判定和访问范围逐项确认后实现，并交付手工复测步骤。
- 后续已确认远程目标为可选字段：没有目标的 Misc/Crypto 等题目可保存；Runner 接通后必须对空目标禁网，有目标时仅允许题目指定域名和端口。真实隔离尚未实现或验收。
- 题目附件及题目说明放在项目 `challenge/t6/`。当前已登记 `pwn` 与 `libc.so.6`，正式附件不归入 `.test/`，不自动删除。
- 当前题目是带远程目标的堆类型 Pwn 题；本轮只在工作台中记录题目信息和附件，不执行利用、连接远程服务或把 Pwn 二进制默认为可由 T5 文件识别解出。
- 沿用单 TaskRun、单 Sandbox、Intent 顺序深度优先、direct 模型、Policy 前置、证据验证及 Controller 原子提交边界。
- 前端当前还显示 T5 运行状态；Stage、Procedure、RAG、平台取题和平台提交不在本轮范围内。

最小交付路径：CLI 接收题目及授权 → 创建 TaskRun/Sandbox 并导入附件 → 模型生成有序 Intent → 同一 Sandbox 中逐个执行（专用 MCP 或通用脚本）→ 验证并提交 Fact → 任务目标评估 → 下一轮或持久化最终结果 → 清理。当前只打通了通用脚本执行、证据与 Controller Execution 提交，尚未接入 CLI 调度。

关键风险：文件识别不能替代解题；模型给出 flag 文本不能替代答案验证；新增调度不能重放未知模型调用或工具副作用；真实工具能力需匹配选定题目。

## 1.1 本地启动与真实运行接入（2026-09-25 补充）

根目录 `start-workbench.cmd` 启动 Python 管理后端、静态页面和真实 Agent 子进程桥。当前桥接复用 `python agent.py run-intent --mode real ...`，仍仅限 T5 单附件文件类型识别；多附件、无附件、远程目标和完整 Pwn/Misc/Crypto 自主解题会返回明确的未接通错误，不能宣称已经完成 T6。

接通后的最小验收路径：保存题目与 0～16 个附件 → 点击开始 → 按当前内存模型配置创建一次独立 TaskRun → 创建 Sandbox 并导入附件 → 生成 Intent/Plan → Policy 审核后直接在 Sandbox 执行 Bash（专用工具仍经 MCP）→ 保存独立证据 → 验证观察并评估 Intent → 后续 Intent/轮次 → 用户取消或预算耗尽 → 显示 pending 候选、执行记录与清理结果。每题独立运行，不设任务数上限。无目标完全禁网；有目标用此前确认的隔离网络和目标转发容器。

两个业务判断的早期建议（现已确认，执行以上方最新记录为准）：

1. **通用事实与 Intent 完成**：建议模型在执行前为 Intent 提出明确的可检查后置条件，由可信侧校验并固定；执行后由验证器检查归属、已停止的 Execution、证据完整性以及后置条件。首个通用事实只证明“本次工具输出中出现了指定原文”或“本次生成文件的精确摘要”，不把模型的漏洞推断、候选 flag 正确性当作 verified Fact。满足已固定条件才完成该 Intent；无法检查的分析结论保留为未验证观察。不能沿用文件类型专用校验器宣称任意结论都已验证。
2. **整轮没有进展**：候选 flag 只记 pending 并继续这一点已确认。对于不同的情形——所有 Intent 都失败或模型主动 stop——建议只结束当前 Intent/轮，保留分类失败摘要并重新生成下一轮，仍受总时限约束；模型错误继续沿用明确报错，外部结果未知继续暂停等待人工恢复。另一可选方案是在整轮无进展时暂停，等待用户补充题目信息。

上述两项已确认，按分批计划接入 Controller 和调度器；不以任意 Shell 循环替代既有核心链路。真实联网、容器创建/销毁、取消和隔离必须用可控测试目标验收；用户提供的远程 Pwn 题由用户自行测试。

## 2. 源码核查结果

| 位置 | 已有能力 | T6 缺口 |
| --- | --- | --- |
| `agent_adapters/cli_contract.py` | T5 单个本地附件、调用方指定 Intent、精确文件识别授权 | 完整题目请求、任务目标、工具权限、预算与结果协议尚未冻结 |
| `agent_adapters/application.py` | 组装真实模型、导入、单 Intent 循环及证据组件 | 目前直接用请求描述构造唯一 Intent；切片结束固定为 `single_intent_slice_finished` |
| `agent_core/turn_manager.py`、`turn_working_copy.py` | 有序选择 Intent、目标失败回溯、工作副本与控制提交 | 还需 TaskRun 级驱动；模型 stop、路径耗尽、成功后新一轮的处置须明确 |
| `agent_core/modeling.py` | Intent 生成、Plan/动作结构校验与调用审计已接通 | 尚未由完整 TaskRun 调度器驱动 |
| `agent_adapters/deepseek_model.py` | 真实 Intent/Plan/动作 JSON 协议及受控超时、取消 | T6 Intent 真实模型运行尚未验收；动作仍限文件类型识别 |
| `agent_core/model_invocation_audit.py` | Plan/动作和 Turn 级 Intent 生成调用台账 | 尚未由完整 TaskRun 调度器恢复轮次 |
| `agent_core/intent_goals.py`、`file_type_verifier.py` | 文件类型目标与确定性证据验证 | 通用找 flag 目标、候选证明及任务目标评估尚未实现 |
| `agent_core/controller.py` | 完成确认、取消、Deadline、清理 | 已有 180 秒外部确认窗口及 `assumed_achieved`；本地答案验证如何接入须确认 |
| `agent_core/interfaces.py` | 控制、模型、工具等具体边界 | `RunSupervisorPort`、TaskRun `GoalEvaluatorPort` 仍是职责声明 |

## 3. 确认顺序及建议

每一步先给出具体契约和失败行为，用户确认后再实现对应切片；本表不授权预先实现所有建议。

### 第一步：题目与成功判据（进行中）

已选本地附件题。请提供原始附件与题目说明；若有已知答案，应单独保留，不混入给解题模型的附件。

建议以独立校验器验证答案或其摘要，解题模型只获得题目、允许工具和可验证观察。必须区分“证据中确有该候选字符串”与“候选确为本题正确答案”。仅符合 flag 格式不足以确认正确。

待确认：具体附件；是否有已知答案/其他可靠校验方式；没有正确性判据时允许报告什么结果。首个真实验收题尚未确定，不能宣称覆盖一般逆向题。

### 第二步：新增工具与事实

先只读检查附件格式，再按实际题目提出最小工具集合。例如文本读取、可打印字符串提取、特定编码解码是不同能力，不能一概授予任意 shell/Python 执行。

每个新增工具均须确认：输入结构、精确授权范围、读取/输出上限、超时、是否生成文件、成功及错误分类、证据字段、Fact 候选与确定性验证规则。涉及解压、运行附件或其他写入时另行确认，不能由“本地附件题”推导授权。

已确认通用 Bash 使用 T6CommandGateway → Policy → Sandbox 和独立证据，专用 File Tool 仍使用 ToolGateway → Policy → MCP → Sandbox；授权不足时在副作用前拒绝。

### 第三步：Intent 调度、预算与失败回溯

建议 RunSupervisor 只通过已有 Controller/工作副本推进状态；模型生成有序候选，可信控制逻辑将候选绑定为受授权的目标。

已确认运行轮数与总调用次数不设数量上限，由 TaskRun 总时限及取消约束；当前与可回溯父轮候选耗尽后生成新轮。每轮候选沿用已有非空列表契约，不新增 4 个上限。后续 T6-3/T6-4 逐项落实回溯位置持久化、重复策略与模型 stop 的具体调度语义；代码交付按 TODO.md 的六批推进。

确定环境故障、目标不匹配、Policy 拒绝和结果未知必须分别处理。未知模型调用/工具结果不自动重派；环境故障不能写成策略失败摘要。

### 第四步：任务目标判断、完成确认与最终结果

建议 GoalEvaluator 只读已提交 Fact 及可复核证明，Controller 拥有最终状态写入权。最终结果分开报告候选答案、正确性验证、支持证据、TaskRun 终态和清理结果。

临时规则已确认：没有独立答案校验器时，候选 flag 保持 pending，不自动触发 `achieved` 或 `assumed_achieved`；观察 Fact 仅能完成当前 Intent。T6-5 再确认独立校验器接入、`partial` 外部字段及取消/Deadline 的结果映射；既有 T5 生命周期接口保持兼容。

### 第五步：CLI、持久化与恢复契约

建议新增完整任务入口并兼容 T5 `run-intent`；具体命令、版本、请求/结果字段和退出码待确认。`status`、`replay`、`resume`、`cancel` 应按任务类型复用应用边界。

需明确 Intent 生成台账、调度决定与结果记录的原子性及幂等范围；通过兼容迁移保留旧库。崩溃后只从已提交记录恢复，不能重新请求模型猜测原先的 Intent 列表。

## 4. 实施与验收切片

1. 选定真实题目并冻结该题所需工具/Fact 契约；先验证真实附件导入、受限工具、证据与候选验证链。
2. 实现受预算约束且可恢复的 Intent 生成与 TaskRun 调度；自动化测试覆盖回溯、路径耗尽、跨轮和未知结果。
3. 接入任务目标验证、完成确认与 TaskResult；测试伪造答案、错误答案、证据缺失/损坏及终态提交失败。
4. 接入确认后的完整 CLI，保持 T5 协议回归；补齐事件、恢复、取消和清理路径。
5. 实际运行真实 DeepSeek + 已确认 Docker 镜像 + 原始附件；保存 CLI 输出、事件、工具证据、Fact、答案验证及清理记录。真实成功与受控模拟失败测试分别记录。
6. 提供手工复测文档：题目准备、授权与环境、准确命令、进度/事件、证据、最终结果、取消/恢复和失败处理。

真实多 Intent/回溯是否发生需检查实际事件，不以预设流程或一次工具成功替代。模型行为不保证固定，确定性故障测试与真实运行证据各自标明。

## 5. 当前工作状态

- 已完成约束与相关源码核查，并创建题目目录。
- 已新增本地工作台服务与页面：支持 SQLite 题目/附件持久化、0～16 个附件、可选远程目标、归档与永久清理、模型配置；页面可启动 T5 `run-intent` 子进程，但不连接远程目标。运行态偏差及修复见 `T6_flow_audit.md`。
- T6 多 Intent Runner、候选 flag 验证及完整 TaskResult 尚未实现；目标专用网络已有适配器和模拟测试，但真实隔离尚未验收。T6-2 通用观察证明与 Fact/Intent 原子提交、T6-3 有序 Intent 生成与注册已接通内部入口，尚未接入工作台；现有 T5 真实模型与单 Intent 文件识别不能代替真实解题闭环验收。
- 本次选取 T5 非官方模型地址拒绝、严格 JSON 与不可覆盖声明的 6 项现有检查，重跑为 **6 passed、11 deselected**。首次受缺失的临时父目录及缓存权限影响；补齐父目录并在沙箱外重跑通过。命令如下，产物均在 `.test/`，不调用真实模型或 Docker。这只是输入/文件契约基线，不是 T5 全套或 T6 验收。

```powershell
. ./scripts/test-env.ps1
New-Item -ItemType Directory -Path .test/t6-baseline -Force | Out-Null
uv run --offline --no-project --with pytest --with docker --with 'mcp==1.29.1' python -m pytest tests/test_t5_cli.py -q -k 'deepseek_rejects_nonofficial_configuration or strict_json_and_immutable_declarations' --basetemp=.test/t6-baseline/pytest-tmp -o cache_dir=.test/t6-baseline/pytest-cache
```
