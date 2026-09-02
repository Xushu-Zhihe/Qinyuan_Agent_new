# Dirsearch MCP Server

基于 [dirsearch](https://github.com/maurosoria/dirsearch) 的 MCP (Model Context Protocol) 服务器，用于目录、接口和隐藏路径扫描。

## 功能特性

- **目录扫描**: 扫描目标网站的目录和文件
- **API 接口发现**: 发现隐藏的 API 端点
- **管理面板发现**: 扫描管理后台、登录页面等
- **隐藏路径检测**: 检测备份文件、敏感路径等

## 安装

### 前置要求

1. **Python 3.10+**
2. **dirsearch**: 需要先安装 dirsearch 工具

```bash
# 安装 dirsearch (选择一种方式)

# 方式1: pip 安装
pip install dirsearch

# 方式2: 从源码安装
git clone https://github.com/maurosoria/dirsearch.git
cd dirsearch
pip install .
```

### 安装 MCP 服务器

```bash
cd Dirsearch_Mcp
pip install -e .
```

## 配置 MCP 客户端

### Claude Desktop 配置

在 Claude Desktop 的配置文件中添加:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "dirsearch": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "C:\\Users\\15131\\Desktop\\Dirsearch_Mcp"
    }
  }
}
```

### 其他 MCP 客户端

根据你的 MCP 客户端文档配置，启动命令为:

```bash
python -m src.server
```

或安装后直接运行:

```bash
dirsearch-mcp
```

## 可用工具

### 1. `dirsearch_check`

检查 dirsearch 是否可用。

**参数**: 无

**示例**:
```json
{}
```

### 2. `dirsearch_scan`

完整的目录扫描，支持所有参数自定义。

**参数**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| target_url | string | 是 | 目标 URL |
| extensions | array | 否 | 文件扩展名列表 |
| wordlist | string | 否 | 自定义字典路径 |
| exclude_status_codes | array | 否 | 排除的状态码 |
| include_status_codes | array | 否 | 仅包含的状态码 |
| threads | integer | 否 | 线程数 (默认: 10) |
| timeout | integer | 否 | 超时时间 (默认: 30) |
| recursive | boolean | 否 | 递归扫描 (默认: false) |
| recursion_depth | integer | 否 | 递归深度 (默认: 1) |
| follow_redirects | boolean | 否 | 跟随重定向 (默认: false) |
| random_user_agent | boolean | 否 | 随机 User-Agent (默认: true) |
| headers | object | 否 | 自定义 HTTP 头 |
| cookies | object | 否 | Cookie |
| proxy | string | 否 | 代理 URL |
| delay | number | 否 | 请求延迟 (秒) |
| max_time | integer | 否 | 最大扫描时间 (秒) |

**示例**:
```json
{
  "target_url": "https://example.com",
  "extensions": ["php", "html", "js"],
  "exclude_status_codes": [404],
  "threads": 20
}
```

### 3. `dirsearch_quick_scan`

快速扫描，使用优化的默认设置。

**参数**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| target_url | string | 是 | 目标 URL |
| extensions | array | 否 | 文件扩展名列表 |

**示例**:
```json
{
  "target_url": "https://example.com"
}
```

### 4. `dirsearch_api_scan`

API 接口扫描，专门用于发现 API 端点。

**参数**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| target_url | string | 是 | 目标 API 基础 URL |
| api_extensions | array | 否 | API 扩展名 (默认: json, xml, yaml) |
| headers | object | 否 | 自定义请求头 (如认证信息) |

**示例**:
```json
{
  "target_url": "https://api.example.com/v1",
  "headers": {
    "Authorization": "Bearer your-token"
  }
}
```

### 5. `dirsearch_admin_scan`

管理面板扫描，用于发现后台管理界面。

**参数**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| target_url | string | 是 | 目标 URL |
| extensions | array | 否 | 文件扩展名 |

**示例**:
```json
{
  "target_url": "https://example.com"
}
```

## 使用示例

### 在 Claude 中使用

```
请帮我扫描 https://example.com 的目录和隐藏文件
```

Claude 会自动调用 `dirsearch_quick_scan` 工具进行扫描。

### 扫描 API 端点

```
请扫描 https://api.example.com/v1 的所有 API 接口
```

### 查找管理后台

```
帮我找到 https://example.com 的管理后台入口
```

## 输出格式

所有扫描结果以 JSON 格式返回:

```json
{
  "success": true,
  "target": "https://example.com",
  "results_count": 15,
  "results": [
    {
      "url": "https://example.com/admin/",
      "status_code": 200,
      "content_length": 1234,
      "redirect_url": null
    },
    {
      "url": "https://example.com/api/users",
      "status_code": 401,
      "content_length": 89,
      "redirect_url": null
    }
  ]
}
```

## 安全提示

⚠️ **重要**: 仅在授权范围内使用此工具进行安全测试。未经授权的扫描可能违反法律法规。

- 仅扫描你有授权的目标
- 遵守目标网站的服务条款
- 使用适当的延迟避免对目标造成过大负载

## 许可证

MIT License
