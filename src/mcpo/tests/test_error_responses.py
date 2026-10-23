import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcpo import environment_management, mcp_management
from mcpo.main import create_dynamic_endpoints
from mcpo.tests.test_dynamic_endpoints import FakeSession, make_tool
from mcpo.utils.auth import APIKeyMiddleware

DETAIL = "internal-detail-/opt/nova/secret-path"


class FailingSession(FakeSession):
    async def call_tool(self, name, arguments):
        raise RuntimeError(DETAIL)


@pytest.fixture
def management(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": {"events": {"type": "sse", "url": "http://169.254.169.254/latest/"}}}))
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(path))
    monkeypatch.setattr(mcp_management, "MARKETPLACE_CONFIG_PATH", str(tmp_path / "m.json"))
    monkeypatch.setattr(environment_management, "ENV_CONFIG_PATH", str(tmp_path / "env.json"))
    monkeypatch.setattr(mcp_management, "load_marketplace_templates", dict)
    app = FastAPI()
    mcp_management.apply_monkey_patch(app)
    environment_management.apply_environment_patch(app)
    return app


def test_tool_failure_does_not_return_exception_text():
    app = FastAPI()
    app.state.session = FailingSession([make_tool("echo")])
    asyncio.run(create_dynamic_endpoints(app))

    response = TestClient(app).post("/echo", json={"value": "x"})

    assert response.status_code == 500
    assert DETAIL not in response.text


def test_auth_middleware_does_not_return_exception_text():
    app = FastAPI()

    @app.get("/boom")
    async def boom():
        raise RuntimeError(DETAIL)

    app.add_middleware(APIKeyMiddleware, api_key="k")
    response = TestClient(app, raise_server_exceptions=False).get("/boom", headers={"Authorization": "Bearer k"})

    assert response.status_code == 500
    assert DETAIL not in response.text


def test_management_failures_do_not_return_exception_text(management, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError(DETAIL)

    monkeypatch.setattr(mcp_management, "save_config", broken)
    monkeypatch.setattr(environment_management, "save_environment_config", broken)
    client = TestClient(management)

    responses = [
        client.post("/api/mcp/servers/new", json={"type": "sse", "url": "http://a/"}),
        client.delete("/api/mcp/servers/events"),
        client.post("/api/environment/variables", json={"key": "K", "value": "v"}),
    ]

    for response in responses:
        assert response.status_code >= 400
        assert DETAIL not in response.text


def test_health_check_does_not_fetch_the_stored_url(management, monkeypatch):
    fetched = []

    class RecordingClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, **kwargs):
            fetched.append(url)
            raise httpx.ConnectError(DETAIL)

    monkeypatch.setattr(httpx, "Client", RecordingClient)
    sub_app = FastAPI()
    sub_app.state.session = object()
    management.mount("/events", sub_app)
    client = TestClient(management)

    test_result = client.get("/api/mcp/servers/events/test")
    health = client.get("/api/mcp/servers/events/health")

    assert fetched == []
    assert DETAIL not in test_result.text + health.text
