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


def test_cli_stdio_server_gets_only_its_given_environment(monkeypatch):
    monkeypatch.setenv("NOVA_PROXY_API_KEY", SECRET)

    async def scenario():
        return build_app(
            api_key="k",
            cors_allow_origins=[],
            server_type="stdio",
            server_command=["npx", "-y", "pkg"],
            server_env={"TOKEN": "server-token"},
        )

    assert_minimal(asyncio.run(scenario()).state.env)


WRAPPER = ["/usr/local/sbin/run-as-tools", "--reuid=1001", "--"]


def capturing_stdio(seen):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def client(params, *args, **kwargs):
        seen.append(params)
        raise RuntimeError("not connecting in tests")
        yield  # pragma: no cover

    return client


def test_config_stdio_servers_run_through_the_wrapper(tmp_path, monkeypatch):
    import mcpo.main

    monkeypatch.setenv("MCPO_STDIO_WRAPPER", json.dumps(WRAPPER))
    seen = []
    monkeypatch.setattr(mcpo.main, "stdio_client", capturing_stdio(seen))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": {"tool": SERVER}}))
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(path))

    async def scenario():
        app = build_app(api_key="k", cors_allow_origins=[], config_path=str(path))
        sub = next(r.app for r in app.routes if getattr(r, "path", "") == "/tool")
        try:
            async with sub.router.lifespan_context(sub):
                pass
        except RuntimeError:
            pass

    asyncio.run(scenario())
    assert seen[0].command == WRAPPER[0]
    assert seen[0].args == WRAPPER[1:] + ["npx", "-y", "pkg"]


def test_hot_added_stdio_servers_run_through_the_wrapper(tmp_path, monkeypatch):
    import mcp.client.stdio

    monkeypatch.setenv("MCPO_STDIO_WRAPPER", json.dumps(WRAPPER))
    seen = []
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", capturing_stdio(seen))
    reloader = MCPHotReloader(FastAPI(), str(tmp_path / "config.json"))

    async def no_session(*_args):
        return None

    monkeypatch.setattr(reloader, "_initialize_server_session", no_session)
    asyncio.run(reloader._start_server("tool", SERVER))

    assert seen[0].command == WRAPPER[0]
    assert seen[0].args == WRAPPER[1:] + ["npx", "-y", "pkg"]
