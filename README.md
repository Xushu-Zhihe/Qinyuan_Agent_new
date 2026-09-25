# Qinyuan_Agent_new

T5 提供同步的单 Intent 文件类型识别 CLI，支持真实 DeepSeek API、真实 Docker、本机 File MCP、SQLite 事件和证据验证。它不负责自主解题、寻找 flag 或平台提交；完整单题调度属于 T6。

最新复核（2026-09-25，用户提供）：T5 历史真实效果复核通过；本次完整独立复验受环境权限阻塞，不能宣告全部通过。`468 passed、1 skipped` 为历史记录，本次 T5 专项为 `12 passed、4 failed、18 errors`，具体边界见 [TODO.md](TODO.md)。

T6 继续以 CLI 验收，并补充手工测试步骤；前端单列在 T6 闭环稳定之后，具体界面与交互届时逐项确认，T7 继续延期。

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

## 模拟检查与测试

模拟模式必须显式提供单独夹具，如 `{"stdout":"ASCII text\n"}`，通过 `--mode simulated --fixture <JSON文件>` 使用；仅验证控制和证据规则，不证明真实文件类型。

```powershell
. ./scripts/test-env.ps1
uv run --offline --no-project --with pytest --with docker python -m pytest -q
uv build --offline --out-dir .test/dist
```

临时测试、构建、缓存统一放在 `.test/`。正式 `--state-dir` 不自动删除。验收记录与边界见 [TODO.md](TODO.md)。
