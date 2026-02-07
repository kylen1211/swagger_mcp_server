# Swagger MCP Server

懒加载模式的 OpenAPI/Swagger MCP 服务器。

## 特性

- **按需加载**: 启动时只解析 API 元数据, 不为每个接口注册 Tool
- **热加载配置**: Token 等配置修改后立即生效, 无需重启
- **多数据源**: 支持配置多个 API 源 (h5, admin 等)

## 安装运行

使用 uvx 运行 (推荐):

```bash
uvx --from /Users/good/git/project/swagger_mcp_server swagger_mcp_server
```

## 配置

配置文件查找顺序:
1. 环境变量 `SWAGGER_MCP_CONFIG` 指定的路径
2. 当前目录 `./config.json`
3. `~/.config/swagger-mcp/config.json`

### 配置文件格式

```json
{
  "sources": {
    "h5": {
      "url": "https://example.com/api/v3/api-docs",
      "baseUrl": "https://example.com/api",
      "token": "your-bearer-token",
      "headers": {
        "Accept-Language": "zh-CN"
      }
    },
    "admin": {
      "url": "https://admin.example.com/api/v3/api-docs",
      "baseUrl": "https://admin.example.com/api",
      "token": ""
    }
  },
  "defaultHeaders": {
    "Content-Type": "application/json"
  }
}
```

## MCP Tools

| Tool | 说明 |
|------|------|
| `list_sources` | 列出所有已配置的数据源 |
| `list_apis` | 列出指定数据源的 API (支持按 tag/keyword 过滤) |
| `get_api_detail` | 获取 API 详情 (参数、请求体、响应) |
| `call_api` | 调用 API 接口 |
| `reload_sources` | 重新加载 API 文档 |

## 使用示例

```
1. list_sources()
   -> 查看有哪些数据源

2. list_apis(source="h5", keyword="login")
   -> 搜索登录相关接口

3. get_api_detail(source="h5", operation_id="userLogin")
   -> 查看接口详情

4. call_api(source="h5", method="POST", path="/v1/user/login", body={"account": "xxx", "password": "xxx"})
   -> 调用接口
```

## Token 热加载

修改 config.json 中的 token 字段后, 下次调用 call_api 会自动使用新 token, 无需重启服务。
