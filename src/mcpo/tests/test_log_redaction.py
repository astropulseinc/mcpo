import asyncio
import json
import logging
from contextlib import asynccontextmanager

import mcpo.main

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


def test_server_urls_are_logged_without_credentials_or_query(tmp_path, monkeypatch, caplog):
    url = f"http://user:{SECRET}@backend:8080/sse?token={SECRET}"
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": {"events": {"type": "sse", "url": url}}}))
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(path))

    @asynccontextmanager
    async def refuse(*_args, **_kwargs):
        raise RuntimeError("no connection in tests")
        yield  # pragma: no cover

    monkeypatch.setattr(mcpo.main, "sse_client", refuse)

    async def scenario():
        with caplog.at_level(logging.DEBUG):
            app = build_app(api_key="k", cors_allow_origins=[], config_path=str(path))
            sub = next(r.app for r in app.routes if getattr(r, "path", "") == "/events")
            async with sub.router.lifespan_context(sub):
                await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert "backend:8080/sse" in caplog.text
    assert SECRET not in caplog.text
