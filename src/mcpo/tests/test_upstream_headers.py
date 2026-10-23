import asyncio
import json
from contextlib import asynccontextmanager

import mcp.client.sse
import mcp.client.streamable_http
import pytest
from fastapi import FastAPI
from starlette.routing import Mount

import mcpo.main
from mcpo import mcp_management
from mcpo.hot_reload import MCPHotReloader
from mcpo.main import build_app

HEADERS = {"Authorization": "Bearer upstream-token"}


class Captured(Exception):
    pass


def capturing_client(seen):
    @asynccontextmanager
    async def client(*args, **kwargs):
        seen.append(kwargs)
        raise Captured()
        yield  # pragma: no cover

    return client


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "http": {"type": "streamable_http", "url": "http://backend:8080/mcp", "headers": HEADERS},
                    "events": {"type": "sse", "url": "http://backend:8080/sse", "headers": HEADERS},
                }
            }
        )
    )
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(path))
    return path


def sub_app(app, name):
    for route in app.routes:
        if isinstance(route, Mount) and route.path == f"/{name}":
            return route.app
    raise AssertionError(f"{name} is not mounted")


async def enter_lifespan(app):
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)


def test_streamable_http_server_gets_its_configured_headers(config_path, monkeypatch):
    seen = []
    monkeypatch.setattr(mcpo.main, "streamablehttp_client", capturing_client(seen))

    async def scenario():
        app = build_app(api_key="k", cors_allow_origins=[], config_path=str(config_path))
        with pytest.raises(Captured):
            await enter_lifespan(sub_app(app, "http"))

    asyncio.run(scenario())
    assert seen and seen[0].get("headers") == HEADERS


def test_sse_server_gets_its_configured_headers(config_path, monkeypatch):
    seen = []
    monkeypatch.setattr(mcpo.main, "sse_client", capturing_client(seen))

    async def scenario():
        app = build_app(api_key="k", cors_allow_origins=[], config_path=str(config_path))
        await enter_lifespan(sub_app(app, "events"))

    asyncio.run(scenario())
    assert seen and seen[0].get("headers") == HEADERS


@pytest.mark.parametrize(
    ("module", "config"),
    [
        (mcp.client.streamable_http, {"type": "streamable_http", "url": "http://backend:8080/mcp", "headers": HEADERS}),
        (mcp.client.sse, {"type": "sse", "url": "http://backend:8080/sse", "headers": HEADERS}),
    ],
)
def test_hot_added_server_gets_its_configured_headers(config_path, monkeypatch, module, config):
    seen = []
    name = "streamablehttp_client" if module is mcp.client.streamable_http else "sse_client"
    monkeypatch.setattr(module, name, capturing_client(seen))
    app = FastAPI()
    reloader = MCPHotReloader(app, str(config_path))
    monkeypatch.setattr(reloader, "_initialize_server_session", lambda *a: asyncio.sleep(0))

    async def scenario():
        await reloader._start_server("added", config)

    asyncio.run(scenario())
    assert seen and seen[0].get("headers") == HEADERS
