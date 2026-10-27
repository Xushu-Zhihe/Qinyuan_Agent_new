# Qinyuan Agent

Qinyuan Agent 是一个面向 CTF 与授权安全实验的双 LLM Agent。它通过本地工作台管理题目，由内层模型执行探索、外层模型负责规划和审核，在独立的 Docker Sandbox 中运行工具，并保存过程中的证据、结论与运行状态。

目前主要运行链路已接通，包括工作台、双角色模型、MCP 工具、Sandbox、Fact 审核，以及多轮调度和恢复。真实任务的完成情况仍以具体运行结果和独立验证为准。

## 工作方式

- **OuterLLM**：规划和调整 Intent，复盘探索进展，审核结论与 Intent 结束申请。
- **InnerLLM**：围绕当前 Intent 选择下一步，调用工具，整理中间结果并提出结论候选。
- **MCP 与 Sandbox**：提供工具目录和受控执行能力，工具结果回传当前 Intent，供后续决策使用。
- **状态与证据**：使用 SQLite 和文件记录保存运行、操作、观测及审核关系，支持查询、回放和恢复。

一个 TaskRun 可以包含多个 Turn，每个 Turn 可以包含多个 Intent。工具调用成功代表获得了一次执行结果，不会自动生成 verified Fact；正式结论需要提供证据并经过审核。工作记录和摘要可以辅助推理，但不能替代正式证据。

Intent 可以完成、合法收束或技术挂起，任务随后继续其他方向。技术挂起会保留进展，并按相邻兄弟失败后的规则获得一次回访机会；业务上的失败或无法判定不会按技术错误自动回访。根方向用尽后最多重新规划一次，仍无方向则等待原 deadline。

正常自动调度以任务通过独立验证或总时限到达为结束边界，也支持人工取消。操作结果不明或共享状态需要核验时会等待恢复，不自动重放工具。

## 环境要求

- Python 3.11–3.13。
- uv，用于安装项目与管理依赖。
- 真实模式需要可用的模型 API、Docker Engine，以及当前执行配置指定的 Sandbox 镜像。
- 访问远程目标时，还需要准备目标转发镜像和相应授权。

主要开发环境为 Windows / PowerShell。模拟模式使用确定性模型和本地 Sandbox 替身，适合检查界面与基本流程，不需要调用真实模型。

## 安装与启动

在项目根目录安装依赖：

```powershell
uv sync --frozen
```

`pyproject.toml` 声明项目依赖，`uv.lock` 锁定具体版本，两者都应保留。

启动工作台：

```powershell
.\start-workbench.cmd
```

也可以通过命令启动：

```powershell
uv run qinyuan-workbench --port 4174 --data-dir workbench_data
```

浏览器打开 <http://127.0.0.1:4174/>。服务绑定本机回环地址，运行期间保持终端开启。

当前启动器默认使用公共协议 v3、执行配置 `d8_v22`。创建运行时需要明确选择模拟或真实模式；已有运行继续使用其保存的配置。

## 配置真实运行

如果还没有 `.env`，从示例创建一份：

