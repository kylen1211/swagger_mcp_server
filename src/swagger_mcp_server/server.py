#!/usr/bin/env python3
"""
Swagger MCP Server - 懒加载模式的 OpenAPI MCP 服务器

特性:
- 按需加载: 启动时只解析元数据, 不为每个接口注册 Tool
- 热加载配置: Token 等配置修改后立即生效
- 多数据源: 支持配置多个 API 源 (h5, admin 等)
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml
from mcp.server.fastmcp import FastMCP

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("swagger_mcp_server")


# 配置文件路径 (按优先级)
def get_config_paths() -> list[Path]:
    """获取配置文件搜索路径"""
    paths = []

    # 1. 环境变量指定
    env_path = os.environ.get("SWAGGER_MCP_CONFIG", "")
    if env_path:
        paths.append(Path(env_path))

    # 2. 项目目录 (通过 __file__ 定位)
    project_dir = Path(__file__).parent.parent.parent
    paths.append(project_dir / "config.json")

    # 3. 用户配置目录
    paths.append(Path.home() / ".config" / "swagger-mcp" / "config.json")

    # 4. 当前工作目录
    paths.append(Path.cwd() / "config.json")

    return paths


# 全局状态
_api_specs: dict[str, dict[str, Any]] = {}  # source -> OpenAPI spec
_api_index: dict[
    str, dict[str, dict[str, Any]]
] = {}  # source -> operationId -> endpoint info
_config_path: Path | None = None


def find_config_file() -> Path | None:
    """查找配置文件"""
    for path in get_config_paths():
        if path and path.exists() and path.is_file():
            logger.debug(f"Found config file: {path}")
            return path
    return None


def load_config() -> dict[str, Any]:
    """加载配置文件 (每次调用时重新读取, 支持热加载)"""
    global _config_path

    if _config_path is None:
        _config_path = find_config_file()

    if _config_path is None or not _config_path.exists():
        logger.warning("Config file not found, using empty config")
        return {"sources": {}}

    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            logger.debug(f"Loaded config from {_config_path}")
            return config
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {"sources": {}}


def get_token(source: str) -> str | None:
    """获取指定数据源的 Token (热加载)"""
    config = load_config()
    source_config = config.get("sources", {}).get(source, {})
    return source_config.get("token")


def get_base_url(source: str) -> str | None:
    """获取指定数据源的 Base URL"""
    config = load_config()
    source_config = config.get("sources", {}).get(source, {})
    return source_config.get("baseUrl")


def get_default_headers(source: str) -> dict[str, str]:
    """获取默认请求头"""
    config = load_config()
    # 全局默认头
    headers = dict(config.get("defaultHeaders", {}))
    # 数据源特定头
    source_config = config.get("sources", {}).get(source, {})
    headers.update(source_config.get("headers", {}))
    return headers


async def fetch_openapi_spec(url: str) -> dict[str, Any]:
    """获取 OpenAPI 文档"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "yaml" in content_type or url.endswith((".yaml", ".yml")):
            return yaml.safe_load(response.text)
        else:
            return response.json()


