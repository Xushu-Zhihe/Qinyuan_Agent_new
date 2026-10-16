# Qinyuan_Agent_new

D9-4后续一次真实验收在启动阶段失败，未进入模型／工具／候选，CLI退出5；没有自动重开、恢复或清理。根因尚未确认，见[真实记录](Plan/D9_4_live_acceptance.md)。以下离线通过记录保留原验收范围。

新启动默认内部19／d8_v19／提示v14，真实／模拟仍显式选择。新19在局部成功后、child扩展前独立判断候选：无候选继续，有候选原文保护保存后等待可信反馈。可信拒绝保留Fact与局部成功；最终接受才可整体达成。工作台新增核查／显式恢复，核查本身不调用模型或执行任务；核查确定拒绝后由操作人恢复。真实验证适配器尚未接入，明确报未配置，不提供网页accepted/rejected裁决。确认范围已构建，离线主组合211项通过；标准见[D9-4方案](Plan/D9_4_proposal.md)，分层回归及限制见[验收](Plan/D9_4_acceptance.md)，真实解题仍留后续验收。

以下D9-3及更早段落为当轮历史记录，其默认版本不覆盖上述新19选择。

新启动默认内部18／d8_v18／提示v13；模拟／真实模式仍显式选择。新18真实模式使用专属Docker及程序冻结的镜像／资源配置，模拟继续使用本地替身；旧6～17运行按原后端恢复，不原地升级。现阶段只接通既有固定大小／SHA观察，不开放通用工具或声称整题完成。普通暂停保留容器，显式取消／清理使用原公共入口，查询不需要模型密钥或Docker派发。真实Docker专项2＋1项通过；唯一真实运行35次模型调用完成、6次真实执行及3条verified Fact，最终受已有review限额停止，整体not_evaluated。见[D9-3验收](Plan/D9_3_acceptance.md)及[真实记录](Plan/D9_3_live_acceptance.md)；以下D9-2及S轮段落保留历史状态。

最小收尾及唯一真实复验已记录：33次模型调用均完成、0未决，6次本地模拟执行、3条verified Fact及2个局部成功，原审核／Plan动作冲突已跨过。最后在review分类8/8处停止，总额43/60、事件62/62，最终not_evaluated；不增加预算或自动重跑。见[收尾验收](Plan/D9_2_close_acceptance.md)及[真实记录](Plan/D9_2_close_live_acceptance.md)。

新运行默认内部17／d8_v17／提示v12：审核策略与Plan共享现有可执行路径，冲突在约束提交和Plan注册前拒绝；旧6～16保留。真实Docker及所需工具仍留D9-3。范围和验收见[最小收尾标准](Plan/D9_2_close_plan.md)。以下S6及更早段落为历史记录。

新16／d8_v16、提示v11采用当前状态派生的Plan合法组合、同源Fact验证投影及无单字段上限的复盘理由；工作台新默认16，模拟／真实仍显式选择。旧6～15及历史证据保持，协议3、schema27、预算60和原纠正上限保持。新专项22 passed；唯一真实验收progress.reason=835字符首次接受，Fact／局部成功／摘要通过，后续审核动作约束与Plan需求冲突而停止，完整流程未通过。详见[S6构建验收](Plan/S6_rule_evidence_reason_contract16_acceptance.md)与[S6真实记录](Plan/S6_live_v16_acceptance.md)；以下保留S5历史结果。

S5最终专项＋公共入口Fake 74 passed；扩大回归571 passed、2个原基线失败。唯一真实运行17次调用完成、2次原规则纠正、0未决，真实核验两份交接摘要；治理／读取／D5提炼只获Fake覆盖，末次D5业务拒绝补齐亦仅Fake。整体目标仍not_evaluated。见[S5真实记录](Plan/S5_live_v15_acceptance.md)。

