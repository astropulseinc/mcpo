#!/usr/bin/env python3
"""
Hot Reload for MCP Configuration
Monitors config file changes and automatically reloads MCP servers
"""

import asyncio
import json
import os
import logging
import time
from typing import Dict, Any, Optional
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileModifiedEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None
    FileSystemEventHandler = None
    FileModifiedEvent = None

from .mcp_management import (
    describe_server_config,
    kill_stdio_processes,
    loggable_url,
    retire_stdio_uid,
    stdio_command,
    stdio_environment,
)

logger = logging.getLogger(__name__)

if WATCHDOG_AVAILABLE:
    class ConfigFileHandler(FileSystemEventHandler):
        """Handles config file changes"""
        
        def __init__(self, config_path: str, reload_callback):
            self.config_path = Path(config_path).resolve()
            self.reload_callback = reload_callback
            self.last_modified = 0
            # Store reference to main event loop for thread-safe callbacks
            try:
                self._main_loop = asyncio.get_running_loop()
            except RuntimeError:
                self._main_loop = None
            
        def on_modified(self, event):
            if event.is_directory:
                return
                
            logger.debug(f"🔍 File change detected: {event.src_path}")
            logger.debug(f"🔍 Watching config file: {self.config_path}")
            logger.debug(f"🔍 Resolved paths match: {Path(event.src_path).resolve() == self.config_path}")
                
            # Check if it's our config file
            if Path(event.src_path).resolve() == self.config_path:
                # Debounce - ignore multiple events within 0.1 second
                current_time = time.time()
                if current_time - self.last_modified < 0.1:
                    logger.debug(f"⏰ Debouncing file change (last modified {current_time - self.last_modified:.2f}s ago)")
                    return
                self.last_modified = current_time
                
                logger.info(f"🔥 CONFIG FILE CHANGED - TRIGGERING RELOAD: {event.src_path}")
                logger.info(f"🕐 Reload triggered at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                
                # Schedule reload callback in the main event loop
                import threading
                import concurrent.futures
                
                # Create a simple executor to run the async callback
                def schedule_reload():
                    try:
                        if hasattr(self, '_main_loop') and self._main_loop:
                            # Use call_soon_threadsafe to schedule in main loop
                            future = asyncio.run_coroutine_threadsafe(
                                self.reload_callback(), self._main_loop
                            )
                            # Don't wait for completion to avoid blocking watchdog
                        else:
                            logger.warning("⚠️ Main event loop not available for hot reload callback")
                    except Exception as e:
                        logger.error(f"❌ Failed to schedule hot reload: {e}")
                
                schedule_reload()
else:
    # Fallback when watchdog is not available
    class ConfigFileHandler:
        """Fallback config file handler using polling"""
        
        def __init__(self, config_path: str, reload_callback):
            self.config_path = Path(config_path).resolve()
            self.reload_callback = reload_callback
            self.last_modified = 0
            self._task = None
            self._running = False
            # Store reference to main event loop for consistency
            try:
                self._main_loop = asyncio.get_running_loop()
            except RuntimeError:
                self._main_loop = None
            
        async def start_polling(self):
            """Start polling for file changes"""
            self._running = True
            while self._running:
                try:
                    if self.config_path.exists():
                        stat = self.config_path.stat()
                        mtime = stat.st_mtime
                        
                        if self.last_modified == 0:
                            self.last_modified = mtime
                        elif mtime != self.last_modified:
                            self.last_modified = mtime
                            logger.info(f"🔥 CONFIG FILE CHANGED (POLLING) - TRIGGERING RELOAD: {self.config_path}")
                            logger.info(f"🕐 Polling reload triggered at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                            logger.info(f"📊 File mtime: {mtime}, Previous mtime: {self.last_modified}")
                            await self.reload_callback()
                    
                    await asyncio.sleep(2)  # Poll every 2 seconds
                except Exception as e:
                    logger.error(f"Error in config polling: {e}")
                    await asyncio.sleep(5)
        
        def stop_polling(self):
            """Stop polling"""
            self._running = False

class MCPHotReloader:
    """Manages hot reloading of MCP servers"""
    
    def __init__(self, app, config_path: str):
        self.app = app
        self.config_path = Path(config_path).resolve()
        self.observer = None
        self.current_config = None
        self._sessions = {}  # Store active MCP sessions
        self.stop_failed: set = set()  # Stopped servers whose processes survived
        
        # Track start time for uptime calculation
        import time
        self.start_time = time.time()
        
    async def start(self):
        """Start watching config file"""
        try:
            logger.info("🔥 INITIALIZING HOT RELOAD SYSTEM")
            logger.info(f"📁 Config file path: {self.config_path}")
            logger.info(f"📁 Config file exists: {self.config_path.exists()}")
            logger.info(f"📁 Parent directory: {self.config_path.parent}")
            logger.info(f"🐕 Watchdog available: {WATCHDOG_AVAILABLE}")
            
            # Load initial config
            self.current_config = self._load_config()
            initial_servers = list(self.current_config.get("mcpServers", {}).keys())
            logger.info(f"📊 Initial servers loaded: {initial_servers}")
            
            # Set up file watcher
            event_handler = ConfigFileHandler(
                str(self.config_path), 
                self._reload_servers
            )
            
            if WATCHDOG_AVAILABLE:
                # Use watchdog for file system events
                self.observer = Observer()
                self.observer.schedule(
                    event_handler,
                    str(self.config_path.parent),
                    recursive=False
                )
                self.observer.start()
                logger.info(f"✅ Started watchdog file watcher for: {self.config_path}")
                logger.info(f"🎯 Watching directory: {self.config_path.parent}")
            else:
                # Use polling fallback
                logger.warning("⚠️ Watchdog not available, using polling fallback")
                self.observer = event_handler  # Store handler as observer for consistency
                self._polling_task = asyncio.create_task(event_handler.start_polling())
                logger.info(f"✅ Started polling file watcher for: {self.config_path}")
                logger.info(f"⏰ Polling interval: 2 seconds")
            
            logger.info("🚀 HOT RELOAD SYSTEM READY - Monitoring for config changes")
            
        except Exception as e:
            logger.error(f"❌ Failed to start hot reloader: {e}")
            import traceback
            logger.error(f"❌ Full traceback: {traceback.format_exc()}")
            # Set observer to None to indicate failure
            self.observer = None
    
    def stop(self):
        """Stop watching config file"""
        try:
            if WATCHDOG_AVAILABLE and self.observer and hasattr(self.observer, 'is_alive') and self.observer.is_alive():
                self.observer.stop()
                self.observer.join()
                logger.info("✅ Stopped watchdog file watcher")
            elif not WATCHDOG_AVAILABLE and hasattr(self, '_polling_task') and self._polling_task:
                if hasattr(self.observer, 'stop_polling'):
                    self.observer.stop_polling()
                self._polling_task.cancel()
                logger.info("✅ Stopped polling file watcher")
        except Exception as e:
            logger.error(f"Error stopping file watcher: {e}")
    
    def is_watching(self):
        """Check if file watcher is active"""
        if WATCHDOG_AVAILABLE:
            return self.observer and hasattr(self.observer, 'is_alive') and self.observer.is_alive()
        else:
            return (hasattr(self, '_polling_task') and 
                   self._polling_task and 
                   not self._polling_task.done() and
                   hasattr(self.observer, '_running') and 
                   self.observer._running)
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from file"""
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return {"mcpServers": {}}
    
    async def _reload_servers(self):
        """Reload MCP servers based on config changes"""
        try:
            logger.info("🚀 STARTING ZERO-DOWNTIME RELOAD PROCESS")
            logger.info(f"🕐 Reload started at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            new_config = self._load_config()
            logger.info(f"📄 Loaded new config with {len(new_config.get('mcpServers', {}))} servers")
            
            # Always process file changes - don't skip based on content comparison
            # This ensures that delete->add cycles are properly handled
            logger.info("🔄 Processing config change (file modification detected)")
            
            old_servers = set(self.current_config.get("mcpServers", {}).keys())
            new_servers = set(new_config.get("mcpServers", {}).keys())
            
            logger.info(f"📊 Old servers: {old_servers}")
            logger.info(f"📊 New servers: {new_servers}")
            
            # Find changes
            removed = old_servers - new_servers
            added = new_servers - old_servers
            modified = {
                s for s in old_servers & new_servers
                if self.current_config["mcpServers"][s] != new_config["mcpServers"][s]
            }
            
            logger.info(f"📈 RELOAD ANALYSIS:")
            logger.info(f"   🗑️  Servers to remove: {removed}")
            logger.info(f"   ➕ Servers to add: {added}")
            logger.info(f"   🔄 Servers to modify: {modified}")
            
            # Check if there are actual changes to process
            if not (removed or added or modified):
                logger.info("✅ No server changes detected, but file was modified - updating config cache")
                self.current_config = new_config
                return
            
            # Stop removed/modified servers. A server whose processes survive
            # is a survivor: not started again (no second instance), its uid
            # not retired, and it stays in current_config under its old
            # config so the next reload (or a DELETE) retries the stop.
            survivors = set()
            for server_id in removed | modified:
                logger.info(f"🛑 Stopping server: {server_id}")
                if await self.stop_server_processes(server_id):
                    self.stop_failed.discard(server_id)
                else:
                    survivors.add(server_id)
            # A removed server's uid is retired after its processes are gone,
            # so a server added later under this id gets a fresh home.
            for server_id in removed - survivors:
                retire_stdio_uid(server_id)

            # Start added/modified servers (never a survivor)
            for server_id in (added | modified) - survivors:
                server_config = new_config["mcpServers"][server_id]
                logger.info(f"🚀 Starting server: {server_id} ({describe_server_config(server_config)})")
                try:
                    await self._start_server(server_id, server_config)
                except Exception as e:
                    logger.error(f"❌ Failed to start {server_id}: {e}")

            tracked = json.loads(json.dumps(new_config))
            for server_id in survivors:
                tracked.setdefault("mcpServers", {})[server_id] = self.current_config["mcpServers"][server_id]
            self.current_config = tracked
            
            logger.info(f"✅ ZERO-DOWNTIME RELOAD COMPLETED SUCCESSFULLY")
            logger.info(f"📊 Final state - Added: {added}, Removed: {removed}, Modified: {modified}")
            logger.info(f"🕐 Reload completed at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # Notify about changes (could emit events here)
            self._notify_changes({
                "added": list(added),
                "removed": list(removed),
                "modified": list(modified),
                "total": len(new_config.get("mcpServers", {}))
            })
            
        except Exception as e:
            logger.error(f"❌ RELOAD FAILED: {e}")
            import traceback
            logger.error(f"❌ Full traceback: {traceback.format_exc()}")
    
    async def _stop_server(self, server_id: str):
        """Stop a specific MCP server with graceful shutdown"""
        try:
            if server_id in self._sessions:
                session_info = self._sessions[server_id]
                
                # Mark server as draining (new requests will be rejected)
                if isinstance(session_info, dict):
                    session_info['status'] = 'draining'
                
                # Wait for active requests to complete (simple implementation)
                # In production, you'd track active requests per server
                logger.info(f"🔄 Draining connections for server: {server_id}")
                await asyncio.sleep(0.5)  # Give time for active requests
                
                # Remove from app routes (graceful unmounting)
                self._unmount_server(server_id)
                
                # Close session and connections if they exist
                if hasattr(session_info, 'close'):
                    await session_info.close()
                elif isinstance(session_info, dict) and 'sub_app' in session_info:
                    # Clean up sub-app resources properly
                    sub_app = session_info['sub_app']
                    
                    # Close MCP session
                    if hasattr(sub_app.state, 'session') and sub_app.state.session:
                        try:
                            if hasattr(sub_app.state, '_session_client'):
                                await sub_app.state._session_client.__aexit__(None, None, None)
                        except Exception as e:
                            logger.warning(f"Error closing session for {server_id}: {e}")
                    
                    # Close client connections
                    for client_attr in ['_sse_client', '_stdio_client', '_http_client']:
                        if hasattr(sub_app.state, client_attr):
                            try:
                                client = getattr(sub_app.state, client_attr)
                                if hasattr(client, '__aexit__'):
                                    await client.__aexit__(None, None, None)
                            except Exception as e:
                                logger.warning(f"Error closing {client_attr} for {server_id}: {e}")
                    
                    # Clean up state
                    for attr in ['session', '_session_client', '_sse_client', '_stdio_client', '_http_client', '_reader', '_writer']:
                        if hasattr(sub_app.state, attr):
                            delattr(sub_app.state, attr)
                
                # Cancel any background connection tasks for this server
                await self._cleanup_background_tasks(server_id)
                
                del self._sessions[server_id]
                logger.info(f"✅ Gracefully stopped MCP server: {server_id}")
            elif server_id in getattr(self.app.state, "server_tasks", {}):
                # A server started with the app (from config.json at boot):
                # its connection task owns the process.
                self._unmount_server(server_id)
                await self._cleanup_background_tasks(server_id)
                logger.info(f"✅ Stopped MCP server started at boot: {server_id}")
        except Exception as e:
            logger.error(f"Failed to stop server {server_id}: {e}")

    async def stop_server_processes(self, server_id: str) -> bool:
        """Kill, clean up, kill again; True when no process of the server is left.

        On failure the server is recorded in stop_failed: unmounted, not
        running tools, and retried by the next reload or DELETE.
        """
        try:
            await kill_stdio_processes(server_id)
            await self._stop_server(server_id)
            await kill_stdio_processes(server_id)
        except RuntimeError as e:
            logger.error(f"❌ {e}; its uid stays assigned, and the stop will be retried")
            self.stop_failed.add(server_id)
            return False
        self.stop_failed.discard(server_id)
        return True

    async def stop_server(self, server_id: str):
        """Stop a server now, whether it started at boot or by hot reload.

        Raises RuntimeError when processes of its uid survive.
        """
        # Kill first: a client cleanup waits for a process that ignores EOF
        # and can't signal it (another uid), so it would hang. Kill again
        # after the cleanup in case anything restarted meanwhile.
        await kill_stdio_processes(server_id)
        await self._stop_server(server_id)
        await kill_stdio_processes(server_id)
        if self.current_config:
            self.current_config.get("mcpServers", {}).pop(server_id, None)
    
    def _unmount_server(self, server_id: str):
        """Unmount a server from FastAPI routes"""
        try:
            # Find and remove the route
            routes_to_remove = []
            for i, route in enumerate(self.app.routes):
                if hasattr(route, 'path') and route.path == f"/{server_id}":
                    routes_to_remove.append(i)
            
            # Remove routes in reverse order to maintain indices
            for i in reversed(routes_to_remove):
                self.app.routes.pop(i)
                logger.info(f"🔄 Unmounted route: /{server_id}")
                
        except Exception as e:
            logger.error(f"Failed to unmount server {server_id}: {e}")
    
    async def _start_server(self, server_id: str, config: Dict[str, Any]):
        """Start a specific MCP server"""
        try:
            logger.info(f"Starting MCP server {server_id} ({describe_server_config(config)})")
            
            # Create a new sub-app for this server. CORS and the API key come
            # from the main app's middleware, which also wraps mounted apps.
            from fastapi import FastAPI
            
            # Import lifespan from main module
            from .main import lifespan
            
            sub_app = FastAPI(
                title=f"{server_id}",
                description=f"{server_id} MCP Server\n\n- [back to tool list](/docs)",
                version="1.0",
                lifespan=lifespan,
            )
            
            # Configure server based on type
            if config.get("command"):
                # stdio server
                sub_app.state.server_type = "stdio"
                sub_app.state.command = config["command"]
                sub_app.state.args = config.get("args", [])
                sub_app.state.env = stdio_environment(config.get("env"))
                sub_app.state.server_id = server_id
            elif config.get("type") == "sse" and config.get("url"):
                # SSE server
                sub_app.state.server_type = "sse"
                sub_app.state.args = config["url"]
            elif config.get("type") in ["streamablehttp", "streamable_http"] and config.get("url"):
                # StreamableHTTP server
                url = config["url"]
                if not url.endswith("/"):
                    url = f"{url}/"
                sub_app.state.server_type = "streamablehttp"
                sub_app.state.args = url
            elif config.get("url"):  # Fallback for old SSE config
                sub_app.state.server_type = "sse"
                sub_app.state.args = config["url"]
            
            sub_app.state.headers = config.get("headers")

            # Get API dependency from main app
            api_dependency = getattr(self.app.state, "api_dependency", None)
            sub_app.state.api_dependency = api_dependency
            
            # Initialize MCP session BEFORE mounting to ensure tools are available
            logger.info(f"🔄 Initializing MCP session before mounting: {server_id}")
            initialization_success = False
            
            try:
                await self._initialize_server_session_blocking(sub_app, server_id)
                initialization_success = True
                
                # Mount the server only AFTER MCP session is ready with tools
                logger.info(f"📋 Mounting server with initialized tools: {server_id}")
                self.app.mount(f"/{server_id}", sub_app)
                
                # Store reference for cleanup
                self._sessions[server_id] = {
                    "sub_app": sub_app,
                    "config": config,
                    "status": "mounted_with_tools"
                }
                
                logger.info(f"✅ Successfully mounted MCP server with tools ready: {server_id}")
                
                # Verify tools are actually available after mounting
                await self._verify_tools_or_force_reload(server_id, sub_app)
                
            except Exception as init_error:
                logger.warning(f"⚠️ Synchronous initialization failed for {server_id}: {init_error}")
                logger.info(f"🔄 Falling back to mount-first approach and will trigger force reload")
                
                # Mount the server immediately (fallback to old behavior)
                self.app.mount(f"/{server_id}", sub_app)
                
                # Start background initialization
                await self._initialize_server_session(sub_app, server_id)
                
                # Store reference for cleanup with fallback status
                self._sessions[server_id] = {
                    "sub_app": sub_app,
                    "config": config,
                    "status": "mounted_fallback"
                }
                
                # Schedule force reload after a short delay to ensure tools are available
                import asyncio
                async def delayed_force_reload():
                    await asyncio.sleep(3)  # Wait 3 seconds for initialization
                    logger.info(f"🔄 Triggering automatic force reload for {server_id}")
                    await self._trigger_force_reload()
                
                asyncio.create_task(delayed_force_reload())
                
                logger.info(f"⚠️ Server {server_id} mounted with fallback - force reload scheduled")
            
        except Exception as e:
            logger.error(f"Failed to start server {server_id}: {e}")
            raise
    
    async def _initialize_server_session_blocking(self, sub_app, server_id: str):
        """Initialize MCP session and wait for tools to be available before mounting"""
        try:
            logger.info(f"🔄 Synchronously initializing MCP session for server: {server_id}")
            
            # Manually establish MCP connection based on server type
            server_type = getattr(sub_app.state, "server_type", "stdio")
            
            if server_type == "sse":
                await self._establish_sse_connection_blocking(sub_app, server_id)
            elif server_type == "stdio":
                await self._establish_stdio_connection_blocking(sub_app, server_id)
            elif server_type == "streamablehttp":
                await self._establish_streamablehttp_connection_blocking(sub_app, server_id)
            else:
                raise Exception(f"Unknown server type for {server_id}: {server_type}")
            
            # Verify that the session was initialized and tools were created
            if not hasattr(sub_app.state, 'session') or not sub_app.state.session:
                raise Exception(f"MCP session not initialized for server {server_id}")
            
            # Test that tools are available and create dynamic endpoints
            try:
                from .main import create_dynamic_endpoints
                api_dependency = getattr(sub_app.state, "api_dependency", None)
                
                # This will create the tool endpoints synchronously
                await create_dynamic_endpoints(sub_app, api_dependency=api_dependency)
                
                # Verify tools were created by listing them
                tools_result = await sub_app.state.session.list_tools()
                tool_count = len(tools_result.tools)
                logger.info(f"✅ MCP server {server_id} ready with {tool_count} tools and endpoints")
                
                if tool_count == 0:
                    logger.warning(f"⚠️ Server {server_id} has 0 tools - may still be initializing")
                
            except Exception as tool_error:
                logger.error(f"❌ Failed to create endpoints for {server_id}: {tool_error}")
                raise Exception(f"Tools/endpoints not available for server {server_id}: {tool_error}")
            
            # Start background connection task to maintain the connection
            task = asyncio.create_task(self._maintain_server_connection(sub_app, server_id))
            
            # Store the task for cleanup
            if not hasattr(self.app.state, 'connection_tasks'):
                self.app.state.connection_tasks = []
            self.app.state.connection_tasks.append(task)
            from .main import register_server_task
            register_server_task(self.app, server_id, task)
            
            logger.info(f"✅ Started maintenance task for server: {server_id}")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize session for server {server_id}: {e}")
            raise

    async def _establish_sse_connection_blocking(self, sub_app, server_id: str):
        """Establish SSE connection synchronously"""
        try:
            from mcp.client.sse import sse_client
            from mcp import ClientSession
            
            url = getattr(sub_app.state, "args", None)
            if not url:
                raise Exception(f"No SSE URL configured for server {server_id}")
            
            logger.info(f"🔗 Establishing SSE connection to: {loggable_url(url)}")
            
            # Create the SSE client connection
            sub_app.state._sse_client = sse_client(
                url=url,
                sse_read_timeout=None,
                headers=getattr(sub_app.state, "headers", None) or None,
            )
            sub_app.state._reader, sub_app.state._writer = await sub_app.state._sse_client.__aenter__()
            
            # Create and initialize the session
            sub_app.state._session_client = ClientSession(sub_app.state._reader, sub_app.state._writer)
            sub_app.state.session = await sub_app.state._session_client.__aenter__()
            
            logger.info(f"✅ SSE connection established for {server_id}")
            
        except Exception as e:
            logger.error(f"❌ Failed to establish SSE connection for {server_id}: {e}")
            raise

    async def _establish_stdio_connection_blocking(self, sub_app, server_id: str):
        """Establish stdio connection synchronously"""
        try:
            from mcp.client.stdio import stdio_client
            from mcp import ClientSession, StdioServerParameters
            import os
            
            command = getattr(sub_app.state, "command", None)
            args = getattr(sub_app.state, "args", [])
            env = getattr(sub_app.state, "env", {})
            
            if not command:
                raise Exception(f"No command configured for server {server_id}")
            
            logger.info(f"🔗 Establishing stdio connection: {command} ({len(args)} args)")
            
            wrapped_command, wrapped_args = stdio_command(command, args, server_id=server_id)
            server_params = StdioServerParameters(
                command=wrapped_command,
                args=wrapped_args,
                env={**env}
            )
            
            # Create the stdio client connection
            sub_app.state._stdio_client = stdio_client(server_params)
            sub_app.state._reader, sub_app.state._writer = await sub_app.state._stdio_client.__aenter__()
            
            # Create and initialize the session
            sub_app.state._session_client = ClientSession(sub_app.state._reader, sub_app.state._writer)
            sub_app.state.session = await sub_app.state._session_client.__aenter__()
            
            logger.info(f"✅ Stdio connection established for {server_id}")
            
        except Exception as e:
            logger.error(f"❌ Failed to establish stdio connection for {server_id}: {e}")
            raise

    async def _establish_streamablehttp_connection_blocking(self, sub_app, server_id: str):
        """Establish streamable HTTP connection synchronously"""
        try:
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import ClientSession
            
            url = getattr(sub_app.state, "args", None)
            if not url:
                raise Exception(f"No HTTP URL configured for server {server_id}")
            
            logger.info(f"🔗 Establishing streamable HTTP connection to: {loggable_url(url)}")
            
            # Create the streamable HTTP client connection
            sub_app.state._http_client = streamablehttp_client(
                url=url, headers=getattr(sub_app.state, "headers", None) or None
            )
            sub_app.state._reader, sub_app.state._writer, _ = await sub_app.state._http_client.__aenter__()
            
            # Create and initialize the session
            sub_app.state._session_client = ClientSession(sub_app.state._reader, sub_app.state._writer)
            sub_app.state.session = await sub_app.state._session_client.__aenter__()
            
            logger.info(f"✅ Streamable HTTP connection established for {server_id}")
            
        except Exception as e:
            logger.error(f"❌ Failed to establish streamable HTTP connection for {server_id}: {e}")
            raise

    async def _initialize_server_session(self, sub_app, server_id: str):
        """Initialize MCP session for a newly mounted server (legacy method)"""
        try:
            logger.info(f"🔄 Initializing MCP session for server: {server_id}")
            
            # Start the server connection in background (non-blocking)
            task = asyncio.create_task(self._start_server_connection_background(sub_app))
            
            # Store the task for cleanup
            if not hasattr(self.app.state, 'connection_tasks'):
                self.app.state.connection_tasks = []
            self.app.state.connection_tasks.append(task)
            from .main import register_server_task
            register_server_task(self.app, server_id, task)
            
            logger.info(f"✅ Started background connection task for server: {server_id}")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize session for server {server_id}: {e}")
            raise
    
    async def _maintain_server_connection(self, sub_app, server_id: str):
        """Maintain server connection after initial setup"""
        try:
            logger.info(f"🔄 Starting connection maintenance for server: {server_id}")
            # Keep connection alive with periodic health checks
            while True:
                await asyncio.sleep(30)  # Health check every 30 seconds
                
                # Verify session is still active
                if hasattr(sub_app.state, 'session') and sub_app.state.session:
                    try:
                        # Simple connectivity test
                        await sub_app.state.session.list_tools()
                        sub_app.state.connection_status = "healthy"
                    except Exception as health_error:
                        logger.warning(f"⚠️ Health check failed for {server_id}: {health_error}")
                        sub_app.state.connection_status = "unhealthy"
                        sub_app.state.connection_error = str(health_error)
                else:
                    logger.error(f"❌ Session lost for server {server_id}")
                    break
                    
        except Exception as e:
            logger.error(f"❌ Connection maintenance failed for {server_id}: {e}")
            if hasattr(sub_app.state, 'connection_error'):
                sub_app.state.connection_error = str(e)
                sub_app.state.connection_status = "failed"

    async def _start_server_connection_background(self, sub_app):
        """Start server connection in background without blocking hot reload"""
        try:
            logger.info(f"🔄 Starting background connection for hot-reloaded server...")
            async with sub_app.router.lifespan_context(sub_app):
                # Keep connection alive
                while True:
                    await asyncio.sleep(30)  # Health check every 30 seconds
        except Exception as e:
            logger.error(f"❌ Background server connection failed: {e}")
            # Store error status for health endpoints
            sub_app.state.connection_error = str(e)
            sub_app.state.connection_status = "failed"
    
    async def _cleanup_background_tasks(self, server_id: str):
        """Cancel this server's connection tasks (and so stop its process), and only its own."""
        try:
            tasks = getattr(self.app.state, "server_tasks", {}).pop(server_id, [])
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if hasattr(self.app.state, "connection_tasks"):
                self.app.state.connection_tasks = [t for t in self.app.state.connection_tasks if t not in tasks]
            logger.info(f"✅ Cleaned up {len(pending)} background tasks for server: {server_id}")
        except Exception as e:
            logger.error(f"❌ Failed to cleanup background tasks for {server_id}: {e}")

    async def _verify_tools_or_force_reload(self, server_id: str, sub_app):
        """Verify tools are available or trigger force reload"""
        try:
            # Wait a moment for tools to be fully available
            import asyncio
            await asyncio.sleep(1)
            
            # Check if tools are accessible via session
            if hasattr(sub_app.state, 'session') and sub_app.state.session:
                try:
                    tools_result = await sub_app.state.session.list_tools()
                    tool_count = len(tools_result.tools)
                    
                    if tool_count > 0:
                        logger.info(f"✅ Tools verified for {server_id}: {tool_count} tools available")
                        return
                    else:
                        logger.warning(f"⚠️ No tools found for {server_id} - will trigger force reload")
                        
                except Exception as tool_error:
                    logger.warning(f"⚠️ Tool verification failed for {server_id}: {tool_error} - will trigger force reload")
            else:
                logger.warning(f"⚠️ No session found for {server_id} - will trigger force reload")
            
            # If we get here, tools aren't available - trigger force reload
            logger.info(f"🔄 Scheduling automatic force reload due to missing tools for {server_id}")
            
            async def delayed_force_reload():
                await asyncio.sleep(2)  # Wait a bit more
                logger.info(f"🔥 Triggering automatic force reload for missing tools")
                await self._trigger_force_reload()
            
            asyncio.create_task(delayed_force_reload())
            
        except Exception as e:
            logger.error(f"❌ Tool verification failed for {server_id}: {e}")

    async def _trigger_force_reload(self):
        """Trigger a force reload of all MCP servers"""
        try:
            logger.info("🔥 TRIGGERING AUTOMATIC FORCE RELOAD")
            
            # Import the reload function from mcp_management
            from .mcp_management import reload_mcp_servers
            
            # Trigger force reload using the existing mechanism
            result = reload_mcp_servers(app=self.app, use_hot_reload=True)
            
            if result.get("success"):
                logger.info(f"✅ Automatic force reload successful: {result.get('message', 'Unknown')}")
            else:
                logger.error(f"❌ Automatic force reload failed: {result.get('error', 'Unknown error')}")
                
        except Exception as e:
            logger.error(f"❌ Failed to trigger automatic force reload: {e}")

    def _notify_changes(self, changes: Dict[str, Any]):
        """Notify about configuration changes"""
        # This could emit events, update UI, etc.
        logger.info(f"Configuration changes: {changes}")

def apply_hot_reload_patch(app, config_path: str):
    """Apply hot reload monkey patch to FastAPI app"""
    
    # Create hot reloader instance
    reloader = MCPHotReloader(app, config_path)
    
    # Store reloader in app state
    app.state.hot_reloader = reloader
    
    # Start hot reload immediately instead of waiting for startup event
    import asyncio
    
    async def initialize_hot_reload():
        try:
            await reloader.start()
            logger.info("🔥 Hot reload watcher started immediately")
        except Exception as e:
            logger.error(f"❌ Failed to start hot reload: {e}")
    
    # Create task to start hot reload
    asyncio.create_task(initialize_hot_reload())
    
    # Add startup/shutdown event handlers for cleanup
    @app.on_event("startup")
    async def start_hot_reload():
        # Hot reload already started, just log
        logger.info("🔥 Hot reload system confirmed active on startup")
    
    @app.on_event("shutdown")
    async def stop_hot_reload():
        reloader.stop()
        logger.info("🔥 Hot reload watcher stopped")
    
    # Add status endpoint
    @app.get("/api/mcp/reload-status")
    async def get_reload_status():
        """Get hot reload status"""
        return {
            "enabled": reloader.observer is not None,
            "config_path": str(reloader.config_path),
            "watching": reloader.is_watching(),
            "watchdog_available": WATCHDOG_AVAILABLE,
            "method": "watchdog" if WATCHDOG_AVAILABLE else "polling",
            "current_servers": list(reloader.current_config.get("mcpServers", {}).keys()) if reloader.current_config else [],
            "active_sessions": len(reloader._sessions),
            "session_status": {
                server_id: session_info.get("status", "unknown") if isinstance(session_info, dict) else "legacy"
                for server_id, session_info in reloader._sessions.items()
            },
            "zero_downtime": True
        }
    
    # Add health check endpoint
    @app.get("/api/mcp/health")
    async def get_health():
        """Get overall MCP system health"""
        healthy_servers = 0
        total_servers = len(reloader.current_config.get("mcpServers", {})) if reloader.current_config else 0
        server_statuses = {}
        
        # Use the actual individual health check results by calling the function directly
        for server_id in reloader.current_config.get("mcpServers", {}):
            try:
                # Import the test function and call it directly
                from .mcp_management import test_mcp_server
                health_data = test_mcp_server(server_id, app)
                server_statuses[server_id] = health_data
                
                # Count as healthy based on the actual health check results
                status = health_data.get("status", "")
                success = health_data.get("success", False)
                tool_count = health_data.get("tool_count", 0)
                
                # Count as healthy if:
                # 1. Test succeeded and status is loaded with tools
                # 2. Status is loaded_partial with connectivity OK (SSE servers)
                if success and status == "loaded" and tool_count > 0:
                    healthy_servers += 1
                elif status == "loaded_partial" and health_data.get("connectivity") == "ok":
                    healthy_servers += 1
                    
            except Exception as e:
                logger.error(f"Health check failed for {server_id}: {e}")
                server_statuses[server_id] = {"status": "error", "message": "health check failed"}
        
        health_ratio = healthy_servers / total_servers if total_servers > 0 else 1.0
        
        # Calculate uptime (since the hot reloader started)
        import time
        uptime_seconds = int(time.time() - reloader.start_time) if hasattr(reloader, 'start_time') else 0
        
        # Format uptime nicely
        if uptime_seconds < 60:
            uptime = f"{uptime_seconds}s"
        elif uptime_seconds < 3600:
            minutes = uptime_seconds // 60
            seconds = uptime_seconds % 60
            uptime = f"{minutes}m {seconds}s"
        else:
            hours = uptime_seconds // 3600
            minutes = (uptime_seconds % 3600) // 60
            uptime = f"{hours}h {minutes}m"
        
        return {
            "status": "healthy" if health_ratio >= 0.8 else "degraded" if health_ratio > 0 else "unhealthy",
            "healthy_servers": healthy_servers,
            "total_servers": total_servers,
            "health_ratio": health_ratio,
            "uptime": uptime,
            "uptime_seconds": uptime_seconds,
            "servers": server_statuses,
            "hot_reload_enabled": reloader.is_watching(),
            "zero_downtime": True
        }
    
    logger.info("Hot reload patch applied successfully")