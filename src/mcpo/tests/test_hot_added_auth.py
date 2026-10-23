import asyncio
import json

from fastapi.testclient import TestClient

from mcpo import mcp_management
from mcpo.hot_reload import MCPHotReloader
from mcpo.main import build_app, create_dynamic_endpoints
from mcpo.tests.test_dynamic_endpoints import FakeSession, make_tool

API_KEY = "hot-reload-key"


def test_hot_added_server_tools_require_the_api_key(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"mcpServers": {"backend": {"type": "streamable_http", "url": "http://b/mcp"}}}))
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(config_path))

    async def scenario():
        app = build_app(api_key=API_KEY, cors_allow_origins=[], config_path=str(config_path))
        reloader = MCPHotReloader(app, str(config_path))

        async def ready(sub_app, _server_id):
            sub_app.state.session = FakeSession([make_tool("echo")])
            await create_dynamic_endpoints(sub_app, api_dependency=sub_app.state.api_dependency)

        async def no_verify(_server_id, _sub_app):
            return None

        monkeypatch.setattr(reloader, "_initialize_server_session_blocking", ready)
        monkeypatch.setattr(reloader, "_verify_tools_or_force_reload", no_verify)
        await reloader._start_server("added", {"type": "streamable_http", "url": "http://x/mcp"})
        return reloader._sessions["added"]["sub_app"]

    sub_app = asyncio.run(scenario())
    client = TestClient(sub_app)

    assert client.post("/echo", json={"value": "hi"}).status_code == 401
    ok = client.post("/echo", json={"value": "hi"}, headers={"Authorization": f"Bearer {API_KEY}"})
    assert ok.status_code == 200
