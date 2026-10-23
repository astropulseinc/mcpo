import asyncio
import json

from fastapi import FastAPI

from mcpo import mcp_management
from mcpo.hot_reload import MCPHotReloader
from mcpo.main import build_app

SECRET = "proxy-secret-value"
SERVER = {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "server-token"}}


def assert_minimal(env):
    assert env["TOKEN"] == "server-token"
    assert env.get("PATH")
    assert SECRET not in env.values()
    assert "NOVA_PROXY_API_KEY" not in env and "ASTROPULSE_API_KEY" not in env


def test_config_stdio_servers_do_not_inherit_the_proxy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_PROXY_API_KEY", SECRET)
    monkeypatch.setenv("ASTROPULSE_API_KEY", SECRET)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": {"tool": SERVER}}))
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(path))

    async def scenario():
        return build_app(api_key="k", cors_allow_origins=[], config_path=str(path))

    app = asyncio.run(scenario())
    sub = next(r.app for r in app.routes if getattr(r, "path", "") == "/tool")
    assert_minimal(sub.state.env)


def test_hot_added_stdio_servers_do_not_inherit_the_proxy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_PROXY_API_KEY", SECRET)
    monkeypatch.setenv("ASTROPULSE_API_KEY", SECRET)
    reloader = MCPHotReloader(FastAPI(), str(tmp_path / "config.json"))
    seen = {}

    async def capture(sub_app, _server_id):
        seen["env"] = sub_app.state.env
        raise RuntimeError("not connecting in tests")

    async def no_session(*_args):
        return None

    monkeypatch.setattr(reloader, "_initialize_server_session_blocking", capture)
    monkeypatch.setattr(reloader, "_initialize_server_session", no_session)
    asyncio.run(reloader._start_server("tool", SERVER))

    assert_minimal(seen["env"])
