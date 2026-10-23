import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcpo import environment_management, mcp_management

SECRET = "sk-operator-secret"
MEMORY = {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-memory"],
    "env": {"MEMORY_TOKEN": SECRET},
}
TEMPLATES = {"memory": {"config_template": {**MEMORY, "env": {"MEMORY_TOKEN": "{{T}}"}}}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nova-backend": {
                        "type": "streamable_http",
                        "url": "http://nova-backend:8080/mcp",
                        "headers": {"Authorization": f"Bearer {SECRET}"},
                        "is_default": True,
                    },
                    "memory": MEMORY,
                }
            }
        )
    )
    marketplace_path = tmp_path / "marketplace.json"
    marketplace_path.write_text(
        json.dumps({"custom": {"name": "Custom", "config_template": {"type": "sse", "url": "http://x/", "headers": {"X-Key": SECRET}, "env": {"TOKEN": SECRET}}}})
    )
    env_path = tmp_path / "environment.json"
    env_path.write_text(
        json.dumps(
            {
                "variables": {
                    "global:global:GITHUB_TOKEN": {
                        "key": "GITHUB_TOKEN",
                        "value": base64.b64encode(SECRET.encode()).decode(),
                        "scope": "global",
                        "server_id": None,
                        "sensitive": True,
                    }
                },
                "templates": {},
            }
        )
    )
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(mcp_management, "MARKETPLACE_CONFIG_PATH", str(marketplace_path))
    monkeypatch.setattr(mcp_management, "load_marketplace_templates", lambda: TEMPLATES)
    monkeypatch.setattr(environment_management, "ENV_CONFIG_PATH", str(env_path))

    app = FastAPI()
    mcp_management.apply_monkey_patch(app)
    environment_management.apply_environment_patch(app)
    test_client = TestClient(app)
    test_client.config_path = config_path
    return test_client


@pytest.mark.parametrize(
    "path",
    [
        "/api/mcp/servers",
        "/api/mcp/servers/nova-backend",
        "/api/mcp/servers/memory",
        "/api/marketplace/custom",
        "/api/marketplace/custom/custom",
        "/api/marketplace/servers",
        "/api/marketplace/servers/custom",
        "/api/environment/variables?show_values=true",
        "/api/environment/variables?scope=all&show_values=true",
    ],
)
def test_read_apis_never_return_secret_values(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert SECRET not in response.text
    assert base64.b64encode(SECRET.encode()).decode() not in response.text


def test_read_apis_keep_header_and_env_names(client):
    servers = client.get("/api/mcp/servers").json()

    assert list(servers["nova-backend"]["headers"]) == ["Authorization"]
    assert list(servers["memory"]["env"]) == ["MEMORY_TOKEN"]


def test_update_with_masked_values_keeps_the_stored_secret(client):
    shown = client.get("/api/mcp/servers/memory").json()
    body = {k: shown[k] for k in ("type", "command", "args", "env")}

    assert client.put("/api/mcp/servers/memory", json=body).status_code == 200
    stored = json.loads(client.config_path.read_text())["mcpServers"]["memory"]
    assert stored["env"]["MEMORY_TOKEN"] == SECRET
