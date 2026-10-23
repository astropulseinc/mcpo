import asyncio
import json
import os
import logging
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from starlette.routing import Mount

logger = logging.getLogger(__name__)


from mcpo.utils.main import get_model_fields, get_tool_handler
from mcpo.utils.auth import get_verify_api_key, APIKeyMiddleware
from mcpo.mcp_management import loggable_url, stdio_environment


async def create_dynamic_endpoints(app: FastAPI, api_dependency=None):
    session: ClientSession = app.state.session
    if not session:
        raise ValueError("Session is not initialized in the app state.")

    result = await session.initialize()
    server_info = getattr(result, "serverInfo", None)
    if server_info:
        app.title = server_info.name or app.title
        app.description = (
            f"{server_info.name} MCP Server" if server_info.name else app.description
        )
        app.version = server_info.version or app.version

    tools_result = await session.list_tools()
    tools = tools_result.tools

    for tool in tools:
        endpoint_name = tool.name
        endpoint_description = tool.description

        inputSchema = tool.inputSchema
        outputSchema = getattr(tool, "outputSchema", None)

        form_model_fields = get_model_fields(
            f"{endpoint_name}_form_model",
            inputSchema.get("properties", {}),
            inputSchema.get("required", []),
            schema_defs=inputSchema.get("$defs", {}),
        )

        response_model_fields = None
        if outputSchema:
            response_model_fields = get_model_fields(
                f"{endpoint_name}_response_model",
                outputSchema.get("properties", {}),
                outputSchema.get("required", []),
                schema_defs=outputSchema.get("$defs", {}),
            )

        tool_handler = get_tool_handler(
            endpoint_name,
            form_model_fields,
            response_model_fields,
        )

        app.post(
            f"/{endpoint_name}",
            summary=endpoint_name.replace("_", " ").title(),
            description=endpoint_description,
            response_model_exclude_none=True,
            dependencies=[Depends(api_dependency)] if api_dependency else [],
        )(tool_handler)


async def start_server_connection_background(sub_app: FastAPI):
    """Start server connection in background without blocking main app startup"""
    try:
        logger.info(f"🔄 Starting background connection for server...")
        async with sub_app.router.lifespan_context(sub_app):
            # Keep connection alive
            while True:
                await asyncio.sleep(30)  # Health check every 30 seconds
    except Exception as e:
        logger.error(f"❌ Background server connection failed: {e}")
        # Store error status for health endpoints
        sub_app.state.connection_error = str(e)
        sub_app.state.connection_status = "failed"


