import asyncio
import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcpo import mcp_management
from mcpo.utils import auth

API_KEY = "test-key-0123456789"
UI_ORIGIN = "http://localhost:10001"

TEMPLATES = {
    "memory": {
        "config_template": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
            "env": {},
        }
    },
    "github": {
        "config_template": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "{{GITHUB_TOKEN}}"},
        },
        "user_configurable": {"GITHUB_TOKEN": {"type": "string"}},
    },
    "filesystem": {
        "config_template": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "{{PATHS}}"],
            "env": {},
        },
        "user_configurable": {"PATHS": {"type": "path_list"}},
    },
}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"backend": {"type": "streamable_http", "url": "http://backend:8080/mcp"}}})
    )
    monkeypatch.setattr(mcp_management, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(
        mcp_management, "MARKETPLACE_CONFIG_PATH", str(tmp_path / "marketplace.json")
    )
    monkeypatch.setattr(mcp_management, "load_marketplace_templates", lambda: TEMPLATES)
    return config_path


def build(config_path, api_key=API_KEY, origins=(UI_ORIGIN,)):
    from mcpo.main import build_app

    async def _build():
        return build_app(
            api_key=api_key,
            cors_allow_origins=list(origins),
            config_path=str(config_path),
        )

    return asyncio.run(_build())


def bearer(key=API_KEY):
    return {"Authorization": f"Bearer {key}"}


def management_app():
    app = FastAPI()
    mcp_management.apply_monkey_patch(app)
    return TestClient(app)


# --- auth -----------------------------------------------------------------


def test_management_api_rejects_requests_without_a_key(stores):
    client = TestClient(build(stores))

    assert client.get("/api/mcp/servers").status_code == 401
    assert client.get("/api/environment/variables").status_code == 401
    assert client.post("/api/mcp/servers/x", json={"type": "sse", "url": "http://a/"}).status_code == 401


def test_management_api_rejects_a_wrong_key(stores):
    client = TestClient(build(stores))

    assert client.get("/api/mcp/servers", headers=bearer("wrong")).status_code == 403


def test_management_api_accepts_bearer_and_basic(stores):
    client = TestClient(build(stores))
    basic = base64.b64encode(f"operator:{API_KEY}".encode()).decode()

    assert client.get("/api/mcp/servers", headers=bearer()).status_code == 200
    assert client.get("/api/mcp/servers", headers={"Authorization": f"Basic {basic}"}).status_code == 200


def test_unauthorized_response_lets_a_browser_prompt_for_basic_auth(stores):
    response = TestClient(build(stores)).get("/")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_management_api_is_not_mounted_without_a_key(stores):
    client = TestClient(build(stores, api_key=""))

    assert client.get("/api/mcp/servers").status_code == 404
    assert client.post("/api/mcp/servers/x", json={"type": "sse", "url": "http://a/"}).status_code in (404, 405)


def test_keys_are_compared_in_constant_time(monkeypatch, stores):
    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    client = TestClient(build(stores))
    client.get("/api/mcp/servers", headers=bearer())

    assert calls


# --- CORS -----------------------------------------------------------------


def preflight(client, origin):
    return client.options(
        "/api/mcp/servers",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def test_cors_allows_only_the_configured_origin(stores):
    client = TestClient(build(stores))

    assert preflight(client, UI_ORIGIN).headers.get("access-control-allow-origin") == UI_ORIGIN
    assert "access-control-allow-origin" not in preflight(client, "https://evil.example").headers


def test_cors_never_sends_credentials_with_a_wildcard(stores):
    client = TestClient(build(stores, origins=("*",)))

    response = preflight(client, "https://evil.example")
    assert response.headers.get("access-control-allow-credentials") != "true"


def test_mounted_servers_do_not_widen_cors(stores):
    stores.write_text(
        json.dumps({"mcpServers": {"remote": {"type": "streamable_http", "url": "http://backend:8080/mcp"}}})
    )
    client = TestClient(build(stores))

    response = client.get("/remote/openapi.json", headers={**bearer(), "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


# --- command allow-list ----------------------------------------------------


def test_add_rejects_a_command_that_is_not_a_verified_template(stores):
    response = management_app().post(
        "/api/mcp/servers/evil", json={"type": "stdio", "command": "bash", "args": ["-c", "id"]}
    )

    assert response.status_code == 400
    assert "evil" not in json.loads(stores.read_text())["mcpServers"]


def test_add_rejects_a_verified_command_with_other_packages(stores):
    response = management_app().put(
        "/api/mcp/servers/evil",
        json={"type": "stdio", "command": "npx", "args": ["-y", "evil-package"]},
    )

    assert response.status_code == 400


def test_add_rejects_env_keys_the_template_does_not_declare(stores):
    response = management_app().post(
        "/api/mcp/servers/memory",
        json={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
            "env": {"NODE_OPTIONS": "--require /tmp/x.js"},
        },
    )

    assert response.status_code == 400


def test_add_accepts_a_verified_template(stores):
    client = management_app()

    memory = client.post(
        "/api/mcp/servers/memory",
        json={"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
    )
    github = client.post(
        "/api/mcp/servers/github",
        json={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"},
        },
    )
    files = client.post(
        "/api/mcp/servers/files",
        json={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data/a", "/data/b"],
        },
    )

    assert (memory.status_code, github.status_code, files.status_code) == (200, 200, 200)


def test_add_accepts_a_url_server(stores):
    response = management_app().post(
        "/api/mcp/servers/remote", json={"type": "streamablehttp", "url": "http://backend:8080/mcp"}
    )

    assert response.status_code == 200


def test_add_rejects_a_server_id_that_is_not_a_plain_name(stores):
    response = management_app().post(
        "/api/mcp/servers/bad.name", json={"type": "sse", "url": "http://a/"}
    )

    assert response.status_code == 400


def test_custom_marketplace_install_rejects_an_unverified_command(stores, tmp_path):
    (tmp_path / "marketplace.json").write_text(
        json.dumps({"evil": {"config_template": {"type": "stdio", "command": "bash", "args": ["-c", "id"]}}})
    )

    response = management_app().post("/api/marketplace/install/evil", json={})

    assert response.status_code == 400
    assert "evil" not in json.loads(stores.read_text())["mcpServers"]


def test_template_install_rejects_extra_environment_variables(stores):
    response = management_app().post(
        "/api/marketplace/install/memory",
        json={"environment_vars": {"NODE_OPTIONS": "--require /tmp/x.js"}},
    )

    assert response.status_code == 400


def start_hot_added_server(app, server_id, config, monkeypatch):
    from mcpo.hot_reload import MCPHotReloader

    reloader = MCPHotReloader(app, mcp_management.CONFIG_PATH)

    async def ready(sub_app, _server_id):
        sub_app.state.session = object()

    async def no_verify(_server_id, _sub_app):
        return None

    monkeypatch.setattr(reloader, "_initialize_server_session_blocking", ready)
    monkeypatch.setattr(reloader, "_verify_tools_or_force_reload", no_verify)
    asyncio.run(reloader._start_server(server_id, config))
    return reloader


def test_hot_added_servers_do_not_widen_cors(stores, monkeypatch):
    app = build(stores)
    start_hot_added_server(app, "added", {"type": "streamable_http", "url": "http://x/mcp"}, monkeypatch)

    response = TestClient(app).get("/added/openapi.json", headers={**bearer(), "Origin": "https://evil.example"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
