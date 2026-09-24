# Qinyuan_Agent 全局约束与技术决策

## 全局约束

1. 具体功能、字段、状态、算法和错误语义未确认前，不自行补全。
2. Agent 在本仓库内拥有 TaskRun 内的唯一调度权，包括内部重试、状态机和业务判断。后续独立转换器负责多题目获取、调用 Agent 和平台 flag 提交，不复制 Agent 内部控制逻辑；批次调度、并发和重试规则另行确认。
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
21. T4 使用 Fake Model、真实本机 File MCP、Fake Docker 和真实持久化验收。Fake Docker 输出来自夹具，仅证明程序按证据规则接受或拒绝候选，不证明真实文件类型。文件类型候选固定为 path、observed_type、execution_id；候选及工具输出必须逐字匹配，不做类型归纳。单步骤 Plan、每 Intent 预算、失败动作和 schema 6 恢复边界均已确认，详见 `T4_proposal.md`。
22. T4 的 Intent 目标由可信调用方提供；上层先持久化 Intent.running，再由 Controller 在首次 Plan 生成前核对归属、运行状态、任务范围和当前授权并原子绑定，未绑定时 T4 拒绝启动。目标随 TaskRun 保留到既定清理，Intent 结束不删除；重试/恢复核对同一目标，模型不能生成或修改目标。目标使用独立一对一 `intent_goals` 控制面表和保留旧数据的 schema 2 迁移；导入、绑定、Fact 证明、只读 Intent 评估及执行循环已实现；验证记录见 TODO。
23. T4 七项绑定规则已确认：可信附件导入记录必须明确本地来源与 Sandbox 精确路径，URL 不支持首期 `file_type`；授权不进入绑定指纹，同 mutation 精确重放、新 mutation 同目标重验当前收据和授权，终态只接受旧 mutation 重放；返回专用三态 `IntentGoalBindingResult`，采用六个稳定错误码，首期仅 `file_type`。首次事件保存原 import_id，目标表不保存授权快照或凭据，恢复后每次新工具派发重验当前授权，不足则停止继续并保留目标。现有每次工具请求收据记录当次请求/授权/Sandbox ID，仅用于一致性和审计，不延长授权有效期。
24. T4 本地附件导入的目标路径由可信调用方明确指定，不从来源文件名推断；只接受 TaskRequest 中索引对应的本地附件，来源必须位于启动时规范绝对根目录内且为非符号链接普通文件，不得覆盖 Sandbox 目标。TaskRun `source_root` 于 schema 3 作为不可变字段随启动事务持久化；NULL 的旧任务或非 T4 调用不得导入。单文件最多 16 MiB，读取、写入和读回共用最多 60 秒且受 TaskRun Deadline 限制；成功收据仅在 Sandbox 读回原大小与 SHA-256 后签发。声明落盘后不可自动重派，未知结果及取消/超时仅走显式只读恢复；同 ID 成功重放不复查当前源文件。Fake Docker 导入不证明真实 Docker 文件持久性。
25. T4 文件类型证明在独立不可覆盖、带校验和的文件中保存；只读专用端口重新核对导入映射、工具请求收据、授权快照、实际命令、Execution 与输出。schema 4 的 `Fact.verification_ref` 对旧 Fact 可空，对 T4 新 verified Fact 必填且不可变；旧 T3 `lookup()` 仍返回 `target_ref=None`。专用提交操作一次事务写入 Fact、事件和 Checkpoint；验证失败不创建 Fact，不写控制面。
26. T4 的 Intent 评估只读：已提交可复核 verified Fact 支持当前目标时为 satisfied；没有支持 Fact 且当前 Intent 有未决或结果未知 Execution 时为 inconclusive；其余为 not_satisfied。证明缺失/损坏或存储失败直接抛异常。Controller 仅在重新评估与调用方支持 Fact ID 完全一致后原子提交 Intent.succeeded；不得自动完成 Turn 或 TaskRun。
27. T4 模型动作只接受已确认的三种互斥结构，不允许模型给出命令、授权、状态或目标。每 Intent 的 Plan/动作模型调用分别最多 2/8 次，首期最多一次成功工具派发。schema 5 独立调用台账在调用前持久登记，完成后仅存经校验的结构化结果或错误类别；未完成调用不自动重调，模型原始提示/响应与凭据不入库。模型观察输出双流合计最多 4096 字符，裁剪标记不能代替原始工具证据。

28. T4 在执行已审计 Plan/动作前持久化 schema 6 声明，固定副作用 ID；有效历史调用缺声明或结果未知时不得猜测补派。领域状态只能经 Controller 提交，台账不推进 Checkpoint。
29. 同一动作重放与新的重复检查必须区分；独立拒绝只结束本次 run，不能把仍 running 的 Intent 当作已失败，也不能由调用方自动空转重调。
30. 模型线程迟到结果不能写 Fact 或派发工具；取消/Deadline 仍由 Controller 优先提交。真实模型必须另行验证协作取消和超时，不能因 Fake Model 通过就宣称线程可强杀。

## 技术决策

| 决策 | 选择 | 原因与边界 |
| --- | --- | --- |
| 核心语言 | Python 3.11–3.13 | 与现有项目配置一致 |
| 核心抽象 | `Protocol` + 不可变/有界领域类型 | 让核心脱离外部 SDK，防止跨层泄漏 |
| 模型边界 | 供应商无关 `ModelAdapter` | Codex CLI、Claude Code CLI、DeepSeek 后续可替换接入 |
| 持久化 | SQLite 控制面，schema version 6 | 已验证原子提交、幂等和恢复边界；原始输出由证据层管理 |
| Sandbox | TaskRun 级单一资源 | 保证多轮 Intent 共享状态并限制并发副作用 |
| 调度 | Intent 顺序尝试 + 深度优先回溯 | 与 confirmed flow 和已验证核心状态一致 |
| 结果 | Candidate 先验证，再提交 Fact | 防止原始输出被误报为事实 |
| RAG | 首期延期 | 流程、匹配阈值、候选生命周期和并行关系未确认 |
| 产品边界 | 独立自动化渗透测试 Agent | TaskRun 内部控制归 Agent，多题目获取与平台 flag 提交归后续独立转换器 |
| T5 | 独立 Agent 的 CLI 与事件 | 2026-09-24 取消 qinyuan-bat 专用转接；公共入口契约仍待确认 |
| 后续转换器 | 独立负责获取多题、调用 Agent、提交 flag | 调用方式、批次策略、平台协议和部署方式待确认，不作为 T5 验收前置条件 |

## T5 与转换器职责约束（2026-09-24）

T5 不承担题目平台取题或 flag 提交，不要求兼容 qinyuan-bat 专用协议。Agent 核心不得依赖后续转换器或平台协议；转换器通过确认后的公共入口调用，不直接改写 Agent 控制面，也不绕过 Policy、证据和生命周期边界。

CLI 输入输出、事件、同步/异步、退出码、`resume/cancel`，以及转换器的调用和提交语义，均须逐项取得用户确认后再实现。不得将当前 Python 类型直接视为已冻结的外部协议，也不得将 Agent 结果自动等同于平台接受 flag 的回执。此次职责调整不扩大已确认的工具权限或真实目标授权。

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