S5新15（2026-10-15）已接入治理／关系精确选择、读取null等价省略、条件审核和D5主体／范围装配；新工作台默认d8_v15、真实提示v10，旧6～14保留。模型继续明确选择证据并生成摘要／claim／理由；混源业务拒绝不增加重试，摘要／过程总结不升级verified。最新Fake与真实结果分别记录于[S5验收](Plan/S5_governance_read_summary_acceptance.md)，冻结范围见[S5标准](Plan/S5_governance_read_summary_proposal.md)。下文保留S4及更早历史。

最新S4真实复验（2026-10-15 13:22–13:24）完成与局部审核主链通过：模型明确选择支持Fact，外层收到原目标及有来源大小／SHA观察后判succeeded，Controller已提交；14次调用完成、0纠正／0未决，事件27/27，预算18/60。扩展耗尽后Turn结束，TaskRun最终目标仍not_evaluated；候选未触发，无Docker／题目验收。见[S4复验记录](Plan/S4_live_v14_recheck_acceptance.md)。下文首次未知与离线范围保留为历史。

2026-10-15后续授权的S4单次真实API验收未完成：外层首次树规划等待约180秒后结果未知，停止派发并保留证据；内层及S4局部审核均未进入，不能判定S4业务失败或真实通过。没有重试、恢复或清理，详见[S4真实记录](Plan/S4_live_v14_acceptance.md)。下文“本轮无真实API”保留为此前离线构建轮的历史范围。

S4新14（2026-10-15）已完成确认范围构建／离线验证：新默认d8_v14、真实v9提示；完成申请保留明确Fact引用，局部审核补齐有来源的首次大小／SHA观察与原目标／能力范围，模型继续作语义判断。候选保留原文与execution_id选择，程序装配固定来源字段，普通绑定不存候选正文。旧6～13及已有运行保持原声明；本轮无真实API／Docker／目标调用，不接通D9-4反馈。冻结见[S4标准](Plan/S4_completion_evaluation_candidate_proposal.md)，实际数量与限制见[S4验收](Plan/S4_completion_evaluation_candidate_acceptance.md)；下面保留各旧轮历史范围。

T5 提供同步的单 Intent 文件类型识别 CLI，支持真实 DeepSeek API、真实 Docker、本机 File MCP、SQLite 事件和证据验证。它不负责自主解题、寻找 flag 或平台提交；完整单题调度属于 T6。

最新复核（2026-09-25，用户提供）：T5 历史真实效果复核通过；本次完整独立复验受环境权限阻塞，不能宣告全部通过。`468 passed、1 skipped` 为历史记录，本次 T5 专项为 `12 passed、4 failed、18 errors`，具体边界见 [TODO.md](TODO.md)。

T6 继续以 CLI 验收，并补充手工测试步骤；本地工作台已接入同一 T6 CLI 作为控制面和只读进度投影，T7 继续延期。

T6-5 已接入独立的 `run-task` v2 CLI、模拟多轮链路和工作台真实模式。T6-6 已接通单一公网目标的隔离转发代码，真实 Docker/模型/目标网络由操作者手工验收。工作台只读取 CLI 持久化状态和有界证据，不拥有或改写 Agent 控制面。没有独立答案校验器时，候选 flag 仅保留为 pending，整体目标始终显示 `not_evaluated`。

新9已完成普通复盘阶段契约、程序绑定作用范围ID及针对性安全纠正，旧6／7／8不升级，progress_review后续单独补强。最终专项与四版本输出矩阵174项、回归387项、新9工作台实际子进程1项通过，重叠不加总；见[离线验收](Plan/D9_2_review_contract_acceptance.md)。2026-10-14 16:00～16:01执行唯一新9真实验收：树规划和开始复盘通过，内层完成大小／SHA-256观察，事件完整17/17；Fact提案解析合法但引用未包含当前proof_id，在外层审核前被来源绑定拦住，完整闭环仍未通过。预算11/60，运行保留，无自动重试；见[新9真实验收](Plan/D9_2_live_v9_acceptance.md)。标准见[普通复盘冻结方案](Plan/D9_2_review_contract_proposal.md)。