```powershell
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

填写与当前部署一致的模型配置：

| 配置项 | 用途 |
|---|---|
| `QINYUAN_LLM_BACKEND` | 当前真实链路使用 `openai` 兼容传输 |
| `QINYUAN_MODEL` | 当前部署实际可用的 DeepSeek 模型名称 |
| `OPENAI_API_KEY` | 模型平台密钥 |
| `QINYUAN_SEARCH_API_KEY` | 可选，DeepSeek官方Search/Fetch独立凭据；不随主模型切换 |
| `OPENAI_BASE_URL` | 必须与当前适配器冻结的传输地址一致 |
| `QINYUAN_LLM_TIMEOUT_SECONDS` | 单次总时限，默认 `600` 秒，受 TaskRun 剩余时间约束 |
| `QINYUAN_LLM_CONNECT_TIMEOUT_SECONDS` | 模型建连，默认 `10` 秒 |
| `QINYUAN_LLM_FIRST_OUTPUT_TIMEOUT_SECONDS` | 连接后首次非空推理／正文增量，默认 `180` 秒 |
| `QINYUAN_LLM_IDLE_TIMEOUT_SECONDS` | 开始生成后无新增推理／正文，默认 `60` 秒 |

当前 D8 模型使用 SSE 流式接收，只在完整响应通过原校验后采用决策。心跳不延长生成等待；thinking=enabled、reasoning_effort=high 保留，不新增默认 max_tokens。省略总时限配置才采用600秒；已有 `.env` 显式180仍为180，需要你调整后用于新运行。实际超时策略在运行内冻结，恢复时改变配置会明确拒绝。诊断保留计数和时间，不保存完整思考；上游未提供 usage 时保持未知。细节见 [上限索引](restrict/explicit_limits_index.md)。

真实运行统一按已核实的模型与端点匹配容量，不再按内部契约版本切换：官方Flash／Pro及已确认的官方Flash别名采用1M上下文、384K输出保守预留，SCNET保留其已确认映射；未知模型／端点明确报配置错误，已移除旧容量策略及兼容回退，旧容量声明不再接受且历史文件不改写。不把预留发送为max_tokens。输入仍按完整序列化字节作保守估算，不冒充精确token。完整请求不足会自动尝试一次有界历史整理，保留原证据和近期对话；局部阻塞且其他分支通过准入探测时技术挂起，任务级阻塞则保留进展并等待有效变化或原deadline。`status`用`capacity_block`说明原因，不增加人工扩容流程。

新建真实d8_v22运行可用`web.search`／`web.fetch`自主检索公共资料。独立搜索Key可放在同一env文件或进程环境；未配置时，仅主模型为DeepSeek官方端点才复用其Key，无可用凭据不展示工具。检索与主模型分开配置，原文取回受公共地址准入，结果不自动成为Fact或目标观测。长正文先返回明确预览，可按details_ref用现有历史详情接口分页读取，不必再次Fetch。真实账号和检索效果需另验。检索选择按未确认的公开知识缺口及其对下一决策的价值判断，也可选择有判别力的现场验证；不会按版本号、次数或“没搜索”强制触发。

真实双角色链路目前使用 DeepSeek 专用适配器，内外层共用配置的模型名称。`openai` 这个传输名称不代表已经完成 GPT 或 Claude 接入，示例文件中的其他供应商注释也不代表运行支持。

模型参数、传输地址和容量策略见 [deepseek_roles.py](agent_adapters/deepseek_roles.py)。自定义平台需要同步匹配这些配置，并显式设置 `QINYUAN_ALLOW_CUSTOM_ENDPOINT=1`；仅修改 `.env` 中的地址不能完成平台适配。密钥保存在本地 `.env`，不要写进题面、任务 JSON 或提交到 Git。

真实运行使用的 Sandbox 镜像由 [docker_profile.py](agent_adapters/docker_profile.py) 固定，当前镜像标签为 `qinyuan/pentest-sandbox:0.3.4`，实际按配置中的镜像 ID 连接。重建同名镜像后需要核对 ID，Agent 不会自动拉取镜像。远程目标使用的转发镜像为 `qinyuan/t6-target-forwarder:0.1.0`。

新建真实运行时，会在 TaskRun 开始计时前最多等待 Docker 引擎就绪 10 秒；已就绪立即继续。该等待不会启动 Docker Desktop，恢复、取消和清理也不会重复增加这段等待。

## 使用工作台

1. 新建题目，填写名称、题面、总时限和单步时限。
2. 按需添加附件或远程目标，并选择允许的 HTTP 方法。
3. 保存后进入详情页，选择运行模式，点击“开始 Agent”。
4. 查看 Turn / Intent 关系、执行记录、Fact、模型诊断和事件。
5. 需要时取消运行，或根据页面提示核查、恢复和清理资源。

模拟模式适合检查本地流程；包含远程 HTTP 目标的任务需要使用真实模式。工具可用性取决于 MCP 注册、Sandbox 环境和本次授权。

候选答案会单独保存，只有可信独立验证接受后，TaskRun 才能认定成功。InnerLLM 和 OuterLLM 一致认可、某个 Intent 成功或获得 verified Fact，都不能单独代表整题完成。

运行数据默认保存在 `workbench_data/`。归档或永久删除前，先确认运行已结束并完成资源清理；需要排查问题时保留对应运行目录。

## 命令行

`agent.py` 是命令行入口，安装后的 `qinyuan-agent-new` 和 `qinyuan-agent` 都指向它。先查看可用命令：

```powershell
uv run qinyuan-agent-new --help
```

已有符合当前协议的任务 JSON 时，可以直接运行：

```powershell
uv run qinyuan-agent-new run-task --mode real --request request.json --env-file .env --state-dir .runs
```

常用管理命令：

```powershell
uv run qinyuan-agent-new status --task-run-id <UUID> --state-dir .runs
uv run qinyuan-agent-new replay --task-run-id <UUID> --state-dir .runs
uv run qinyuan-agent-new resume --task-run-id <UUID> --state-dir .runs
uv run qinyuan-agent-new cancel --task-run-id <UUID> --state-dir .runs
uv run qinyuan-agent-new cleanup --task-run-id <UUID> --state-dir .runs
```

这些命令需要在同一个 `--state-dir` 下定位对应运行；也可以用 `--run-dir` 指定具体目录。恢复是否能够继续执行，取决于保存的授权、配置和操作核验状态。

## MCP 工具

MCP 注册配置位于 [MCPs/Mcp_client/registry.json](MCPs/Mcp_client/registry.json)。InnerLLM 根据已注册、已授权的工具目录选择调用，执行结果和证据返回同一 Intent，可继续滚动探索，不要求预先列出完整工具链。

MCP 服务提供工具能力，可执行命令由所属 TaskRun 的 Sandbox 承载。工具执行、HTTP 观察和附件观察都需要保留来源；模型自行生成的说明不能作为观测证据。重要发现可以提出正式结论候选，交由外层审核、调整、复用或拒绝。

## 日志与排查

日志按五个通道分类：

| 通道 | 内容 |
|---|---|
| `sys` | 启动、配置和环境 |
| `ctrl` | 调度、状态转换和等待原因 |
| `model` | 模型调用、输出格式和业务校验 |
| `exec` | Sandbox、命令和 MCP 工具 |
| `store` | 数据库、证据和文件 |

排查时先确定对应的 TaskRun 和请求 ID，再查看错误类别、操作记录与证据。模型输出不符合要求、上游调用失败、状态读取失败和执行结果未知是不同的问题，不应统一当作 Intent 失败。

已知且允许纠正的模型请求保留一次共享纠正机会；未知操作不自动重放。暂停或等待不会清空已有 Fact、执行与证据。

已有运行可以直接只读查看，不需要工作台：

```powershell
python scripts/qymonitor.py runs -n 10      # 最近几轮
python scripts/qymonitor.py show latest     # 单轮总览
python scripts/qymonitor.py watch latest    # 实时盯
```

各命令的读法、指标口径与排查顺序见[运行监测手册](Plan/monitoring_guide.md)。

## 测试

日常改动优先运行相关测试。快速检查核心不变量：

```powershell
uv run pytest -m smoke
```

例如，仅检查 Docker 装配与就绪等待的离线行为：

```powershell
uv run pytest tests/test_docker.py -q
```

默认测试配置排除标记为 `live` 和 `slow` 的用例。真实模型、真实 Docker 与实际题目验收应单独安排，离线测试通过不等于真实任务已经完成。

## 目录说明

| 路径 | 内容 |
|---|---|
| `agent_core/` | 领域契约、调度、状态、审核和证据规则 |
| `agent_adapters/` | CLI、工作台、模型与执行适配 |
| `qinyuan_core/` | 跨层复用的错误和日志基础模块 |
| `MCPs/` | MCP 服务、客户端和工具注册 |
| `Sandbox/` | Docker、工具运行和目标网络隔离 |
| `frontend/` | 本地工作台页面 |
| `tests/` | 单元、集成与环境验收测试 |
| `scripts/` | 开发和诊断辅助脚本 |
| `workbench_data/` | 工作台本地数据和运行记录 |
| `.test/` | 本地测试、构建与缓存产物 |

## 开源协议

本项目以 **GNU Affero General Public License v3.0（AGPL-3.0）** 发布。许可证全文见 [LICENSE](LICENSE)。

Copyright (C) 2026 Autyui · Qinyuan Agent

你可以自由地使用、修改和分发本项目，包括部署到商业环境；条件是下面几条必须一起遵守：

- **保留版权声明**：源码、文档和分发物中现有的版权声明与许可证声明不得删除。
- **注明修改**：修改过的文件要显著标明"已修改"，并给出修改日期。
- **衍生作品同样开源**：基于本项目的整体必须以 AGPL-3.0 授权，不能只开放其中一部分，也不能附加额外限制。
- **网络服务也要给源码**：这是 AGPL 区别于 GPL 的核心。如果修改后把它作为在线服务对外提供（例如部署这个工作台），必须向使用者提供对应的完整源码（第 13 条）。

换句话说：本项目欢迎任何人拿去改、拿去用，但改完之后必须留在开源世界里，包括以 SaaS 形式使用的情形。

如果需要引用本项目或其设计，请保留仓库地址：<https://github.com/Xushu-Zhihe/Qinyuan_Agent_new>。


