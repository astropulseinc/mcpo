#!/usr/bin/env python3
"""
Environment Variable Management for MCP Marketplace
Basic implementation of secure environment variable storage
"""

import json
import os
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path
import base64

logger = logging.getLogger(__name__)

ENV_CONFIG_PATH = os.environ.get("NOVA_ENV_CONFIG_PATH", "/opt/nova/environment.json")

def load_environment_config() -> Dict[str, Any]:
    """Load environment variable configuration"""
    if os.path.exists(ENV_CONFIG_PATH):
        with open(ENV_CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {"variables": {}, "templates": {}}

def save_environment_config(config: Dict[str, Any]) -> None:
    """Save environment configuration to file"""
    os.makedirs(os.path.dirname(ENV_CONFIG_PATH), exist_ok=True)
    with open(ENV_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

def simple_encrypt(value: str) -> str:
    """Simple base64 encoding (not secure, but basic obfuscation)"""
    return base64.b64encode(value.encode()).decode()

def simple_decrypt(value: str) -> str:
    """Simple base64 decoding"""
    try:
        return base64.b64decode(value.encode()).decode()
    except:
        return value  # Return as-is if not encoded

def store_environment_variable(key: str, value: str, scope: str = "global", 
                             server_id: Optional[str] = None, sensitive: bool = True) -> Dict[str, Any]:
    """Store environment variable"""
    try:
        config = load_environment_config()
        
        # Simple encryption for sensitive values
        stored_value = simple_encrypt(value) if sensitive else value
        
        var_key = f"{scope}:{server_id or 'global'}:{key}"
        config["variables"][var_key] = {
            "key": key,
            "value": stored_value,
            "scope": scope,
            "server_id": server_id,
            "sensitive": sensitive,
            "created_at": "2024-01-01T00:00:00Z"  # Simplified timestamp
        }
        
        save_environment_config(config)
        logger.info(f"Stored environment variable: {key}")
        return {"success": True, "message": f"Variable {key} stored successfully"}
        
    except Exception as e:
        logger.error(f"Failed to store environment variable {key}: {e}")
        return {"success": False, "error": str(e)}

def get_environment_variable(key: str, scope: str = "global", 
                           server_id: Optional[str] = None) -> Optional[str]:
    """Retrieve environment variable value"""
    try:
        config = load_environment_config()
        var_key = f"{scope}:{server_id or 'global'}:{key}"
        
        if var_key in config["variables"]:
            var_data = config["variables"][var_key]
            value = var_data["value"]
            
            # Decrypt if sensitive
            if var_data.get("sensitive", True):
                return simple_decrypt(value)
            else:
                return value
        
        return None
        
    except Exception as e:
        logger.error(f"Failed to get environment variable {key}: {e}")
        return None

def list_environment_variables(scope: str = "global",
                             server_id: Optional[str] = None) -> Dict[str, Any]:
    """List environment variables; sensitive values are always masked"""
    try:
        config = load_environment_config()
        variables = []
        
        for var_key, var_data in config["variables"].items():
            var_scope, var_server_id, var_name = var_key.split(":", 2)
            
            # Filter by scope and server_id
            if scope != "all" and var_scope != scope:
                continue
            if server_id and var_server_id != server_id:
                continue
            
            value = var_data["value"]
            if var_data.get("sensitive", True):
                value = "***HIDDEN***"
            
            variables.append({
                "key": var_data["key"],
                "value": value,
                "scope": var_data["scope"],
                "server_id": var_data.get("server_id"),
                "sensitive": var_data.get("sensitive", True)
            })
        
        return {"success": True, "variables": variables}
        
    except Exception as e:
        logger.error(f"Failed to list environment variables: {e}")
        return {"success": False, "error": str(e)}

def delete_environment_variable(key: str, scope: str = "global", 
                              server_id: Optional[str] = None) -> Dict[str, Any]:
    """Delete environment variable"""
    try:
        config = load_environment_config()
        var_key = f"{scope}:{server_id or 'global'}:{key}"
        
        if var_key in config["variables"]:
            del config["variables"][var_key]
            save_environment_config(config)
            logger.info(f"Deleted environment variable: {key}")
            return {"success": True, "message": f"Variable {key} deleted successfully"}
        else:
            return {"success": False, "error": f"Variable {key} not found"}
            
    except Exception as e:
        logger.error(f"Failed to delete environment variable {key}: {e}")
        return {"success": False, "error": str(e)}

def get_variable_templates() -> Dict[str, Any]:
    """Get predefined variable templates"""
    templates = {
        "GITHUB_TOKEN": {
            "key": "GITHUB_TOKEN",
            "type": "github_token",
            "description": "GitHub Personal Access Token",
            "help_text": "Create a personal access token in GitHub Settings → Developer settings → Personal access tokens",
            "help_link": "https://github.com/settings/tokens",
            "required": True,
            "sensitive": True,
            "validation_pattern": r"^ghp_[a-zA-Z0-9]{36}$"
        },
        "SLACK_BOT_TOKEN": {
            "key": "SLACK_BOT_TOKEN",
            "type": "slack_token",
            "description": "Slack Bot Token",
            "help_text": "Create a Slack app and get the bot token from OAuth & Permissions",
            "help_link": "https://api.slack.com/apps",
            "required": True,
            "sensitive": True,
            "validation_pattern": r"^xoxb-[0-9]+-[0-9]+-[a-zA-Z0-9]+$"
        },
        "SLACK_TEAM_ID": {
            "key": "SLACK_TEAM_ID",
            "type": "slack_team_id",
            "description": "Slack Team/Workspace ID",
            "help_text": "Your Slack workspace ID, found in your workspace settings or URL",
            "help_link": "https://mcp.so/server/slack",
            "required": True,
            "sensitive": False,
            "validation_pattern": r"^T[A-Z0-9]{8,}$"
        },
        "BRAVE_API_KEY": {
            "key": "BRAVE_API_KEY",
            "type": "brave_api_key",
            "description": "Brave Search API Key",
            "help_text": "Get your API key from Brave Search API dashboard",
            "help_link": "https://api.search.brave.com",
            "required": True,
            "sensitive": True,
            "validation_pattern": r"^BSA[a-zA-Z0-9-_]+$"
        },
        "OPENAI_API_KEY": {
            "key": "OPENAI_API_KEY",
            "type": "openai_key",
            "description": "OpenAI API Key",
            "help_text": "Get your API key from OpenAI Platform → API keys",
            "help_link": "https://platform.openai.com/api-keys",
            "required": True,
            "sensitive": True,
            "validation_pattern": r"^sk-[a-zA-Z0-9]{48}$"
        },
        "DATABASE_URL": {
            "key": "DATABASE_URL",
            "type": "database_url",
            "description": "Database Connection URL",
            "help_text": "Full database connection string including credentials",
            "required": False,
            "sensitive": True,
            "validation_pattern": r"^(postgresql|mysql|sqlite)://.*$"
        }
    }
    
    return {"success": True, "templates": templates}

def validate_variable(key: str, value: str) -> Dict[str, Any]:
    """Basic validation for environment variables"""
    try:
        templates = get_variable_templates()["templates"]
        
        if key not in templates:
            return {"valid": True, "message": "No validation template found"}
        
        template = templates[key]
        pattern = template.get("validation_pattern")
        
        if pattern:
            import re
            if re.match(pattern, value):
                return {"valid": True, "message": "Value format is valid"}
            else:
                return {"valid": False, "message": f"Value does not match expected format for {key}"}
        
        return {"valid": True, "message": "Value accepted"}
        
    except Exception as e:
        logger.error(f"Failed to validate variable {key}: {e}")
        return {"valid": False, "message": f"Validation failed: {str(e)}"}

def apply_environment_patch(app):
    """Add environment management endpoints to the app"""
    from fastapi import HTTPException, Query
    from pydantic import BaseModel
    
    class EnvironmentVariableRequest(BaseModel):
        key: str
        value: str
        scope: str = "global"
        server_id: Optional[str] = None
        sensitive: bool = True
    
    class ValidateVariableRequest(BaseModel):
        value: str
    
    @app.get("/api/environment/variables")
    async def list_env_variables(
        scope: str = Query("global"),
        server_id: Optional[str] = Query(None),
    ):
        """List environment variables (sensitive values are masked)"""
        result = list_environment_variables(scope, server_id)
        if result["success"]:
            return result  # Return full result object with variables array
        else:
            raise HTTPException(status_code=500, detail=result["error"])
    
    @app.post("/api/environment/variables")
    async def create_env_variable(request: EnvironmentVariableRequest):
        """Create or update environment variable"""
        result = store_environment_variable(
            request.key, request.value, request.scope, 
            request.server_id, request.sensitive
        )
        if result["success"]:
            return {"message": result["message"]}
        else:
            raise HTTPException(status_code=400, detail=result["error"])
    
    @app.delete("/api/environment/variables/{key}")
    async def delete_env_variable(
        key: str,
        scope: str = Query("global"),
        server_id: Optional[str] = Query(None)
    ):
        """Delete environment variable"""
        result = delete_environment_variable(key, scope, server_id)
        if result["success"]:
            return {"message": result["message"]}
        else:
            raise HTTPException(status_code=404, detail=result["error"])
    
    @app.get("/api/environment/templates")
    async def get_env_templates():
        """Get variable templates"""
        result = get_variable_templates()
        return result["templates"]
    
    @app.post("/api/environment/variables/{key}/validate")
    async def validate_env_variable(key: str, request: ValidateVariableRequest):
        """Validate environment variable value"""
        result = validate_variable(key, request.value)
        return result
    
    logger.info("Environment Management API endpoints added successfully")