新10已补强同源Fact可用证明引用契约，模型仍明确填写当前proof_id，旧运行不升级。来源专项22项、相关回归233项、五版本输出矩阵171项通过，范围有重叠；见[离线验收](Plan/D9_2_fact_source_contract_acceptance.md)。2026-10-14 16:59～17:00执行唯一新10真实验收：树规划、开始复盘及Plan通过，但大小动作两次脚本文本与固定脚本不一致，被执行准入拒绝，Execution0、Fact阶段未进入，来源契约的真实效果未验证到。预算6/60，事件完整7/7，运行保留，无自动新开重试；见[新10真实验收](Plan/D9_2_live_v10_acceptance.md)。冻结见[Fact来源方案](Plan/D9_2_fact_source_contract_proposal.md)。

S3新13已按确认方案接通进展复盘：模式／classification／当前状态联合生成合法选项，程序绑定scope_id与审核水位；模型仍明确给理由、引用及最新决策举证，不自动修补fixed_probe或填引用。新默认d8_v13／真实v8提示；旧6～12保留。冻结见[S3方案](Plan/S3_progress_review_proposal.md)，离线结果与边界见[S3验收](Plan/S3_progress_review_acceptance.md)，本轮没有真实API调用。

2026-10-14后续唯一新13真实验收：S3普通进展首次meaningful／continue通过，模型明确引用最新决策，程序补全Intent身份并提交水位4；没有纠正。11次调用已知、未决0、预算15/60、事件22/22，S1／S2继承阶段亦通过。后续局部目标inconclusive，完整任务未通过；证据保留，无自动重试／恢复／清理，本次Sandbox为local_simulated。见[S3真实验收](Plan/S3_live_v13_acceptance.md)。

S2新12已按确认方案接通规划／断言装配：模型显式选择规则、附件、断言类型及proof引用；程序填写冻结目标、注册版本和Observation身份。原选择与完整装配绑定保留，外层审核、可信验证和原ID恢复仍严格。工作台新默认d8_v12／真实v7提示；旧6～11保留。标准见[S2方案](Plan/S2_planning_assertion_proposal.md)，结果与限制见[S2验收](Plan/S2_planning_assertion_acceptance.md)。本轮没有真实API调用。

2026-10-14后续唯一新12真实验收：S2规划／断言首次合法，程序装配、外层审核及可信证明复核通过，形成1条verified Fact；后续progress_review及一次纠正都含非法fixed_probe允许动作，第一次另有scope绑定不符，按原规则暂停。9次调用已知、预算13/60、事件20/20；完整流程未通过，无自动重试。见[S2真实验收](Plan/S2_live_v12_acceptance.md)。

S1新11已按确认方案接通固定观察动作装配：模型只选择fixed_probe的当前operation、有界timeout_seconds及review_requested；脚本、目录、stdin和固定条件由程序填写，并保存原输出与完整装配绑定。内部11／d8_v11、真实v6提示；旧6～10及运行证据不升级。真实API本轮不调用，具体离线验证及限制见[S1验收](Plan/S1_fixed_probe_acceptance.md)，冻结见[S1标准](Plan/S1_fixed_probe_proposal.md)。

2026-10-14后续唯一新11真实验收已完成：S1大小／SHA固定动作首次回答通过，装配及2次本地模拟执行正确，形成1条verified Fact；外层局部目标inconclusive暂停，完整任务未完成。10次调用已知、预算14/60、事件21/21，无自动重试。见[S1真实验收](Plan/S1_live_v11_acceptance.md)。

## 本地工作台（D9-1／D9-2）

工作台是浏览器可用的本地题目管理和 Agent 运行界面。默认入口采用公共 v3／d8_v13／内部契约13；模拟使用确定性双角色模型，真实模式接入双角色 deepseek-flash，两种模式均使用本地模拟 Sandbox。实际调度、审核、会话及交接由 D8 核心执行。开始按钮启动本机 Agent CLI 子进程，状态、事件、取消和最终 JSON 来自持久化运行目录。标准见 [D9-1](Plan/D9_1_proposal.md)、[D9-2](Plan/D9_2_proposal.md)、[树规划安全诊断](Plan/D9_2_diagnostics_proposal.md)、[Fact诊断及事件修复](Plan/D9_2_fact_diagnostics_proposal.md)。真实 API 验收单独记录；Docker及实际解题留后续接入。

