# Nuclei MCP
适合黑板架构Agent 的 Nuclei 执行器

## 作用
本 MCP 在架构中只充当 Nuclei 执行器，负责调用 Nuclei、查询模板、校验模板、解析扫描结果。

本 MCP 不负责：
- 下一步扫描策略
- 多工具编排
- 漏洞利用决策
- 黑板状态管理


## 环境要求
- Python 3.11+
- uv
- nuclei
- nuclei-templates

## 环境变量
- `NUCLEI_COMMAND`
  - Nuclei 命令名或可执行文件路径，默认使用 `nuclei`。
- `NUCLEI_TEMPLATES_DIR`
  - nuclei-templates 模板目录路径。

## 启动方式
uv run server.py

## MCP 配置示例
mcp_server.json

## 工具列表
**check_nuclei_installation**
- 检查 MCP 运行环境里有没有 Nuclei，能不能执行，返回版本、路径、是否可用。

**run_scan**
- 按指定模板路径扫描目标。

**validate_template**
- 校验某个 Nuclei 模板是否合法。

**list_templates**
- 从模板目录里按关键词搜索模板文件，返回模板路径列表。

**read_template_info**
- 读取模板轻量元信息，给上层 planner 选择模板用。

**run_scan_by_tags**
- 按 Nuclei tags 和 severity 扫描目标。

**find_template_by_id**
- 根据模板 ID 找到本地模板路径和轻量信息。

**run_scan_by_template_id**
- 根据模板 ID 扫描目标。
