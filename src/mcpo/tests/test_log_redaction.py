import asyncio
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcpo import mcp_management
from mcpo.hot_reload import MCPHotReloader
from mcpo.main import build_app, create_dynamic_endpoints
from mcpo.tests.test_dynamic_endpoints import FakeSession, make_tool

SECRET = "s3cr3t-value-123"


def test_tool_calls_log_argument_names_not_values(caplog):
    app = FastAPI()
    app.state.session = FakeSession([make_tool("echo")])
    asyncio.run(create_dynamic_endpoints(app))

    with caplog.at_level(logging.DEBUG):
        TestClient(app).post("/echo", json={"value": SECRET})

    assert "echo" in caplog.text
    assert "value" in caplog.text
    assert SECRET not in caplog.text


def test_hot_reload_logs_configs_without_secret_values(tmp_path, monkeypatch, caplog):
    config = {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", f"/data/{SECRET}"],
        "env": {"TOKEN": SECRET},
        "headers": {"Authorization": f"Bearer {SECRET}"},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": {}}))
    app = FastAPI()
    reloader = MCPHotReloader(app, str(path))
    reloader.current_config = {"mcpServers": {}}
    path.write_text(json.dumps({"mcpServers": {"files": config}}))

    async def no_session(*_args):
        return None

    async def no_connect(*_args):
        raise RuntimeError("not connecting in tests")

    monkeypatch.setattr(reloader, "_initialize_server_session_blocking", no_connect)
    monkeypatch.setattr(reloader, "_initialize_server_session", no_session)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(reloader._reload_servers())

    assert "files" in caplog.text
    assert SECRET not in caplog.text


def test_startup_logs_config_servers_without_args(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"mcpServers": {"files": {"command": "npx", "args": ["-y", "pkg", f"--token={SECRET}"]}}})
    )
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(path))

    async def scenario():
        with caplog.at_level(logging.DEBUG):
            build_app(api_key="k", cors_allow_origins=[], config_path=str(path))

    asyncio.run(scenario())
    assert "files" in caplog.text
    assert SECRET not in caplog.text