2026-10-14 首次真实API验收已执行：外层两次响应在业务校验中被拒绝，内层尚未进入，真实双角色闭环未通过。安全证据及后续诊断建议见[真实验收记录](Plan/D9_2_live_acceptance.md)；不自动新开运行重试。

后续已按用户确认为新7构建树规划业务诊断，真实提示版本为qinyuan.d8.outer.v2／inner.v2。新7记录固定安全拒绝码、路径及标签，并在既有一次纠正中使用；错误正文不保存。旧6和首次失败运行不补诊断、不升级或重派。

2026-10-14 经用户授权执行一次新7真实复验：外层规划及真实内层Plan／大小／SHA-256动作已应用，两次模拟Execution成功；Fact提案及既有一次纠正均业务拒绝，尚未进入Fact审核或交接，完整闭环仍未通过。另有event_export_timeout，运行及证据保留，无自动重试。详情见[新7真实复验](Plan/D9_2_live_v7_acceptance.md)。

后续按用户确认采用新8／真实v3提示补充Fact提案安全诊断，并修复公共v3同步运行期间事件IPC缺少持续监控的问题。固定原因／安全路径进入原一次纠正，非法正文不保存；旧6／7继续原证据和角色版本，显式--execution-profile可选。该构建轮仅离线验证，不能还原已丢弃的真实Fact提案。

2026-10-14 12:54～12:55经用户另行授权执行一次新8真实验收：树规划通过，Turn开始复盘及既有一次纠正均业务拒绝，内层／Fact阶段尚未进入，完整闭环仍未通过。3次模型调用已知完成，预算4/60；事件完整导出5/5、无超时。拒绝原因和未验证边界见[新8真实验收](Plan/D9_2_live_v8_acceptance.md)，运行及证据保留，无自动重试。

确认范围已完成离线验证：最终专项45项、公共／旧链组合235项通过，组间重叠及时间范围分开记录；扩大核心存在3项修改前同样失败的D5测试及一项已修正并专项通过的新8旧预期，未宣告全项目全绿。详情见[Fact诊断与事件修复验收](Plan/D9_2_fact_diagnostics_acceptance.md)。

```powershell
uv run python -m agent_adapters.workbench --port 4174 --data-dir workbench_data
```

Windows 可双击根目录 `start-workbench.cmd`，或在终端运行：

```powershell
.\start-workbench.cmd
# 自定义监听端口（数据目录默认始终相对于仓库根目录）
.\start-workbench.cmd --port 4180
```

脚本优先使用项目 `.venv` 的 Python，否则使用系统 Python；不会自动安装依赖。终端就是管理后端，保持窗口打开，按 `Ctrl+C` 关闭。启动后打印 URL、数据目录及实际能力。

默认启动后，在开始按钮旁明确选择模式；也可用 `start-workbench.cmd --d7-mode simulated` 或 `--d7-mode real` 显式配置。模拟无需 API Key 或 Docker。新版真实模式使用仓库私密 `.env` 的官方 DeepSeek 连接；需要 `QINYUAN_LLM_BACKEND=openai`、`OPENAI_BASE_URL=https://api.deepseek.com`、`OPENAI_API_KEY`，超时180秒。两角色的模型和提示版本由新版请求冻结，不采用旧页面内存Key或 `QINYUAN_MODEL` 覆盖。thinking enabled、effort high、输出16384，可能产生思考耗时及费用；不自动重试。结果明确 `sandbox_backend=local_simulated`，不代表真实Docker或解题验收。请求可暂存0～16项附件、合计最多64MiB；执行支持1～2项且每项≤16MiB，0项为未评估终态并清理，超执行能力完整暂存后明确拒绝。内部搜索耗尽保持整体目标未评估，不生成假答案候选；可显式取消并清理。

