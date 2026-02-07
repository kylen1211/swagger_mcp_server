"""
Swagger MCP Server - HTTP/SSE Mode

Remote mode entry point for deployment on Linux servers.

Usage:
    swagger_mcp_server_http [--host HOST] [--port PORT]
"""

import argparse
import logging
import os
import sys

from .server import mcp, init_sources, _api_index, logger

try:
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from starlette.responses import JSONResponse
    import uvicorn
except ImportError:
    print("Error: HTTP mode requires additional dependencies.")
    print("Install with: uv add starlette uvicorn sse-starlette")
    sys.exit(1)


def create_http_app():
    """Create Starlette app with MCP SSE transport."""

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp._mcp_server.run(
                streams[0], streams[1], mcp._mcp_server.create_initialization_options()
            )

    async def handle_messages(request):
        await sse.handle_post_message(request.scope, request.receive, request._send)

    async def handle_health(request):
        """Health check endpoint."""
        return JSONResponse(
            {
                "status": "ok",
                "sources": list(_api_index.keys()),
                "api_count": sum(len(idx) for idx in _api_index.values()),
            }
        )

    async def handle_root(request):
        """Info endpoint."""
        return JSONResponse(
            {
                "name": "swagger_mcp_server",
                "mode": "http/sse",
                "endpoints": {
                    "sse": "/sse",
                    "messages": "/messages/",
                    "health": "/health",
                },
            }
        )

    async def on_startup():
        logger.info("=" * 60)
        logger.info("[STARTUP] Swagger MCP Server (HTTP mode) starting...")
        logger.info("=" * 60)

        try:
            await init_sources()
            logger.info(f"[STARTUP] Loaded sources: {list(_api_index.keys())}")
            logger.info(
                f"[STARTUP] Total APIs: {sum(len(idx) for idx in _api_index.values())}"
            )
        except Exception as e:
            logger.warning(f"[STARTUP] Failed to preload sources: {e}")

        logger.info("[STARTUP] HTTP server ready")

    routes = [
        Route("/", handle_root),
        Route("/health", handle_health),
        Route("/sse", handle_sse),
        Mount("/messages", routes=[Route("/", handle_messages, methods=["POST"])]),
    ]

    return Starlette(
        routes=routes,
        on_startup=[on_startup],
    )


def main():
    """HTTP mode entry point."""
    parser = argparse.ArgumentParser(description="Swagger MCP Server (HTTP mode)")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8766, help="Bind port (default: 8766)"
    )

    args = parser.parse_args()

    http_app = create_http_app()

    print(f"Starting Swagger MCP Server (HTTP mode)")
    print(f"  URL: http://{args.host}:{args.port}")
    print(f"  SSE: http://{args.host}:{args.port}/sse")
    print(f"  Health: http://{args.host}:{args.port}/health")
    print()

    uvicorn.run(
        http_app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
