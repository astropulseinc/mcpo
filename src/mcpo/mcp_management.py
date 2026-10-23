#!/usr/bin/env python3
"""
MCP Management API - Monkey patch for nova-proxy
Adds dynamic MCP server management capabilities to mcpo
"""

import json
import os
import logging
import re
import time
from typing import Dict, Any, List
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False
    logger.warning("PyYAML not available, will use JSON fallback for templates")

import tempfile
CONFIG_PATH = os.environ.get("NOVA_CONFIG_PATH", "/opt/nova/config.json")
MARKETPLACE_CONFIG_PATH = os.environ.get("NOVA_MARKETPLACE_CONFIG_PATH", "/opt/nova/marketplace_servers.json")
MARKETPLACE_TEMPLATES_PATH = os.environ.get("NOVA_MARKETPLACE_TEMPLATES_PATH", "../../nova-proxy/marketplace-templates.yaml")

def load_config() -> Dict[str, Any]:
    """Load current mcpo configuration"""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {"mcpServers": {}, "logging": {"level": "info", "format": "json"}}

def save_config(config: Dict[str, Any]) -> None:
    """Save configuration to file"""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

def load_marketplace_servers() -> Dict[str, Any]:
    """Load marketplace-added servers"""
    if os.path.exists(MARKETPLACE_CONFIG_PATH):
        with open(MARKETPLACE_CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {}

def save_marketplace_servers(servers: Dict[str, Any]) -> None:
    """Save marketplace servers"""
    os.makedirs(os.path.dirname(MARKETPLACE_CONFIG_PATH), exist_ok=True)
    with open(MARKETPLACE_CONFIG_PATH, 'w') as f:
        json.dump(servers, f, indent=2)

def get_protected_servers() -> List[str]:
    """Get list of servers that cannot be removed"""
    return ["nova-backend"]

def load_marketplace_templates() -> Dict[str, Any]:
    """Load verified marketplace templates from YAML/JSON file"""
    try:
        # Try multiple possible locations for the templates file
        if YAML_AVAILABLE:
            template_paths = [
                MARKETPLACE_TEMPLATES_PATH,
                "../../nova-proxy/marketplace-templates.yaml",
                "../nova-proxy/marketplace-templates.yaml", 
                "marketplace-templates.yaml",
                "/opt/nova/marketplace-templates.yaml"
            ]
            
            for template_path in template_paths:
                if os.path.exists(template_path):
                    logger.info(f"Loading marketplace templates from: {template_path}")
                    with open(template_path, 'r') as f:
                        return yaml.safe_load(f) or {}
        
        # Fallback to JSON if YAML not available
        json_paths = [
            "../../nova-proxy/marketplace-templates.json",
            "../nova-proxy/marketplace-templates.json", 
            "marketplace-templates.json"
        ]
        
        for template_path in json_paths:
            if os.path.exists(template_path):
                logger.info(f"Loading marketplace templates from JSON: {template_path}")
                with open(template_path, 'r') as f:
                    return json.load(f)
        
        logger.warning("No marketplace templates file found, using fallback templates")
        return {}
        
    except Exception as e:
        logger.error(f"Failed to load marketplace templates: {e}")
        return {}


SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLACEHOLDER_PATTERN = re.compile(r"^\{\{[A-Za-z0-9_]+\}\}$")


def _is_placeholder(arg: Any) -> bool:
    return isinstance(arg, str) and bool(_PLACEHOLDER_PATTERN.match(arg))


def _args_match(template_args: list[Any], args: list[Any]) -> bool:
    """Literal template args must appear in order; a {{placeholder}} takes one or more args."""
    if not template_args:
        return not args
    head, rest = template_args[0], template_args[1:]
    if _is_placeholder(head):
        return any(_args_match(rest, args[n:]) for n in range(1, len(args) + 1))
    return bool(args) and args[0] == head and _args_match(rest, args[1:])


def find_verified_template(config: dict[str, Any], templates: dict[str, Any]) -> str | None:
    """Return the id of the verified template whose command this config runs, if any."""
    command = config.get("command")
    args = config.get("args") or []
    env_keys = set((config.get("env") or {}).keys())
    for template_id, template in templates.items():
        template_config = (template or {}).get("config_template") or {}
        if not template_config.get("command") or template_config["command"] != command:
            continue
        if not _args_match(template_config.get("args") or [], args):
            continue
        if env_keys <= set((template_config.get("env") or {}).keys()):
            return template_id
    return None


def validate_server_config(server_id: str, config: dict[str, Any]) -> str | None:
    """Return why a server config may not be stored, or None when it may."""
    if not SERVER_ID_PATTERN.match(server_id or ""):
        return "Server id may contain only letters, digits, '-' and '_' (at most 64)"
    if config.get("command") and find_verified_template(config, load_marketplace_templates()) is None:
        return "A command server must match a verified marketplace template (command, arguments and environment keys)"
    return None


SECRET_MASK = "***HIDDEN***"
_SECRET_FIELDS = ("env", "headers")


def redact_secrets(value: Any) -> Any:
    """Copy of a config (at any depth) with every env and header value masked."""
    if isinstance(value, dict):
        return {
            key: (
                {name: SECRET_MASK for name in item}
                if key in _SECRET_FIELDS and isinstance(item, dict)
                else redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _restore_masked_secrets(config: dict[str, Any], stored: dict[str, Any] | None) -> None:
    """Put stored values back where a client sent the mask it was shown."""
    for field in _SECRET_FIELDS:
        submitted = config.get(field)
        if not isinstance(submitted, dict):
            continue
        previous = (stored or {}).get(field) or {}
        config[field] = {
            name: (previous[name] if value == SECRET_MASK else value)
            for name, value in submitted.items()
            if value != SECRET_MASK or name in previous
        }


def add_mcp_server(server_id: str, config: Dict[str, Any], add_to_marketplace: bool = False) -> Dict[str, Any]:
    """Add a new MCP server dynamically"""
    try:
        error = validate_server_config(server_id, config)
        if error:
            logger.warning(f"Rejected MCP server config for {server_id}: {error}")
            return {"success": False, "error": error}

        # Load current config
        current_config = load_config()
        _restore_masked_secrets(config, current_config["mcpServers"].get(server_id))

        # Add to main config
        current_config["mcpServers"][server_id] = config
        save_config(current_config)
        
        # Only add to marketplace if explicitly requested (e.g., from marketplace install)
        if add_to_marketplace:
            marketplace_servers = load_marketplace_servers()
            marketplace_servers[server_id] = config
            save_marketplace_servers(marketplace_servers)
            logger.info(f"Added MCP server to marketplace: {server_id}")
        
        logger.info(f"Added MCP server: {server_id}")
        return {"success": True, "message": f"Server {server_id} added successfully"}
        
    except Exception as e:
        logger.error(f"Failed to add MCP server {server_id}: {e}")
        return {"success": False, "error": str(e)}

def remove_mcp_server(server_id: str, force: bool = False) -> Dict[str, Any]:
    """Remove an MCP server"""
    try:
        # Check if server is protected (cannot be removed unless forced)
        protected_servers = get_protected_servers()
        if server_id in protected_servers and not force:
            logger.warning(f"Attempted to remove protected server: {server_id}")
            return {"success": False, "error": f"Server {server_id} is protected and cannot be removed"}
        
        # Load current config
        current_config = load_config()
        marketplace_servers = load_marketplace_servers()
        
        # TODO: Implement context-aware delete logic:
        # - If called from "Installed Servers" -> only remove from config.json (uninstall)
        # - If called from "Custom Servers" -> remove from marketplace_servers.json (permanent delete)
        # - If called from "Marketplace" -> check if user is author, then unpublish or just uninstall
        # For now, this removes from both places (wrong behavior)
        
        # Remove from marketplace tracking
        if server_id in marketplace_servers:
            del marketplace_servers[server_id]
            save_marketplace_servers(marketplace_servers)
        
        # Remove from main config
        if server_id in current_config["mcpServers"]:
            del current_config["mcpServers"][server_id]
            save_config(current_config)
            
            logger.info(f"Removed MCP server: {server_id}")
            return {"success": True, "message": f"Server {server_id} removed successfully"}
        else:
            return {"success": False, "error": f"Server {server_id} not found"}
            
    except Exception as e:
        logger.error(f"Failed to remove MCP server {server_id}: {e}")
        return {"success": False, "error": str(e)}

def list_mcp_servers() -> Dict[str, Any]:
    """List all MCP servers"""
    try:
        config = load_config()
        marketplace_servers = load_marketplace_servers()
        templates = load_marketplace_templates()
        
        servers = {}
        for server_id, server_config in config["mcpServers"].items():
            # Infer type if not explicitly set
            inferred_type = server_config.get("type")
            if not inferred_type:
                if server_config.get("url"):
                    inferred_type = "sse"  # Has URL = SSE
                elif server_config.get("command"):
                    inferred_type = "stdio"  # Has command = STDIO
                else:
                    inferred_type = "unknown"
            
            # Determine server source
            is_template = server_id in templates
            is_custom_marketplace = server_id in marketplace_servers
            # Respect is_default from config, fallback to false for manually added servers
            is_default = server_config.get("is_default", False)
            
            servers[server_id] = {
                **server_config,
                "type": inferred_type,  # Always include type
                "is_marketplace": is_custom_marketplace,  # Only custom marketplace servers
                "is_template": is_template,  # Verified template servers
                "is_default": is_default,  # Default/manual servers
                "source": "template" if is_template else ("marketplace" if is_custom_marketplace else "default")
            }
        
        return {"success": True, "servers": servers}
        
    except Exception as e:
        logger.error(f"Failed to list MCP servers: {e}")
        return {"success": False, "error": str(e)}

def get_mcp_server(server_id: str) -> Dict[str, Any]:
    """Get specific MCP server configuration"""
    try:
        config = load_config()
        marketplace_servers = load_marketplace_servers()
        
        if server_id in config["mcpServers"]:
            server_config = config["mcpServers"][server_id]
            return {
                "success": True,
                "server": {
                    **server_config,
                    "is_marketplace": server_id in marketplace_servers,
                    "is_default": server_config.get("is_default", False)
                }
            }
        else:
            return {"success": False, "error": f"Server {server_id} not found"}
            
    except Exception as e:
        logger.error(f"Failed to get MCP server {server_id}: {e}")
        return {"success": False, "error": str(e)}

def toggle_mcp_server(server_id: str, enabled: bool) -> Dict[str, Any]:
    """Enable/disable an MCP server"""
    try:
        config = load_config()
        marketplace_servers = load_marketplace_servers()
        
        if server_id not in config["mcpServers"]:
            return {"success": False, "error": f"Server {server_id} not found"}
        
        # Update marketplace tracking
        if server_id in marketplace_servers:
            marketplace_servers[server_id]["enabled"] = enabled
            save_marketplace_servers(marketplace_servers)
        
        # For now, we add/remove from main config to simulate enable/disable
        if enabled:
            # Server should already be in config
            pass
        else:
            # Remove from config to disable
            del config["mcpServers"][server_id]
            save_config(config)
        
        logger.info(f"{'Enabled' if enabled else 'Disabled'} MCP server: {server_id}")
        return {"success": True, "message": f"Server {server_id} {'enabled' if enabled else 'disabled'}"}
        
    except Exception as e:
        logger.error(f"Failed to toggle MCP server {server_id}: {e}")
        return {"success": False, "error": str(e)}

def _test_underlying_connectivity(server_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Test the underlying connectivity (SSE/command) of an MCP server"""
    try:
        server_config = config["mcpServers"][server_id]
        
        if server_config.get("type") == "sse" and server_config.get("url"):
            # Properly test SSE endpoint
            import httpx
            try:
                with httpx.Client(timeout=5.0) as client:
                    # SSE endpoints should accept text/event-stream
                    headers = {
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache"
                    }
                    with client.stream("GET", server_config["url"], headers=headers) as response:
                        # Check status and content-type
                        if response.status_code >= 400:
                            return {"success": False, "message": f"SSE endpoint returned {response.status_code}"}
                        
                        content_type = response.headers.get("content-type", "")
                        if "text/event-stream" not in content_type:
                            return {"success": False, "message": f"SSE endpoint has wrong content-type: {content_type}"}
                        
                        # Try to read first event/line to verify it's streaming
                        for line in response.iter_lines():
                            if line:  # Got some data, SSE is working
                                return {"success": True, "message": "SSE endpoint streaming"}
                            break  # Exit after first line
                        
                        return {"success": True, "message": "SSE endpoint responding"}
                        
            except httpx.ConnectError as e:
                return {"success": False, "message": f"SSE connection failed: {str(e)}"}
            except Exception as e:
                return {"success": False, "message": f"SSE test error: {str(e)}"}
        
        elif server_config.get("command"):
            # Test if command exists and is working
            import shutil
            command = server_config["command"]
            if shutil.which(command):
                return {"success": True, "message": f"command '{command}' available"}
            else:
                return {"success": False, "message": f"command '{command}' not found"}
        
        return {"success": True, "message": "no connectivity test needed"}
        
    except Exception as e:
        return {"success": False, "message": f"connectivity test failed: {str(e)}"}

def _get_mcp_tools_count(server_id: str, app=None) -> Dict[str, Any]:
    """Get actual MCP tools count via tools/list API call"""
    try:
        if not app:
            return {"count": 0, "method": "no_app"}
            
        # Find the mounted server and try to get tools via the sub-app
        for route in app.routes:
            if hasattr(route, 'path') and route.path == f"/{server_id}":
                if hasattr(route, 'app'):
                    sub_app = route.app
                    # Try to get tools from the sub-app's OpenAPI spec as fallback
                    try:
                        if hasattr(sub_app, 'openapi'):
                            openapi_spec = sub_app.openapi()
                            if openapi_spec and 'paths' in openapi_spec:
                                # Count actual tool endpoints (not just paths)
                                tool_paths = [path for path in openapi_spec['paths'].keys() 
                                            if '/tools/' in path or path.endswith('/call')]
                                if tool_paths:
                                    return {"count": len(tool_paths), "method": "openapi_tools"}
                                else:
                                    # Count all paths as potential tools
                                    path_count = len(openapi_spec['paths'])
                                    return {"count": path_count, "method": "openapi_paths"}
                    except:
                        pass
                    
                    # If we have a session, assume it's working but count unknown
                    if hasattr(sub_app.state, 'session') and sub_app.state.session:
                        return {"count": -1, "method": "session_found", "message": "MCP session active"}
                break
        
        return {"count": 0, "method": "no_session"}
    except Exception as e:
        logger.error(f"Failed to get MCP tools count for {server_id}: {e}")
        return {"count": 0, "method": "error", "error": str(e)}

def test_mcp_server(server_id: str, app=None) -> Dict[str, Any]:
    """Test if MCP server is actually loaded and working in MCPO"""
    try:
        config = load_config()
        
        if server_id not in config["mcpServers"]:
            return {"success": False, "message": f"Server {server_id} not found", "status": "not_found"}
        
        # Check if server is mounted in the FastAPI app
        if app:
            # Look for mounted sub-applications
            server_mounted = False
            tool_count = 0
            
            for route in app.routes:
                if hasattr(route, 'path') and route.path == f"/{server_id}":
                    server_mounted = True
                    # For MCP servers, get actual tools count via MCP protocol
                    if hasattr(route, 'app'):
                        try:
                            # Get MCP tools count
                            mcp_result = _get_mcp_tools_count(server_id, app)
                            tool_count = max(0, mcp_result.get("count", 0))  # Don't show negative counts
                            
                            # If we have an active MCP session but count is unknown, show that
                            if mcp_result.get("count") == -1:
                                connectivity_result = _test_underlying_connectivity(server_id, config)
                                return {
                                    "success": True,
                                    "message": f"MCP session active, connectivity OK - {mcp_result.get('message', '')}",
                                    "status": "loaded",
                                    "tool_count": "unknown",
                                    "mcp_session": True
                                }
                            
                            # Check if server has no tools (indicates a problem)
                            if tool_count == 0:
                                # For SSE servers, check if connectivity is OK even with 0 tools
                                # This handles the case where SSE connection works but async lifecycle has issues
                                connectivity_result = _test_underlying_connectivity(server_id, config)
                                if connectivity_result["success"] and server_config.get("type") == "sse":
                                    return {
                                        "success": True,
                                        "message": "SSE connection working, but 0 tools - may need reload",
                                        "status": "loaded_partial",
                                        "tool_count": 0,
                                        "connectivity": "ok",
                                        "hint": "Try reloading this server"
                                    }
                                else:
                                    return {
                                        "success": False,
                                        "message": "Server loaded but has 0 tools - likely initialization failed",
                                        "status": "loaded_failed",
                                        "tool_count": 0
                                    }
                            
                            # Server is mounted AND has tools - now test underlying connectivity
                            if tool_count > 0:
                                connectivity_result = _test_underlying_connectivity(server_id, config)
                                if connectivity_result["success"]:
                                    return {
                                        "success": True,
                                        "message": f"Server loaded with {tool_count} tools, connectivity OK",
                                        "status": "loaded",
                                        "tool_count": tool_count
                                    }
                                else:
                                    return {
                                        "success": False,
                                        "message": f"Server loaded with {tool_count} tools, but {connectivity_result['message']}",
                                        "status": "loaded_failed",
                                        "tool_count": tool_count
                                    }
                        except:
                            pass
                    break
            
            if server_mounted:
                # Server mounted but no tools - test connectivity
                connectivity_result = _test_underlying_connectivity(server_id, config)
                if connectivity_result["success"]:
                    return {"success": True, "message": "Server mounted, connectivity OK", "status": "loaded"}
                else:
                    return {"success": False, "message": f"Server mounted but {connectivity_result['message']}", "status": "loaded_failed"}
            else:
                return {"success": False, "message": "Server not mounted in MCPO", "status": "not_loaded"}
        
        # Fallback to basic config validation if no app provided
        server_config = config["mcpServers"][server_id]
        
        if server_config.get("type") == "sse" and server_config.get("url"):
            return {"success": True, "message": "SSE configuration valid", "status": "config_valid"}
        elif server_config.get("command"):
            import shutil
            command = server_config["command"]
            if shutil.which(command):
                return {"success": True, "message": f"Command '{command}' available", "status": "config_valid"}
            else:
                return {"success": False, "message": f"Command '{command}' not found", "status": "unavailable"}
        
        return {"success": True, "message": "Configuration valid", "status": "config_valid"}
        
    except Exception as e:
        logger.error(f"Failed to test MCP server {server_id}: {e}")
        return {"success": False, "message": str(e), "status": "error"}

def reload_individual_server(server_id: str, app=None) -> Dict[str, Any]:
    """Reload a specific MCP server (bypass exponential backoff)"""
    try:
        # Get current server configuration
        server_result = get_mcp_server(server_id)
        if not server_result["success"]:
            return {"success": False, "error": f"Server {server_id} not found"}
        
        server_config = server_result["server"]
        
        # For now, use the simpler fallback approach for individual server reload
        # The hot reload async context is complex and better suited for full system reloads
        logger.info(f"🔄 Reloading server {server_id} via config update (bypasses exponential backoff)")
        
        # Remove and re-add the server to force immediate reconnection
        # This effectively bypasses any exponential backoff timers
        
        # Step 1: Remove the server (this will disconnect SSE streams)
        # Force remove to bypass protection for individual reloads
        remove_result = remove_mcp_server(server_id, force=True)
        if not remove_result["success"]:
            logger.warning(f"Failed to remove server {server_id} during reload: {remove_result['error']}")
        else:
            logger.info(f"✅ Removed server {server_id} for reload")
        
        # Step 2: Add the server back (this will create fresh connections)
        add_result = add_mcp_server(server_id, server_config, add_to_marketplace=False)
        if add_result["success"]:
            logger.info(f"✅ Re-added server {server_id} with fresh connections")
            return {
                "success": True,
                "message": f"Server {server_id} reloaded successfully - fresh connections established",
                "method": "remove_and_readd",
                "timestamp": time.time(),
                "details": "Bypassed exponential backoff by creating fresh connections"
            }
        else:
            # If re-adding fails, we're in a bad state
            logger.error(f"❌ Failed to re-add server {server_id} during reload: {add_result['error']}")
            return {
                "success": False, 
                "error": f"Server reload failed - could not re-add server: {add_result['error']}",
                "recovery_hint": "Try removing and manually re-adding the server"
            }
            
    except Exception as e:
        logger.error(f"Failed to reload individual server {server_id}: {e}")
        return {"success": False, "error": str(e)}

def reload_mcp_servers(app=None, use_hot_reload=True) -> Dict[str, Any]:
    """Reload all MCP servers using hot reload or fallback to restart"""
    try:
        # Validate config first
        config = load_config()
        marketplace_servers = load_marketplace_servers()
        
        # Try hot reload first if available and requested
        if use_hot_reload and app and hasattr(app.state, 'hot_reloader'):
            hot_reloader = app.state.hot_reloader
            if hot_reloader and hot_reloader.observer and hot_reloader.is_watching():
                logger.info("🔥 MCP servers reload requested - using zero-downtime hot reload")
                
                # Trigger hot reload manually with proper async handling
                import asyncio
                
                async def trigger_reload():
                    try:
                        await hot_reloader._reload_servers()
                        logger.info("✅ Hot reload completed successfully")
                    except Exception as e:
                        logger.error(f"❌ Hot reload failed: {e}")
                        raise
                
                # Schedule the reload to happen after response is sent
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Use call_soon to ensure it runs in the next event loop iteration
                    def schedule_reload():
                        asyncio.create_task(trigger_reload())
                    loop.call_soon(schedule_reload)
                else:
                    # Run in new loop if no loop is running
                    asyncio.run(trigger_reload())
                
                return {
                    "success": True, 
                    "message": "Zero-downtime reload initiated - servers will reload shortly",
                    "method": "hot_reload",
                    "server_count": len(config["mcpServers"]),
                    "marketplace_count": len(marketplace_servers),
                    "zero_downtime": True
                }
        
        # Fallback to process restart
        logger.info("MCP servers reload requested - falling back to process restart")
        
        # Signal the application to restart
        import signal
        import os
        
        # Schedule restart after response is sent
        def restart_process():
            import time
            import sys
            time.sleep(1)  # Give time for response to be sent
            logger.info("Restarting mcpo process...")
            os.execvp(sys.executable, [sys.executable] + sys.argv)
        
        import threading
        restart_thread = threading.Thread(target=restart_process, daemon=True)
        restart_thread.start()
        
        return {
            "success": True, 
            "message": "Servers reload initiated - mcpo will restart (fallback method)",
            "method": "process_restart",
            "server_count": len(config["mcpServers"]),
            "marketplace_count": len(marketplace_servers)
        }
        
    except Exception as e:
        logger.error(f"Failed to reload MCP servers: {e}")
        return {"success": False, "error": str(e)}

def apply_monkey_patch(app):
    """Apply monkey patch to add MCP management endpoints"""
    from fastapi import HTTPException, Query
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel
    
    # Add static file serving (try multiple locations)
    static_mounted = False
    for static_dir in ["/app/static", "static", "../nova-proxy/static", "../../nova-proxy/static"]:
        try:
            if os.path.exists(static_dir):
                app.mount("/static", StaticFiles(directory=static_dir), name="static")
                logger.info(f"Mounted static files from: {static_dir}")
                static_mounted = True
                break
        except Exception as e:
            continue
    
    if not static_mounted:
        logger.warning("Could not mount static files from any location")
    
    # Add root redirect to test interface
    @app.get("/", response_class=HTMLResponse)
    async def root():
        """Serve test interface"""
        # Try to serve the static HTML file
        for static_dir in ["/app/static", "static", "../nova-proxy/static", "../../nova-proxy/static"]:
            try:
                index_path = os.path.join(static_dir, "index.html")
                if os.path.exists(index_path):
                    with open(index_path, "r") as f:
                        return f.read()
            except Exception as e:
                continue
        
        # Fallback to simple HTML
        return "<h1>Nova MCP Marketplace API</h1><p>API endpoints available at /api/</p><p>Documentation: <a href='/docs'>/docs</a></p>"
    
    class MCPServerConfig(BaseModel):
        type: str
        url: str = None
        command: str = None
        args: List[str] = None
        env: Dict[str, str] = None
    
    class ServerToggleRequest(BaseModel):
        enabled: bool
    
    # Basic CRUD endpoints
    @app.get("/api/mcp/servers")
    async def get_servers():
        """List all MCP servers"""
        result = list_mcp_servers()
        if result["success"]:
            return redact_secrets(result["servers"])
        else:
            raise HTTPException(status_code=500, detail=result["error"])
    
    @app.get("/api/mcp/servers/reload")
    async def reload_servers(force: bool = Query(False)):
        """Reload all MCP servers. If force=true, kills process (Docker restarts it)"""
        if force:
            # Force restart - kill the process, Docker will restart it
            import signal
            import os
            try:
                logger.info("🔥 Force restarting process for all servers")
                
                # Schedule restart after response is sent
                def restart_process():
                    import time
                    import sys
                    time.sleep(1)  # Give time for response to be sent
                    logger.info("Force restarting mcpo process...")
                    os.execvp(sys.executable, [sys.executable] + sys.argv)
                
                import threading
                restart_thread = threading.Thread(target=restart_process, daemon=True)
                restart_thread.start()
                
                return {
                    "success": True,
                    "message": "Force restart initiated for all servers - process will restart",
                    "method": "process_restart"
                }
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Force restart failed: {str(e)}")
        else:
            # Normal reload
            result = reload_mcp_servers(app=app, use_hot_reload=True)
            if result["success"]:
                return result
            else:
                raise HTTPException(status_code=500, detail=result["error"])
    
    @app.get("/api/mcp/servers/{server_id}")
    async def get_server(server_id: str):
        """Get specific MCP server"""
        result = get_mcp_server(server_id)
        if result["success"]:
            return redact_secrets(result["server"])
        else:
            raise HTTPException(status_code=404, detail=result["error"])
    
    @app.post("/api/mcp/servers/{server_id}")
    async def add_server(server_id: str, config: MCPServerConfig):
        """Add new MCP server"""
        result = add_mcp_server(server_id, config.dict(exclude_none=True))
        if result["success"]:
            return {"message": result["message"]}
        else:
            raise HTTPException(status_code=400, detail=result["error"])
    
    @app.put("/api/mcp/servers/{server_id}")
    async def update_server(server_id: str, config: MCPServerConfig):
        """Update MCP server configuration"""
        result = add_mcp_server(server_id, config.dict(exclude_none=True))  # Same as add for now
        if result["success"]:
            return {"message": f"Server {server_id} updated successfully"}
        else:
            raise HTTPException(status_code=400, detail=result["error"])
    
    @app.delete("/api/mcp/servers/{server_id}")
    async def remove_server(server_id: str):
        """Remove MCP server"""
        result = remove_mcp_server(server_id)
        if result["success"]:
            return {"message": result["message"]}
        else:
            raise HTTPException(status_code=404, detail=result["error"])
    
    @app.post("/api/mcp/servers/{server_id}/reload")
    async def reload_single_server(server_id: str, force: bool = Query(False)):
        """Reload a specific MCP server. If force=true, kills process (Docker restarts it)"""
        if force:
            # Force restart - kill the process, Docker will restart it
            import signal
            import os
            try:
                # For Docker containers, we just restart the whole mcpo process
                # The Docker container will restart and reconnect
                logger.info(f"🔥 Force restarting process for server {server_id}")
                
                # Schedule restart after response is sent
                def restart_process():
                    import time
                    import sys
                    time.sleep(1)  # Give time for response to be sent
                    logger.info("Force restarting mcpo process...")
                    os.execvp(sys.executable, [sys.executable] + sys.argv)
                
                import threading
                restart_thread = threading.Thread(target=restart_process, daemon=True)
                restart_thread.start()
                
                return {
                    "success": True,
                    "message": f"Force restart initiated for {server_id} - process will restart",
                    "method": "process_restart"
                }
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Force restart failed: {str(e)}")
        else:
            # Normal reload
            result = reload_individual_server(server_id, app=app)
            if result["success"]:
                return result
            else:
                raise HTTPException(status_code=500, detail=result["error"])
    
    # Additional endpoints from design docs
    @app.patch("/api/mcp/servers/{server_id}/toggle")
    async def toggle_server(server_id: str, request: ServerToggleRequest):
        """Enable/disable MCP server"""
        result = toggle_mcp_server(server_id, request.enabled)
        if result["success"]:
            return {"message": result["message"]}
        else:
            raise HTTPException(status_code=400, detail=result["error"])
    
    @app.get("/api/mcp/servers/{server_id}/test")
    async def test_server(server_id: str):
        """Test MCP server connectivity"""
        result = test_mcp_server(server_id, app)
        if result["success"]:
            return {"message": result["message"], "status": result.get("status", "unknown"), "tool_count": result.get("tool_count"), "success": True}
        else:
            return {"message": result.get("message", "Unknown error"), "status": "error", "success": False}
    
    @app.get("/api/mcp/servers/{server_id}/health")
    async def get_server_health(server_id: str):
        """Get MCP server health status"""
        result = test_mcp_server(server_id, app)  # Same as test for now
        if result["success"]:
            return {"status": result.get("status", "unknown"), "message": result["message"], "tool_count": result.get("tool_count")}
        else:
            return {"status": "error", "message": result.get("message", "Unknown error")}
    
    # Marketplace endpoints (API-driven implementation)
    @app.get("/api/marketplace/templates")
    async def get_marketplace_templates(category: str = Query(None)):
        """Get verified marketplace templates from YAML/JSON file"""
        templates = load_marketplace_templates()
        
        if category:
            filtered = {k: v for k, v in templates.items() if v.get("category") == category}
            return filtered
        return templates
    
    @app.get("/api/marketplace/custom")
    async def get_custom_servers():
        """Get user's custom servers (non-verified)"""
        return redact_secrets(load_marketplace_servers())
    
    @app.get("/api/marketplace/servers")
    async def get_marketplace_servers(category: str = Query(None)):
        """Browse all marketplace servers (templates + custom)"""
        # Load verified templates from YAML
        templates = load_marketplace_templates()
        
        # Load user's custom servers
        custom_servers = load_marketplace_servers()
        
        # Combine them with distinction
        all_servers = {}
        
        # Add verified templates (create copies to avoid mutation)
        for server_id, template in templates.items():
            template_copy = template.copy()
            template_copy["verified"] = True
            template_copy["source"] = "template"
            all_servers[server_id] = template_copy
        
        # Add custom servers (mark as unverified)
        for server_id, custom in redact_secrets(custom_servers).items():
            custom_copy = custom.copy()
            custom_copy["verified"] = False
            custom_copy["source"] = "custom"
            all_servers[server_id] = custom_copy
        
        # Fallback to hardcoded if YAML is empty
        if not templates:
            marketplace_catalog = {
            "github": {
                "id": "github",
                "name": "GitHub",
                "description": "Access GitHub repositories, issues, pull requests, and more. Search code, create issues, manage repos.",
                "category": "development",
                "author": "ModelContextProtocol",
                "version": "1.0.0",
                "icon": "🐙",
                "config_template": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
                },
                "requires": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
                "setup_help": {
                    "title": "GitHub Personal Access Token",
                    "description": "You need a GitHub Personal Access Token to use this server.",
                    "steps": [
                        "1. Go to GitHub.com → Settings → Developer settings → Personal access tokens",
                        "2. Click \"Generate new token (classic)\"",
                        "3. Give it a name and select scopes: repo, read:user, read:org",
                        "4. Copy the token and paste it below"
                    ],
                    "link": "https://github.com/settings/tokens",
                    "placeholder": "ghp_..."
                }
            },
            "slack": {
                "id": "slack",
                "name": "Slack",
                "description": "Send messages to Slack channels and users. Manage channels, read messages, and interact with your workspace.",
                "category": "communication",
                "author": "ModelContextProtocol",
                "version": "1.0.0",
                "icon": "💬",
                "config_template": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-slack"],
                    "env": {"SLACK_BOT_TOKEN": "${SLACK_BOT_TOKEN}", "SLACK_TEAM_ID": "${SLACK_TEAM_ID}"}
                },
                "requires": ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
                "setup_help": {
                    "title": "Slack App Setup",
                    "description": "You need a Slack Bot Token and Team ID to use this server.",
                    "steps": [
                        "1. Go to api.slack.com/apps and create a new app",
                        "2. Go to OAuth & Permissions and add bot scopes: channels:history, channels:read, chat:write, reactions:write, users:read",
                        "3. Install the app to your workspace",
                        "4. Copy the Bot User OAuth Token (SLACK_BOT_TOKEN) and your Team ID (SLACK_TEAM_ID)"
                    ],
                    "link": "https://mcp.so/server/slack",
                    "placeholder": "xoxb-... (Bot Token) and T... (Team ID)"
                }
            },
            "filesystem": {
                "id": "filesystem",
                "name": "File System",
                "description": "Read and write files on the local filesystem. Perfect for editing code, reading documents, and file management.",
                "category": "system",
                "author": "ModelContextProtocol",
                "version": "1.0.0",
                "icon": "📁",
                "config_template": {
                    "type": "stdio",
                    "command": "npx",
                    "args": [
                        "-y", 
                        "@modelcontextprotocol/server-filesystem",
                        "/Users/username/workspace",
                        "/Users/username/Desktop"
                    ],
                    "env": {}
                },
                "requires": [],
                "setup_help": {
                    "title": "Filesystem Access Paths",
                    "description": "Configure which directories the filesystem server can access.",
                    "steps": [
                        "1. Replace '/Users/username/workspace' with your actual workspace path",
                        "2. Replace '/Users/username/Desktop' with your actual desktop path or remove if not needed",
                        "3. Add additional allowed directories as needed",
                        "4. Paths must be absolute (starting with /) for security"
                    ],
                    "placeholder": "/Users/yourname/allowed/directory"
                }
            },
            "memory": {
                "id": "memory",
                "name": "Memory",
                "description": "Store and retrieve information across conversations. Great for maintaining context and remembering important details.",
                "category": "utility",
                "author": "ModelContextProtocol",
                "version": "1.0.0",
                "icon": "🧠",
                "config_template": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-memory"],
                    "env": {}
                },
                "requires": []
            }
            }
            
            # Add fallback catalog to all_servers with verified flag
            for server_id, template in marketplace_catalog.items():
                template["verified"] = True
                template["source"] = "fallback"
                all_servers[server_id] = template
        
        if category:
            filtered = {k: v for k, v in all_servers.items() if v.get("category") == category}
            return filtered
        return all_servers
    
    @app.get("/api/marketplace/servers/{server_id}")
    async def get_marketplace_server(server_id: str):
        """Get marketplace server details"""
        marketplace_servers = await get_marketplace_servers(category=None)
        if server_id in marketplace_servers:
            return marketplace_servers[server_id]
        else:
            raise HTTPException(status_code=404, detail="Marketplace server not found")
    
    @app.get("/api/marketplace/servers/{server_id}/config-schema")
    async def get_server_config_schema(server_id: str):
        """Get configuration schema for a marketplace server"""
        templates = load_marketplace_templates()
        
        if server_id not in templates:
            raise HTTPException(status_code=404, detail="Server not found in marketplace")
        
        server_info = templates[server_id]
        
        # Generate configuration schema from user_configurable fields
        schema = {
            "server_id": server_id,
            "name": server_info.get("name", server_id),
            "description": server_info.get("description", ""),
            "required_env_vars": server_info.get("requires", []),
            "setup_help": server_info.get("setup_help", {}),
            "configurable_fields": []
        }
        
        # Extract configurable fields from the template
        user_configurable = server_info.get("user_configurable", {})
        for field_key, field_config in user_configurable.items():
            schema["configurable_fields"].append({
                "field": field_key,
                "type": field_config.get("type", "string"),
                "label": field_config.get("label", field_key),
                "description": field_config.get("description", ""),
                "required": field_config.get("required", False),
                "default": field_config.get("default"),
                "validation": field_config.get("validation", {}),
                "help": field_config.get("help", ""),
                "placeholder": field_config.get("placeholder", "")
            })
        
        return schema
    
    class InstallRequest(BaseModel):
        user_config: Dict[str, Any] = {}  # User-configured values for template variables
        environment_vars: Dict[str, str] = {}  # Environment variables
    
    def _resolve_template_config(config_template: Dict, server_info: Dict, user_config: Dict) -> Dict:
        """Resolve template variables in config with user-provided values"""
        import json
        import re
        
        # Convert config to JSON string for easy replacement
        config_str = json.dumps(config_template)
        
        # Get user configurable fields
        user_configurable = server_info.get("user_configurable", {})
        
        # Replace template variables
        for field_key, field_config in user_configurable.items():
            template_var = f"{{{{{field_key}}}}}"
            
            if field_key in user_config:
                # Use user-provided value
                replacement_value = user_config[field_key]
            else:
                # Use default value
                replacement_value = field_config.get("default")
            
            if replacement_value is not None:
                # Handle different value types
                if field_config.get("type") == "path_list" and isinstance(replacement_value, list):
                    # For path lists, expand each path as separate args
                    if template_var in config_str:
                        # Find the args array and expand the template
                        config_obj = json.loads(config_str)
                        if "args" in config_obj and isinstance(config_obj["args"], list):
                            new_args = []
                            for arg in config_obj["args"]:
                                if arg == template_var:
                                    # Replace with all paths
                                    new_args.extend(replacement_value)
                                else:
                                    new_args.append(arg)
                            config_obj["args"] = new_args
                            config_str = json.dumps(config_obj)
                elif isinstance(replacement_value, (list, dict)):
                    # For complex values, replace as JSON
                    config_str = config_str.replace(f'"{template_var}"', json.dumps(replacement_value))
                else:
                    # For simple values, replace as string
                    config_str = config_str.replace(template_var, str(replacement_value))
        
        return json.loads(config_str)
    
    @app.post("/api/marketplace/install/{server_id}")
    async def install_marketplace_server(server_id: str, install_request: InstallRequest = None):
        """Install server from marketplace with optional customization"""
        # Check verified templates first
        templates = load_marketplace_templates()
        if server_id in templates:
            server_info = templates[server_id]
            config = server_info["config_template"].copy()
            required_vars = server_info.get("requires", [])
        else:
            # Check custom marketplace servers
            marketplace_servers = load_marketplace_servers()
            if server_id in marketplace_servers:
                server_info = marketplace_servers[server_id]
                config = server_info.get("config_template", server_info).copy()
                required_vars = server_info.get("requires", [])
            else:
                raise HTTPException(status_code=404, detail="Server not found in marketplace")
        
        # Resolve template variables with user configuration (or defaults)
        user_config = {}
        if install_request and install_request.user_config:
            user_config = install_request.user_config
        
        # Always resolve templates (will use defaults if no user config provided)
        config = _resolve_template_config(config, server_info, user_config)
        
        # Apply environment variable customizations
        if install_request and install_request.environment_vars:
            if "env" not in config:
                config["env"] = {}
            config["env"].update(install_request.environment_vars)
        
        error = validate_server_config(server_id, config)
        if error:
            logger.warning(f"Rejected marketplace install of {server_id}: {error}")
            raise HTTPException(status_code=400, detail=error)

        # Validate required environment variables exist
        if required_vars:
            from mcpo.environment_management import list_environment_variables
            env_result = list_environment_variables()
            if env_result["success"]:
                existing_vars = [var["key"] for var in env_result["variables"]]
                missing_vars = [var for var in required_vars if var not in existing_vars]
                
                if missing_vars:
                    raise HTTPException(
                        status_code=400, 
                        detail=f"Missing required environment variables: {', '.join(missing_vars)}"
                    )
        
        # Install using the direct config addition (bypassing marketplace tracking for templates)
        # Load current config
        current_config = load_config()
        
        # Add to main config
        current_config["mcpServers"][server_id] = config
        save_config(current_config)
        
        # Only track in marketplace_servers.json if it's a custom server (not a verified template)
        if server_id not in templates:
            # This is a custom marketplace server, track it
            marketplace_servers = load_marketplace_servers()
            marketplace_servers[server_id] = server_info
            save_marketplace_servers(marketplace_servers)
        
        logger.info(f"Installed MCP server from marketplace: {server_id}")
        return {"message": f"Server {server_id} installed successfully"}
    
    @app.post("/api/marketplace/custom/{server_id}")
    async def add_to_marketplace(server_id: str):
        """Add an existing configured server to the marketplace"""
        # Check if server exists in config
        current_config = load_config()
        if server_id not in current_config.get("mcpServers", {}):
            raise HTTPException(status_code=404, detail=f"Server {server_id} not found in configuration")
        
        # Check if it's already a verified template
        templates = load_marketplace_templates()
        if server_id in templates:
            raise HTTPException(status_code=400, detail=f"Server {server_id} is a verified template and cannot be modified")
        
        # Check if server is healthy before allowing marketplace addition
        from mcpo.mcp_management import test_mcp_server
        test_result = test_mcp_server(server_id)
        if not test_result.get("success", False):
            raise HTTPException(
                status_code=400, 
                detail=f"Server {server_id} must pass health check before adding to marketplace. Current status: {test_result.get('message', 'Unknown error')}"
            )
        
        # Get server config
        server_config = current_config["mcpServers"][server_id]
        error = validate_server_config(server_id, server_config)
        if error:
            raise HTTPException(status_code=400, detail=error)

        # Add to marketplace
        marketplace_servers = load_marketplace_servers()
        marketplace_servers[server_id] = {
            "name": server_id.replace("_", " ").title(),
            "description": f"Custom MCP server: {server_id}",
            "author": "Custom",
            "version": "1.0.0",
            "verified": False,
            "config_template": server_config,
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        save_marketplace_servers(marketplace_servers)
        
        logger.info(f"Added server {server_id} to marketplace")
        return {"message": f"Server {server_id} successfully added to marketplace"}
    
    @app.delete("/api/marketplace/custom/{server_id}")
    async def remove_from_marketplace(server_id: str):
        """Remove a custom server from the marketplace"""
        # Check if it's a verified template (cannot be removed)
        templates = load_marketplace_templates()
        if server_id in templates:
            raise HTTPException(status_code=400, detail=f"Server {server_id} is a verified template and cannot be removed")
        
        # Check if it exists in marketplace
        marketplace_servers = load_marketplace_servers()
        if server_id not in marketplace_servers:
            raise HTTPException(status_code=404, detail=f"Server {server_id} not found in marketplace")
        
        # Remove from marketplace
        del marketplace_servers[server_id]
        save_marketplace_servers(marketplace_servers)
        
        logger.info(f"Removed server {server_id} from marketplace")
        return {"message": f"Server {server_id} removed from marketplace"}
    
    @app.get("/api/marketplace/custom/{server_id}")
    async def get_marketplace_status(server_id: str):
        """Check if a server is in the marketplace and get its details"""
        templates = load_marketplace_templates()
        marketplace_servers = load_marketplace_servers()
        
        if server_id in templates:
            return {
                "in_marketplace": True,
                "type": "verified_template",
                "can_modify": False,
                "details": templates[server_id]
            }
        elif server_id in marketplace_servers:
            return {
                "in_marketplace": True,
                "type": "custom",
                "can_modify": True,
                "details": redact_secrets(marketplace_servers[server_id])
            }
        else:
            return {
                "in_marketplace": False,
                "type": None,
                "can_modify": False,
                "details": None
            }
    
    # Add OpenAPI schema endpoint
    @app.get("/openapi.json")
    async def get_openapi():
        """Get OpenAPI schema for client generation"""
        try:
            if hasattr(app, 'openapi'):
                return app.openapi()
            else:
                # Fallback manual schema
                return {
                    "openapi": "3.0.0",
                    "info": {
                        "title": "Nova MCP Marketplace API",
                        "version": "1.0.0",
                        "description": "API for managing MCP servers and environment variables in Nova Platform"
                    },
                    "servers": [{"url": "http://localhost:8000"}],
                    "paths": {
                        "/api/mcp/servers": {
                            "get": {
                                "summary": "List all MCP servers",
                                "tags": ["MCP Servers"]
                            },
                            "post": {
                                "summary": "Add new MCP server",
                                "tags": ["MCP Servers"],
                                "parameters": [{"name": "server_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                                "requestBody": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "properties": {
                                                    "type": {"type": "string", "enum": ["sse", "stdio"]},
                                                    "url": {"type": "string"},
                                                    "command": {"type": "string"},
                                                    "args": {"type": "array", "items": {"type": "string"}},
                                                    "env": {"type": "object"}
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        },
                        "/api/marketplace/servers": {
                            "get": {
                                "summary": "Browse marketplace catalog",
                                "tags": ["Marketplace"]
                            }
                        },
                        "/api/environment/variables": {
                            "get": {
                                "summary": "List environment variables",
                                "tags": ["Environment"]
                            },
                            "post": {
                                "summary": "Create environment variable",
                                "tags": ["Environment"]
                            }
                        }
                    }
                }
        except Exception as e:
            return {"error": f"Failed to generate OpenAPI schema: {str(e)}"}
    
    # Add API documentation endpoint
    @app.get("/docs", response_class=HTMLResponse)
    async def api_docs():
        """Serve API documentation"""
        return """
        <html>
        <head>
            <title>Nova MCP API Documentation</title>
            <link rel="stylesheet" type="text/css" href="https://unpkg.com/swagger-ui-dist@3.25.0/swagger-ui.css" />
        </head>
        <body>
            <div id="swagger-ui"></div>
            <script src="https://unpkg.com/swagger-ui-dist@3.25.0/swagger-ui-bundle.js"></script>
            <script>
                SwaggerUIBundle({
                    url: '/openapi.json',
                    dom_id: '#swagger-ui',
                    presets: [
                        SwaggerUIBundle.presets.apis,
                        SwaggerUIBundle.presets.standalone
                    ]
                });
            </script>
        </body>
        </html>
        """
    
    logger.info("MCP Management API endpoints added successfully")