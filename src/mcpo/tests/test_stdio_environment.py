import asyncio
import json

import pytest
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


POOL_WRAPPER = ["/usr/local/sbin/nova-run-tool", "--uid={uid}", "--server={server_id}", "--"]


@pytest.fixture
def uid_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("MCPO_STDIO_WRAPPER", json.dumps(POOL_WRAPPER))
    monkeypatch.setenv("MCPO_STDIO_UID_POOL", "20000:10000")
    monkeypatch.setenv("MCPO_STDIO_UID_MAP", str(tmp_path / "stdio_uids.json"))
    return tmp_path / "stdio_uids.json"


def test_each_server_gets_its_own_uid(uid_pool):
    _, args_a = mcp_management.stdio_command("npx", ["-y", "pkg-a"], server_id="a")
    _, args_b = mcp_management.stdio_command("npx", ["-y", "pkg-b"], server_id="b")

    uid_a = mcp_management.stdio_uid("a", "npx pkg-a")
    uid_b = mcp_management.stdio_uid("b", "npx pkg-b")
    assert uid_a != uid_b
    assert 20000 <= uid_a < 30000 and 20000 <= uid_b < 30000
    assert args_a[:3] == [f"--uid={uid_a}", "--server=a", "--"]
    assert args_b[0] == f"--uid={uid_b}"


def test_assignments_persist_owner_only(uid_pool):
    import stat

    uid = mcp_management.stdio_uid("a", "npx pkg-a")

    stored = json.loads(uid_pool.read_text())
    assert stored["assigned"]["a"]["uid"] == uid
    assert stat.S_IMODE(uid_pool.stat().st_mode) == 0o600
    assert mcp_management.stdio_uid("a", "npx pkg-a") == uid


def test_an_id_that_hashes_to_a_taken_uid_gets_the_next_free_one(uid_pool, monkeypatch):
    monkeypatch.setattr(mcp_management, "_uid_hash", lambda _server_id: 7)

    uid_a = mcp_management.stdio_uid("a", "npx pkg")
    uid_b = mcp_management.stdio_uid("b", "npx pkg")

    assert (uid_a, uid_b) == (20007, 20008)


def test_a_deleted_servers_uid_is_never_reassigned(uid_pool, monkeypatch):
    monkeypatch.setattr(mcp_management, "_uid_hash", lambda _server_id: 7)
    old = mcp_management.stdio_uid("a", "npx pkg")

    mcp_management.retire_stdio_uid("a")

    assert mcp_management.stdio_uid("c", "npx pkg") != old
    readded = mcp_management.stdio_uid("a", "npx pkg")
    assert readded not in (old, mcp_management.stdio_uid("c", "npx pkg"))


def test_a_different_package_under_the_same_id_gets_a_fresh_uid(uid_pool):
    first = mcp_management.stdio_uid("a", "npx pkg-one")

    second = mcp_management.stdio_uid("a", "npx pkg-two")

    assert second != first
    assert first in json.loads(uid_pool.read_text())["retired"]


def test_the_package_is_the_command_and_first_non_flag_argument():
    assert mcp_management.stdio_package("npx", ["-y", "@scope/pkg@1.0", "/workspace"]) == "npx @scope/pkg@1.0"


def test_an_exhausted_pool_is_refused(uid_pool, monkeypatch):
    monkeypatch.setenv("MCPO_STDIO_UID_POOL", "20000:1")
    mcp_management.stdio_uid("a", "npx pkg")

    with pytest.raises(ValueError, match="uid"):
        mcp_management.stdio_uid("b", "npx pkg")


def test_deleting_a_server_retires_its_uid(uid_pool, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mcpServers": {"a": {"type": "sse", "url": "http://x/"}}}))
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(config))
    monkeypatch.setattr(mcp_management, "MARKETPLACE_CONFIG_PATH", str(tmp_path / "m.json"))
    uid = mcp_management.stdio_uid("a", "npx pkg")
    app = FastAPI()
    mcp_management.apply_monkey_patch(app)

    assert TestClient(app).delete("/api/mcp/servers/a").status_code == 200
    assert uid in json.loads(uid_pool.read_text())["retired"]


def test_a_wrapper_with_uid_needs_a_pool(monkeypatch):
    monkeypatch.setenv("MCPO_STDIO_WRAPPER", json.dumps(POOL_WRAPPER))
    monkeypatch.delenv("MCPO_STDIO_UID_POOL", raising=False)

    with pytest.raises(ValueError, match="MCPO_STDIO_UID_POOL"):
        mcp_management.stdio_command("npx", ["pkg"], server_id="a")
