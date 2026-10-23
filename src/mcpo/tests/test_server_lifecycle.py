"""Deleting or replacing a server stops its process, whether it started at boot or by hot reload."""

import asyncio
import json
from contextlib import asynccontextmanager

from starlette.routing import Mount

import mcpo.main
from mcpo import mcp_management
from mcpo.main import build_app

SERVERS = {
    "one": {"command": "npx", "args": ["-y", "pkg-one"]},
    "two": {"command": "npx", "args": ["-y", "pkg-two"]},
}


def fake_stdio(events):
    @asynccontextmanager
    async def client(params, *args, **kwargs):
        name = params.args[-1]
        events.append(("start", name))
        try:
            await asyncio.Event().wait()  # a running process until cancelled
            yield None, None
        finally:
            events.append(("stop", name))

    return client


def mounted(app):
    return {route.path for route in app.routes if isinstance(route, Mount) and route.path not in ("/static",)}


def run_startup(tmp_path, monkeypatch, scenario):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": SERVERS}))
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(path))
    monkeypatch.setattr(mcp_management, "MARKETPLACE_CONFIG_PATH", str(tmp_path / "m.json"))
    monkeypatch.setattr(mcp_management, "load_marketplace_templates", lambda: {
        name: {"config_template": cfg} for name, cfg in SERVERS.items()
    })
    events = []
    monkeypatch.setattr(mcpo.main, "stdio_client", fake_stdio(events))

    async def main():
        app = build_app(api_key="k", cors_allow_origins=[], config_path=str(path))
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
            await scenario(app, events)
            await asyncio.sleep(0.05)
        app.state.hot_reloader.stop()

    asyncio.run(main())
    return events


def test_startup_servers_are_registered_by_id(tmp_path, monkeypatch):
    seen = {}

    async def scenario(app, events):
        seen.update({k: len(v) for k, v in app.state.server_tasks.items()})

    run_startup(tmp_path, monkeypatch, scenario)
    assert seen == {"one": 1, "two": 1}


def test_stopping_a_startup_server_stops_its_process_and_only_its_own(tmp_path, monkeypatch):
    seen = {}

    async def scenario(app, events):
        await app.state.hot_reloader.stop_server("one")
        await asyncio.sleep(0.05)
        seen["mounted"] = mounted(app)
        seen["events"] = list(events)

    run_startup(tmp_path, monkeypatch, scenario)

    assert ("stop", "pkg-one") in seen["events"]
    assert ("stop", "pkg-two") not in seen["events"]
    assert "/one" not in seen["mounted"] and "/two" in seen["mounted"]


def test_delete_stops_the_process_before_retiring_its_uid(tmp_path, monkeypatch):
    order = []
    seen = {}

    async def scenario(app, events):
        reloader = app.state.hot_reloader
        real_stop = reloader.stop_server

        async def stop(server_id):
            await real_stop(server_id)
            order.append(("stopped", server_id))

        monkeypatch.setattr(reloader, "stop_server", stop)
        monkeypatch.setattr(mcp_management, "retire_stdio_uid", lambda sid: order.append(("retired", sid)))
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            seen["status"] = (await client.delete("/api/mcp/servers/one", headers={"Authorization": "Bearer k"})).status_code

    events = run_startup(tmp_path, monkeypatch, scenario)

    assert seen["status"] == 200
    assert order == [("stopped", "one"), ("retired", "one")]
    assert ("stop", "pkg-one") in events