def build_api_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从 OpenAPI 文档构建接口索引"""
    index = {}
    paths = spec.get("paths", {})

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        for method in ["get", "post", "put", "delete", "patch", "head", "options"]:
            if method not in path_item:
                continue

            operation = path_item[method]
            if not isinstance(operation, dict):
                continue

            # 生成 operationId
            operation_id = operation.get("operationId")
            if not operation_id:
                # 自动生成: get_/v1/user/login -> get_v1_user_login
                operation_id = (
                    f"{method}_{path}".replace("/", "_")
                    .replace("{", "")
                    .replace("}", "")
                    .strip("_")
                )

            # 提取标签
            tags = operation.get("tags", ["default"])

            # 提取参数信息
            parameters = operation.get("parameters", [])
            request_body = operation.get("requestBody")

            index[operation_id] = {
                "operationId": operation_id,
                "method": method.upper(),
                "path": path,
                "summary": operation.get("summary", ""),
                "description": operation.get("description", ""),
                "tags": tags,
                "parameters": parameters,
                "requestBody": request_body,
                "responses": operation.get("responses", {}),
                "deprecated": operation.get("deprecated", False),
            }

    return index


async def init_sources():
    """初始化所有数据源"""
    config = load_config()
    sources = config.get("sources", {})

    for source_name, source_config in sources.items():
        url = source_config.get("url")
        if not url:
            logger.warning(f"Source '{source_name}' has no URL, skipping")
            continue

        try:
            logger.info(f"Loading OpenAPI spec for '{source_name}' from {url}")
            spec = await fetch_openapi_spec(url)
            _api_specs[source_name] = spec
            _api_index[source_name] = build_api_index(spec)

            api_count = len(_api_index[source_name])
            logger.info(f"Loaded {api_count} APIs from '{source_name}'")
        except Exception as e:
            logger.error(f"Failed to load spec for '{source_name}': {e}")


def resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """解析 $ref 引用"""
    if not ref.startswith("#/"):
        return {}

    parts = ref[2:].split("/")
    current = spec
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return {}
    return current if isinstance(current, dict) else {}


def extract_schema_info(
    spec: dict[str, Any], schema: dict[str, Any], depth: int = 0
) -> dict[str, Any]:
    """提取 schema 信息, 解析 $ref"""
    if depth > 5:  # 防止无限递归
        return {"type": "object", "note": "max depth reached"}

    if "$ref" in schema:
        resolved = resolve_ref(spec, schema["$ref"])
        return extract_schema_info(spec, resolved, depth + 1)

    result = {}

    if "type" in schema:
        result["type"] = schema["type"]

    if "description" in schema:
        result["description"] = schema["description"]

    if "enum" in schema:
        result["enum"] = schema["enum"]

    if "properties" in schema:
        result["properties"] = {}
        required = set(schema.get("required", []))
        for prop_name, prop_schema in schema["properties"].items():
            prop_info = extract_schema_info(spec, prop_schema, depth + 1)
            prop_info["required"] = prop_name in required
            result["properties"][prop_name] = prop_info

    if "items" in schema:
        result["items"] = extract_schema_info(spec, schema["items"], depth + 1)

    return result


# 创建 MCP 服务器
mcp = FastMCP("swagger-mcp-server")


@mcp.tool()
async def list_sources() -> str:
    """
    列出所有已配置的 API 数据源

    Returns:
        已配置的数据源列表及其状态
    """
    if not _api_index:
        await init_sources()

    result = []
    for source_name, index in _api_index.items():
        result.append(
            {
                "source": source_name,
                "apiCount": len(index),
                "hasToken": get_token(source_name) is not None,
            }
        )

    if not result:
        return json.dumps(
            {"error": "No sources configured. Please add sources to config.json"},
            ensure_ascii=False,
        )

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_apis(
    source: str,
    tag: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> str:
    """
    列出指定数据源的 API 接口

    Args:
        source: 数据源名称 (如 h5, admin)
        tag: 按标签过滤 (可选)
        keyword: 按关键词搜索路径/描述 (可选)
        limit: 返回数量限制, 默认 50

    Returns:
        匹配的 API 列表
    """
    if not _api_index:
        await init_sources()

    if source not in _api_index:
        available = list(_api_index.keys())
        return json.dumps(
            {
                "error": f"Source '{source}' not found",
                "available": available,
            },
            ensure_ascii=False,
        )

    index = _api_index[source]
    results = []

    for op_id, info in index.items():
        # 标签过滤
        if tag and tag.lower() not in [t.lower() for t in info["tags"]]:
            continue

        # 关键词搜索
        if keyword:
            keyword_lower = keyword.lower()
            searchable = f"{info['path']} {info['summary']} {info['description']} {op_id}".lower()
            if keyword_lower not in searchable:
                continue

        results.append(
            {
                "operationId": op_id,
                "method": info["method"],
                "path": info["path"],
                "summary": info["summary"],
                "tags": info["tags"],
                "deprecated": info["deprecated"],
            }
        )

        if len(results) >= limit:
            break

    return json.dumps(
        {
            "source": source,
            "total": len(results),
            "apis": results,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def get_api_detail(source: str, operation_id: str) -> str:
    """
    获取指定 API 的详细信息

    Args:
        source: 数据源名称
        operation_id: 接口的 operationId

    Returns:
        接口的完整定义, 包括参数、请求体、响应
    """
    if not _api_index:
        await init_sources()

    if source not in _api_index:
        return json.dumps({"error": f"Source '{source}' not found"}, ensure_ascii=False)

    index = _api_index[source]
    spec = _api_specs.get(source, {})

    if operation_id not in index:
        # 尝试模糊匹配
        matches = [op for op in index.keys() if operation_id.lower() in op.lower()]
        if matches:
            return json.dumps(
                {
                    "error": f"Operation '{operation_id}' not found",
                    "suggestions": matches[:10],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"error": f"Operation '{operation_id}' not found"}, ensure_ascii=False
        )

    info = index[operation_id].copy()

    # 解析参数 schema
    if info.get("parameters"):
        for param in info["parameters"]:
            if "schema" in param:
                param["schema"] = extract_schema_info(spec, param["schema"])

    # 解析请求体 schema
    if info.get("requestBody"):
        content = info["requestBody"].get("content", {})
        for media_type, media_info in content.items():
            if "schema" in media_info:
                media_info["schema"] = extract_schema_info(spec, media_info["schema"])

    # 解析响应 schema
    for status_code, response in info.get("responses", {}).items():
        if isinstance(response, dict):
            content = response.get("content", {})
            for media_type, media_info in content.items():
                if isinstance(media_info, dict) and "schema" in media_info:
                    media_info["schema"] = extract_schema_info(
                        spec, media_info["schema"]
                    )

    return json.dumps(info, ensure_ascii=False, indent=2)


@mcp.tool()
async def call_api(
    source: str,
    method: str,
    path: str,
    path_params: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """
    调用指定的 API 接口

    Args:
        source: 数据源名称
        method: HTTP 方法 (GET, POST, PUT, DELETE, PATCH)
        path: API 路径 (如 /v1/user/login)
        path_params: 路径参数, 用于替换 {id} 等占位符 (可选)
        query_params: URL 查询参数 (可选)
        body: 请求体 JSON (可选)
        headers: 额外的请求头 (可选)

    Returns:
        API 响应
    """
    if not _api_index:
        await init_sources()

    # 获取配置
    base_url = get_base_url(source)
    if not base_url:
        return json.dumps(
            {"error": f"No baseUrl configured for source '{source}'"},
            ensure_ascii=False,
        )

    # 构建请求头
    req_headers = get_default_headers(source)

    # 添加 Token
    token = get_token(source)
    if token:
        req_headers["Authorization"] = f"Bearer {token}"

    # 添加自定义头
    if headers:
        req_headers.update(headers)

    # 处理路径参数
    actual_path = path
    if path_params:
        for key, value in path_params.items():
            actual_path = actual_path.replace(f"{{{key}}}", str(value))

    # 构建完整 URL
    url = urljoin(base_url.rstrip("/") + "/", actual_path.lstrip("/"))

    # 发送请求
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=method.upper(),
                url=url,
                params=query_params,
                json=body if body else None,
                headers=req_headers,
            )

            # 尝试解析 JSON 响应
            try:
                response_data = response.json()
            except Exception:
                response_data = response.text

            result = {
                "status": response.status_code,
                "headers": dict(response.headers),
                "data": response_data,
            }

            return json.dumps(result, ensure_ascii=False, indent=2)

    except httpx.TimeoutException:
        return json.dumps({"error": "Request timeout"}, ensure_ascii=False)
    except httpx.RequestError as e:
        return json.dumps({"error": f"Request failed: {str(e)}"}, ensure_ascii=False)


@mcp.tool()
async def reload_sources() -> str:
    """
    重新加载所有 API 数据源

    当 API 文档有更新时调用此方法刷新

    Returns:
        重新加载的结果
    """
    global _api_specs, _api_index
    _api_specs = {}
    _api_index = {}

    await init_sources()

    result = []
    for source_name, index in _api_index.items():
        result.append(
            {
                "source": source_name,
                "apiCount": len(index),
                "status": "loaded",
            }
        )

    return json.dumps(result, ensure_ascii=False, indent=2)


def main():
    """入口函数"""
    # 懒加载: 不在启动时加载 API 文档, 第一次调用时才加载
    # 这样可以加快 opencode 启动速度
    mcp.run()


if __name__ == "__main__":
    main()