@asynccontextmanager
async def lifespan(app: FastAPI):
    server_type = getattr(app.state, "server_type", "stdio")
    command = getattr(app.state, "command", None)
    args = getattr(app.state, "args", [])
    env = getattr(app.state, "env", {})

    args = args if isinstance(args, list) else [args]
    api_dependency = getattr(app.state, "api_dependency", None)
    headers = getattr(app.state, "headers", None) or None

    if (server_type == "stdio" and not command) or (
        server_type == "sse" and not args[0]
    ):
        # Main app lifespan (when config_path is provided)
        # NON-BLOCKING STARTUP: Don't wait for sub-apps to connect
        logger.info("🚀 Starting main app immediately (non-blocking mode)")
        
        # Start all server connections in background
        connection_tasks = []
        for route in app.routes:
            if isinstance(route, Mount) and isinstance(route.app, FastAPI):
                # Start connection task in background instead of waiting
                task = asyncio.create_task(
                    start_server_connection_background(route.app)
                )
                connection_tasks.append(task)
        
        # Store tasks for cleanup later
        app.state.connection_tasks = connection_tasks
        
        try:
            yield  # App is ready immediately!
        finally:
            # Cleanup background tasks
            for task in connection_tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
    else:
        if server_type == "stdio":
            server_params = StdioServerParameters(
                command=command,
                args=args,
                env={**env},
            )

            async with stdio_client(server_params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    app.state.session = session
                    await create_dynamic_endpoints(app, api_dependency=api_dependency)
                    yield
        if server_type == "sse":
            from httpx import ConnectError, ConnectTimeout
            
            # Production retry strategy - retry indefinitely with backoff
            max_retry_delay = 60  # Cap retry delay at 1 minute
            retry_delay = 2
            continuous_retry_interval = 30  # 30 seconds for continuous retries
            
            # Classify errors for smart retry logic
            def is_permanent_error(error_str):
                permanent_indicators = [
                    "authentication", "unauthorized", "forbidden", "not found",
                    "invalid", "malformed", "certificate", "ssl"
                ]
                return any(indicator in error_str.lower() for indicator in permanent_indicators)
            
            # Initialize state
            app.state.sse_server_status = "connecting"
            app.state.sse_last_error = None
            app.state.session = None
            
            # Background connection task with retry logic
            async def maintain_sse_connection():
                retry_delay = 2
                attempt = 0
                
                while True:
                    attempt += 1
                    try:
                        logger.info(f"Connecting to SSE endpoint: {loggable_url(args[0])} (attempt {attempt})")
                        async with sse_client(
                            url=args[0], sse_read_timeout=None, headers=headers
                        ) as (
                            reader,
                            writer,
                        ):
                            logger.info(f"✅ Successfully connected to SSE endpoint: {loggable_url(args[0])}")
                            async with ClientSession(reader, writer) as session:
                                app.state.session = session
                                app.state.sse_server_status = "healthy"
                                
                                # Create dynamic endpoints when connection succeeds
                                await create_dynamic_endpoints(app, api_dependency=api_dependency)
                                
                                # Keep the connection alive until it fails
                                try:
                                    # Keep connection alive by periodically checking
                                    while True:
                                        await asyncio.sleep(60)  # Check every 60 seconds
                                        # Try to list tools to verify connection is alive
                                        await session.list_tools()
                                except Exception as disconnect_error:
                                    logger.warning(f"SSE connection lost for {loggable_url(args[0])}: {disconnect_error}")
                                    app.state.sse_server_status = "unhealthy"
                                    app.state.session = None
                                    
                        # Reset retry delay on successful connection
                        retry_delay = 2
                                    
                    except Exception as e:
                        last_error = str(e)
                        logger.warning(f"❌ Failed to connect to SSE endpoint {loggable_url(args[0])} (attempt {attempt}): {e}")
                        app.state.sse_last_error = last_error
                        
                        # Stop retrying for permanent errors
                        if is_permanent_error(last_error):
                            logger.error(f"🚫 Permanent error detected for {loggable_url(args[0])}. Stopping retries: {e}")
                            app.state.sse_server_status = "error"
                            return
                        
                        # Set status as unhealthy but keep retrying
                        app.state.sse_server_status = "unhealthy"
                        
                        # Exponential backoff with cap
                        current_delay = min(retry_delay, max_retry_delay)
                        logger.info(f"⏰ Retrying in {current_delay} seconds...")
                        await asyncio.sleep(current_delay)
                        
                        # Increase delay for next attempt (capped)
                        retry_delay = min(retry_delay * 1.5, max_retry_delay)
            
            # Start background connection task
            connection_task = asyncio.create_task(maintain_sse_connection())
            
            # Yield immediately to start the application
            try:
                yield
            finally:
                # Cleanup on shutdown
                if not connection_task.done():
                    connection_task.cancel()
                    try:
                        await connection_task
                    except asyncio.CancelledError:
                        pass
        if server_type == "streamablehttp" or server_type == "streamable_http":
            # Ensure URL has trailing slash to avoid redirects
            url = args[0]
            if not url.endswith("/"):
                url = f"{url}/"

            # Connect using streamablehttp_client from the SDK, similar to sse_client
            async with streamablehttp_client(url=url, headers=headers) as (
                reader,
                writer,
                _,  # get_session_id callback not needed for ClientSession
            ):
                async with ClientSession(reader, writer) as session:
                    app.state.session = session
                    await create_dynamic_endpoints(app, api_dependency=api_dependency)
                    yield


def cors_options(cors_allow_origins) -> dict:
    """CORS settings; credentials are never allowed together with a wildcard origin."""
    origins = list(cors_allow_origins or [])
    return {
        "allow_origins": origins,
        "allow_credentials": "*" not in origins,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


def build_app(
    api_key: Optional[str] = "",
    cors_allow_origins=None,
    **kwargs,
) -> FastAPI:
    """Build the mcpo app with its middleware, management APIs and MCP servers."""
    api_dependency = get_verify_api_key(api_key) if api_key else None

    # MCP Server
    server_type = kwargs.get(
        "server_type"
    )  # "stdio", "sse", or "streamablehttp" ("streamable_http" is also accepted)
    server_command = kwargs.get("server_command")

    # MCP Config
    config_path = kwargs.get("config_path")

    # mcpo server
    name = kwargs.get("name") or "MCP OpenAPI Proxy"
    description = (
        kwargs.get("description") or "Automatically generated API from MCP Tool Schemas"
    )
    version = kwargs.get("version") or "1.0"

    ssl_certfile = kwargs.get("ssl_certfile")
    ssl_keyfile = kwargs.get("ssl_keyfile")
    path_prefix = kwargs.get("path_prefix") or "/"

    main_app = FastAPI(
        title=name,
        description=description,
        version=version,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        lifespan=lifespan,
    )

    # The key guards every route, including docs, mounted servers (also those
    # added by hot reload) and the management APIs.
    if api_key:
        main_app.add_middleware(APIKeyMiddleware, api_key=api_key)

    # Added last so it runs first: preflights and 401s carry CORS headers.
    main_app.add_middleware(CORSMiddleware, **cors_options(cors_allow_origins))

    # The management APIs start processes and edit server configs, so they
    # exist only behind an API key.
    if api_key:
        try:
            logger.info("Adding Nova MCP Marketplace APIs...")
            from mcpo.mcp_management import apply_monkey_patch
            from mcpo.environment_management import apply_environment_patch
            apply_monkey_patch(main_app)
            apply_environment_patch(main_app)
            logger.info("✅ Nova MCP Marketplace APIs added successfully!")

            # Enable hot reload for zero-downtime config changes
            if config_path:
                logger.info("Enabling hot reload for zero-downtime config changes...")
                try:
                    from mcpo.hot_reload import apply_hot_reload_patch
                    apply_hot_reload_patch(main_app, config_path)
                    logger.info("✅ Zero-downtime hot reload enabled!")
                except ImportError as e:
                    logger.warning(f"Full hot reload dependencies not available ({e}), falling back to simple hot reload...")
                    from mcpo.simple_hot_reload import apply_simple_hot_reload
                    apply_simple_hot_reload(main_app, config_path)
                    logger.info("✅ Simple hot reload enabled!")
            else:
                logger.info("Note: Hot reload disabled (no config file provided)")
        except Exception as e:
            logger.warning(f"Failed to add marketplace APIs: {e}")
    else:
        logger.warning("No API key configured: the management APIs are disabled")

    if server_type == "sse":
        logger.info(
            f"Configuring for a single SSE MCP Server with URL {loggable_url(server_command[0])}"
        )
        main_app.state.server_type = "sse"
        main_app.state.args = server_command[0]  # Expects URL as the first element
        main_app.state.api_dependency = api_dependency
    elif server_type == "streamablehttp" or server_type == "streamable_http":
        logger.info(
            f"Configuring for a single StreamableHTTP MCP Server with URL {loggable_url(server_command[0])}"
        )
        main_app.state.server_type = "streamablehttp"
        main_app.state.args = server_command[0]  # Expects URL as the first element
        main_app.state.api_dependency = api_dependency
    elif server_command:  # This handles stdio
        logger.info(
            f"Configuring for a single Stdio MCP Server with command: {server_command[0]}"
        )
        main_app.state.server_type = "stdio"  # Explicitly set type
        main_app.state.command = server_command[0]
        main_app.state.args = server_command[1:]
        main_app.state.env = stdio_environment(kwargs.get("server_env"))
        main_app.state.api_dependency = api_dependency
    elif config_path:
        logger.info(f"Loading MCP server configurations from: {config_path}")
        # Hot reload copies this onto servers added at runtime.
        main_app.state.api_dependency = api_dependency
        with open(config_path, "r") as f:
            config_data = json.load(f)

        mcp_servers = config_data.get("mcpServers", {})
        if not mcp_servers:
            logger.error(f"No 'mcpServers' found in config file: {config_path}")
            raise ValueError("No 'mcpServers' found in config file.")

        logger.info("Configured MCP Servers:")
        for server_name_cfg, server_cfg_details in mcp_servers.items():
            if server_cfg_details.get("command"):
                logger.info(
                    f"  Configuring Stdio MCP Server '{server_name_cfg}' with command: {server_cfg_details['command']}"
                )
            elif server_cfg_details.get("type") == "sse" and server_cfg_details.get(
                "url"
            ):
                logger.info(
                    f"  Configuring SSE MCP Server '{server_name_cfg}' with URL: {loggable_url(server_cfg_details['url'])}"
                )
            elif (
                server_cfg_details.get("type") == "streamablehttp"
                or server_cfg_details.get("type") == "streamable_http"
            ) and server_cfg_details.get("url"):
                logger.info(
                    f"  Configuring StreamableHTTP MCP Server '{server_name_cfg}' with URL: {loggable_url(server_cfg_details['url'])}"
                )
            elif server_cfg_details.get("url"):  # Fallback for old SSE config
                logger.info(
                    f"  Configuring SSE (fallback) MCP Server '{server_name_cfg}' with URL: {loggable_url(server_cfg_details['url'])}"
                )
            else:
                logger.warning(
                    f"  Unknown configuration for MCP server: {server_name_cfg}"
                )

        main_app.description += "\n\n- **available tools**："
        for server_name, server_cfg in mcp_servers.items():
            sub_app = FastAPI(
                title=f"{server_name}",
                description=f"{server_name} MCP Server\n\n- [back to tool list](/docs)",
                version="1.0",
                lifespan=lifespan,
            )

            if server_cfg.get("command"):
                # stdio
                sub_app.state.server_type = "stdio"
                sub_app.state.command = server_cfg["command"]
                sub_app.state.args = server_cfg.get("args", [])
                sub_app.state.env = stdio_environment(server_cfg.get("env"))

            server_config_type = server_cfg.get("type")
            if server_config_type == "sse" and server_cfg.get("url"):
                sub_app.state.server_type = "sse"
                sub_app.state.args = server_cfg["url"]
            elif (
                server_config_type == "streamablehttp"
                or server_config_type == "streamable_http"
            ) and server_cfg.get("url"):
                # Store the URL with trailing slash to avoid redirects
                url = server_cfg["url"]
                if not url.endswith("/"):
                    url = f"{url}/"
                sub_app.state.server_type = "streamablehttp"
                sub_app.state.args = url
            elif not server_config_type and server_cfg.get(
                "url"
            ):  # Fallback for old SSE config
                sub_app.state.server_type = "sse"
                sub_app.state.args = server_cfg["url"]

            sub_app.state.headers = server_cfg.get("headers")
            sub_app.state.api_dependency = api_dependency

            main_app.mount(f"{path_prefix}{server_name}", sub_app)
            main_app.description += f"\n    - [{server_name}](/{server_name}/docs)"
    else:
        logger.error("MCPO server_command or config_path must be provided.")
        raise ValueError("You must provide either server_command or config.")

    return main_app



async def run(
    host: str = "127.0.0.1",
    port: int = 8000,
    api_key: Optional[str] = "",
    cors_allow_origins=["*"],
    **kwargs,
):
    name = kwargs.get("name") or "MCP OpenAPI Proxy"
    version = kwargs.get("version") or "1.0"
    description = (
        kwargs.get("description") or "Automatically generated API from MCP Tool Schemas"
    )
    ssl_certfile = kwargs.get("ssl_certfile")
    ssl_keyfile = kwargs.get("ssl_keyfile")
    path_prefix = kwargs.get("path_prefix") or "/"

    # Configure basic logging
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    logger.info("Starting MCPO Server...")
    logger.info(f"  Name: {name}")
    logger.info(f"  Version: {version}")
    logger.info(f"  Description: {description}")
    logger.info(f"  Hostname: {socket.gethostname()}")
    logger.info(f"  Port: {port}")
    logger.info(f"  API Key: {'Provided' if api_key else 'Not Provided'}")
    logger.info(f"  CORS Allowed Origins: {cors_allow_origins}")
    if ssl_certfile:
        logger.info(f"  SSL Certificate File: {ssl_certfile}")
    if ssl_keyfile:
        logger.info(f"  SSL Key File: {ssl_keyfile}")
    logger.info(f"  Path Prefix: {path_prefix}")

    main_app = build_app(
        api_key=api_key, cors_allow_origins=cors_allow_origins, **kwargs
    )

    logger.info("Uvicorn server starting...")
    config = uvicorn.Config(
        app=main_app,
        host=host,
        port=port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        log_level="info",
    )
    server = uvicorn.Server(config)

    await server.serve()
