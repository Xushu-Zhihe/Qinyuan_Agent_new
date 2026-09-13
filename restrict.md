# Qinyuan_Agent 全局约束与技术决策

## 全局约束

1. 具体功能、字段、状态、算法和错误语义未确认前，不自行补全。
2. Agent 在本仓库内拥有唯一调度权；`qinyuan-bat` 只能基础调用或协议转接，不承载第二套调度器。
3. 依赖方向由外向内：核心领域不依赖 CLI、SQLite、Docker、MCP SDK 或具体模型 SDK；副作用只能经显式端口进入外围。
4. 每个 TaskRun 只创建一个全局唯一 Sandbox；所有 Intent 轮复用它，结束、取消或 Deadline 后清理。不同运行不共享 Sandbox。
5. 同一 Sandbox 不并行执行多个 Intent 或多个不受协调的 LLM 工具调用。
6. Intent 按模型返回的可能性顺序逐个尝试，采用深度优先；环境故障保留重试/重新申请路径，目标任务失败才形成策略失败摘要。
7. LLM 只能提出 Intent、Plan 或工具请求，不能直接执行命令、绕过 Policy、写控制面或提交 Fact。
8. Policy 必须在任何副作用前检查工具权限、目标范围和当前任务授权。
9. 原始执行输出不能直接成为 ResultFact；必须有可追溯的 ExecutionRecord/证据，并经过范围、时效、后置条件和内容验证。
10. 只有已提交的事实和事件对恢复可见；崩溃恢复不得猜测未知副作用，不得自动重放未知的命令。
11. 控制面变更必须带 UUID4 mutation ID，并遵守幂等、陈旧写入、原子事件和 Checkpoint 规则。
12. 密钥不得进入源码、日志、Artifact、模型上下文或版本库；输出和错误必须有界并按需要脱敏。
13. 首期只实现 direct 方案，不实现 RAG 检索、retrieve_first、RAG 阈值、候选仲裁或双 LLM 并行方案。
14. T2 只进行 Fake Docker 模拟验收，真实文件复用、网络隔离和进程终止必须由后续真实 Docker 验收确认。测试仅发出固定 Python 写入/读取/等待脚本，不把 python3 可执行文件白名单当作脚本授权。
15. T2 容器使用 `network_mode=none`（保留内部回环），无其他网络、发布端口、宿主挂载或 Docker socket。`/workspace` tmpfs 只支持同一运行容器的跨 Intent 状态复用，停止后不承诺数据保留。
16. 取消/Deadline 的提交前复核或事务失败不得产生 Sandbox 副作用；终态提交且成功重载后才关闭执行准入并清理。无法确认的创建、停止和删除必须报告未知，不自动再创建、重建或重发 stop。业务重放与显式资源清理分开。
17. T3 已确认 `File_Mcp.inspect_file` 和真实本机 stdio MCP + Fake Docker 验收。只授权 `/workspace/input/` 中显式列出的普通文件；不联网、不写入目标、不接受任意脚本；路径和受控实际命令均经 Policy 检查。原始工具输出仍不自动成为 Fact。
18. T3 证据存放在调用方指定目录，双流各最多 64 KiB、显式敏感值脱敏、摘要最长 256 字；不自动过期。请求只执行一次，未知结果及不完整收据不重放，只有确定环境故障允许调用方显式重试一次。具体接口和故障映射以 T3 已确认协议节为准。
19. T4 已确认单 Intent 切片：Fact 验证、Intent 目标判断、TaskRun 目标判断分开。工具成功或 Intent 满足不得触发任务成功声明；完成确认、TaskRun 终态和 TaskResult 由上层按已有契约处理。取消/Deadline 仍由 Controller 处理。
20. T4 文件类型候选采用专用结构和确定性验证；路径需与可信请求收据、授权、保存的实际命令及 Execution 交叉核对。历史 `target_ref=None` 不直接改写成已验证目标；不得把工具识别描述未经确认地归纳成更强结论。
21. T4 使用 Fake Model、真实本机 File MCP、Fake Docker 和真实持久化验收。Fake Docker 输出来自夹具，仅证明程序按证据规则接受或拒绝候选，不证明真实文件类型。具体候选字段及循环、失败、恢复规则仍需确认。

## 技术决策

| 决策 | 选择 | 原因与边界 |
| --- | --- | --- |
| 核心语言 | Python 3.11–3.13 | 与现有项目配置一致 |
| 核心抽象 | `Protocol` + 不可变/有界领域类型 | 让核心脱离外部 SDK，防止跨层泄漏 |
| 模型边界 | 供应商无关 `ModelAdapter` | Codex CLI、Claude Code CLI、DeepSeek 后续可替换接入 |
| 持久化 | SQLite 控制面，schema version 1 | 已验证原子提交、幂等和恢复边界；原始输出由证据层管理 |
| Sandbox | TaskRun 级单一资源 | 保证多轮 Intent 共享状态并限制并发副作用 |
| 调度 | Intent 顺序尝试 + 深度优先回溯 | 与 confirmed flow 和已验证核心状态一致 |
| 结果 | Candidate 先验证，再提交 Fact | 防止原始输出被误报为事实 |
| RAG | 首期延期 | 流程、匹配阈值、候选生命周期和并行关系未确认 |
| qinyuan-bat | 可选单一转接层 | 保持 Agent 所有权，避免重复调度 |

## 当前禁止的实现扩展

- 未确认字段的万能 `dict[str, Any]` 公共接口。
- 未经用户确认的真实目标访问、网络、提权、写文件或新增 MCP 权限。
- 把 MCP/Sandbox/Docker/宿主机故障加入策略失败记忆。
- 在 RAG、Procedure 或 Stage 的提交粒度和失败分支未确认前实现对应业务逻辑。
- 通过删除断言、吞异常、关闭检查或覆盖用户未提交改动制造通过结果。

## 验收要求

每个新增切片必须有可观察的主路径、关键失败路径和证据；验证顺序为相关测试、所属模块、跨模块链路，再运行编译、静态检查、构建和 `git diff --check`。未能运行的检查及剩余风险必须在 TODO 中记录。

## 临时文件目录

用户于 2026-09-11 最终确认：**保留 `.venv/`；测试、缓存、构建及临时产物统一放入 `.test/`**，不进入版本库。包括测试 SQLite、pytest 临时目录及缓存、uv/Ruff/mypy 缓存、验证产生的 Python 字节码、临时日志、构建中间文件和试验打包产物。正式 TaskRun 数据和题目附件不属于临时文件，不迁移或清理。

PowerShell 验证会话先执行 `. ./scripts/test-env.ps1`，将当前进程及子进程的临时目录、Python 字节码和工具缓存设到 `.test/`；该脚本不修改系统或用户级环境变量。pytest、Ruff、mypy 和 uv 的目录默认值也写在 `pyproject.toml` 中；setuptools 中间文件路径写在 `setup.cfg` 中。

后续验证命令从仓库根目录运行：

```powershell
. ./scripts/test-env.ps1
uv run --no-project --with 'mcp==1.29.1' --with pytest --with docker python -m pytest -q
uv run --no-project python -m compileall -q agent_core agent_adapters Sandbox MCPs/file_tool_adapter.py MCPs/File_Mcp tests
uv build --out-dir .test/dist
```

Ruff/mypy 沿用对应切片的检查范围。额外的 `--cache-dir`、`--basetemp`、日志重定向及 `--out-dir` 必须指向 `.test/` 下的专用子目录。pytest 可清空其 `basetemp`，因此不能将 `.test/` 本身用作 `basetemp`。历史验收命令保留当时路径，重跑时采用本节新路径。
