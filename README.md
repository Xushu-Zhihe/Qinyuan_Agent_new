# Qinyuan_Agent_new

T5 提供同步的单 Intent 文件类型识别 CLI，支持真实 DeepSeek API、真实 Docker、本机 File MCP、SQLite 事件和证据验证。它不负责自主解题、寻找 flag 或平台提交；完整单题调度属于 T6。

最新复核（2026-09-25，用户提供）：T5 历史真实效果复核通过；本次完整独立复验受环境权限阻塞，不能宣告全部通过。`468 passed、1 skipped` 为历史记录，本次 T5 专项为 `12 passed、4 failed、18 errors`，具体边界见 [TODO.md](TODO.md)。

T6 继续以 CLI 验收，并补充手工测试步骤；本地工作台已接入同一 T6 CLI 作为控制面和只读进度投影，T7 继续延期。

T6-5 已接入独立的 `run-task` v2 CLI、模拟多轮链路和工作台真实模式。T6-6 已接通单一公网目标的隔离转发代码，真实 Docker/模型/目标网络由操作者手工验收。工作台只读取 CLI 持久化状态和有界证据，不拥有或改写 Agent 控制面。没有独立答案校验器时，候选 flag 仅保留为 pending，整体目标始终显示 `not_evaluated`。

## 本地工作台（D9-1／D9-2）

工作台是浏览器可用的本地题目管理和 Agent 运行界面。默认入口采用公共 v3／d8_v7／内部契约7；模拟使用确定性双角色模型，真实模式接入双角色 deepseek-flash，两种模式均使用本地模拟 Sandbox。实际调度、审核、会话及交接由 D8 核心执行。开始按钮启动本机 Agent CLI 子进程，状态、事件、取消和最终 JSON 来自持久化运行目录。标准见 [D9-1](Plan/D9_1_proposal.md)、[D9-2](Plan/D9_2_proposal.md)、[安全诊断补强](Plan/D9_2_diagnostics_proposal.md)。真实 API 验收单独记录；Docker及实际解题留后续接入。

2026-10-14 首次真实API验收已执行：外层两次响应在业务校验中被拒绝，内层尚未进入，真实双角色闭环未通过。安全证据及后续诊断建议见[真实验收记录](Plan/D9_2_live_acceptance.md)；不自动新开运行重试。

后续已按用户确认为新7构建树规划业务诊断，真实提示版本为qinyuan.d8.outer.v2／inner.v2。新7记录固定安全拒绝码、路径及标签，并在既有一次纠正中使用；错误正文不保存。旧6和首次失败运行不补诊断、不升级或重派。

2026-10-14 经用户授权执行一次新7真实复验：外层规划及真实内层Plan／大小／SHA-256动作已应用，两次模拟Execution成功；Fact提案及既有一次纠正均业务拒绝，尚未进入Fact审核或交接，完整闭环仍未通过。另有event_export_timeout，运行及证据保留，无自动重试。详情见[新7真实复验](Plan/D9_2_live_v7_acceptance.md)。

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

旧v2工作台使用 `start-workbench.cmd --protocol-version 2`；旧v3模拟使用 `--protocol-version 3 --execution-profile d7 --d7-mode simulated`；旧D8契约6可用 `--execution-profile d8_v6`。已有运行按原声明恢复，不因新默认升级。旧模型页面的Key只保留在服务内存；新版真实链路使用指定私密文件。

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