旧v2工作台使用 `start-workbench.cmd --protocol-version 2`；旧v3模拟使用 `--protocol-version 3 --execution-profile d7 --d7-mode simulated`；旧D8契约6／7／8／9可用 `--execution-profile d8_v6`／`d8_v7`／`d8_v8`／`d8_v9`。已有运行按原声明恢复，不因新默认升级。旧模型页面的Key只保留在服务内存；新版真实链路使用指定私密文件。

打开 [http://127.0.0.1:4174/](http://127.0.0.1:4174/)。点击题目进入独立的全宽详情视图，顶部查看已提交的 TaskRun、Turn 和 Intent 关系，下方查看短 Plan、Execution、Fact 和题目资料；可返回列表。解题图的连线只表示归属与轮次顺序，不表示跨 Intent 依赖。服务只绑定回环地址，网页只读投影 Agent 进度，不判断候选 flag 正确性，也不宣称整题成功。

永久清理需先结束当前运行，再归档题目；归档后列表自动显示该题，输入题名确认才能删除题目、附件和运行目录。暂停中的运行可通过“取消并清理”结束；已终止但清理未确认的运行显示“重试资源清理”，只调用 CLI 的 `resume --cleanup-only`，确认资源清理成功后才可归档。若结果证据仍需恢复，页面保留该信息，不把它算作整题成功。

## 启动

Python 3.11–3.13，安装项目依赖后可使用 `qinyuan-agent-new`（兼容别名 `qinyuan-agent`），也可在仓库运行 `python agent.py`。

```powershell
uv sync --frozen
uv run qinyuan-agent-new run-intent --mode real --request .test/t5/request.json --state-dir .test/t5/runs --events .test/t5/new-events.jsonl
```

真实模式默认读取调用目录 `.env`，可用 `--env-file` 指定。仅需 `QINYUAN_LLM_BACKEND=openai`、`QINYUAN_MODEL=deepseek-v4-flash`、`OPENAI_BASE_URL=https://api.deepseek.com`、`OPENAI_API_KEY`，可选 `QINYUAN_LLM_TIMEOUT_SECONDS=180`。不在请求或日志中填写密钥。T5 拒绝代理端点，不自动重试网络请求。

Docker Engine 必须可用，且本机已有 `qinyuan/pentest-sandbox:0.3.4`；不会构建、拉取镜像或修改网络。容器网络为 none、非 root、只读根目录，无宿主挂载。`.env` 中历史 MCP/网络配置不扩大 T5 的工具或网络权限。

请求示例见 [protocol.md](protocol.md) 的 T5 v1 节。附件必须是调用目录以内的单个相对本地路径，授权只允许 `file.inspect_file` 和导入后的精确路径。

```powershell
uv run qinyuan-agent-new status --task-run-id <UUID> --state-dir .test/t5/runs
uv run qinyuan-agent-new replay --task-run-id <UUID> --state-dir .test/t5/runs --after-sequence 0
uv run qinyuan-agent-new resume --task-run-id <UUID> --state-dir .test/t5/runs --authorization current-authorization.json
uv run qinyuan-agent-new resume --task-run-id <UUID> --state-dir .test/t5/runs --verify-import
uv run qinyuan-agent-new resume --task-run-id <UUID> --state-dir .test/t5/runs --cleanup-only
uv run qinyuan-agent-new cancel --task-run-id <UUID> --state-dir .test/t5/runs
```

stdout 只输出一个最终 JSON。退出码 0 表示切片明确停止且清理成功；TaskRun 保持整体目标未评估的 inconclusive。未知结果不会自动重派，终态查询不会自动清理。退出码及完整恢复规则见 protocol.md。

## T6-5 `run-task` 手工复测

在包含本地附件 `sample.bin` 的工作目录启动。v2 请求格式见 [protocol.md](protocol.md)；授权必须是 `{"allowed_tools":["sandbox.command"],"allowed_target_urls":[]}`。真实模式需已配置 DeepSeek 及本机 Docker 镜像，不会拉取镜像，也不访问题目目标。先用模拟夹具核对结果与证据，再由操作者执行真实模式：

```powershell
uv run qinyuan-agent-new run-task --mode simulated --request request-v2.json --fixture fixture-v2.json --state-dir .test/t6-manual/runs
uv run qinyuan-agent-new run-task --mode real --request request-v2.json --state-dir .test/t6-manual/real-runs
uv run qinyuan-agent-new status --task-run-id <UUID> --state-dir .test/t6-manual/real-runs
uv run qinyuan-agent-new replay --task-run-id <UUID> --state-dir .test/t6-manual/real-runs
uv run qinyuan-agent-new resume --task-run-id <UUID> --state-dir .test/t6-manual/real-runs --authorization authorization-v2.json
uv run qinyuan-agent-new resume --task-run-id <UUID> --state-dir .test/t6-manual/real-runs --verify-import
uv run qinyuan-agent-new cancel --task-run-id <UUID> --state-dir .test/t6-manual/real-runs
uv run qinyuan-agent-new resume --task-run-id <UUID> --state-dir .test/t6-manual/real-runs --cleanup-only
```

`--verify-import` 仅用于报告 `import_outcome_unknown` 的真实运行，只读核对原件或工作副本，之后需另行 `resume --authorization`。普通暂停、夹具耗尽或模型错误返回 4；取消且无历史未知结果、清理确认时返回 130；Deadline 返回 3；未知副作用、损坏证据或清理未确认返回 5。真实运行中检查 `verified_facts` 的证明、`pending_candidates` 的来源、`recovery` 与 `cleanup`，不要把退出码或候选文本当作整题成功。

`execution_outcome_unknown` 不会自动重新派发命令。工作台可对仍在运行态的 TaskRun 显式“结束并清理”；CLI 用 `cancel` 终结任务，若终态仍显示 `cleanup:not_confirmed`，再用 `resume --cleanup-only` 重试资源清理。有远程目标时，只有执行 Sandbox 和目标转发网络两份清理收据均确认，才显示整体清理成功。

## T6-6 公网目标手工复测（操作者执行）

有目标时，先在仓库根目录准备固定转发镜像；Agent 不会自动构建或拉取镜像。目标必须是单个显式端口的 `http://`、`https://` 或 `tcp://` 公网地址。内网和回环 IP 会在提交前拒绝，DNS 每次连接由转发容器解析并只接受公网结果。无目标时仍使用禁网 Sandbox。

```powershell
docker build -f Sandbox/target_forwarder.dockerfile -t qinyuan/t6-target-forwarder:0.1.0 .
```

零附件目标题的 `request-v2.json` 可写为：

```json
{"protocol_version":2,"task_request":{"goal":"题目说明","target":{"attachments":[],"endpoints":[{"url":"tcp://target.example.org:24009"}]},"deadline_seconds":3600,"model_backend":"deepseek_openai"},"authorization":{"allowed_tools":["sandbox.command"],"allowed_target_urls":["tcp://target.example.org:24009"]}}
```

由操作者替换为已授权的实际公网目标，再按上节 `run-task --mode real`、`status`、`replay` 和 `cancel` 命令复测。Sandbox 通过 `target.qinyuan:<目标端口>` 访问目标；HTTP(S) 客户端需保留原始 URL 的 Host/TLS 名称并将连接指向该别名。核对 TaskRun 的目标网络绑定、命令证据、Intent 顺序和取消后的 Sandbox/转发容器/两张网络清理。代码构造阶段未运行真实 Docker、模型或目标访问。

取消后可用返回的 TaskRun UUID 检查残留资源：

```powershell
docker ps -a --filter "label=qinyuan.task_run_id=<UUID>"
docker network ls --filter "label=qinyuan.task_run_id=<UUID>"
```

## 模拟检查与测试

模拟模式必须显式提供单独夹具，如 `{"stdout":"ASCII text\n"}`，通过 `--mode simulated --fixture <JSON文件>` 使用；仅验证控制和证据规则，不证明真实文件类型。

```powershell
. ./scripts/test-env.ps1
uv run --offline --no-project --with pytest --with docker python -m pytest -q
uv build --offline --out-dir .test/dist
```

临时测试、构建、缓存统一放在 `.test/`。正式 `--state-dir` 不自动删除。验收记录与边界见 [TODO.md](TODO.md)。
