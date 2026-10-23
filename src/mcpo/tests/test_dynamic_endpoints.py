import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcpo.main import create_dynamic_endpoints


class FakeSession:
    def __init__(self, tools):
        self._tools = tools
        self.calls = []

    async def initialize(self):
        return SimpleNamespace(serverInfo=None)

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text="ok")],
        )


def make_tool(name="echo"):
    return SimpleNamespace(
        name=name,
        description="Echo a value",
        inputSchema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        outputSchema=None,
    )


def test_listed_tool_becomes_a_post_route():
    app = FastAPI()
    app.state.session = FakeSession([make_tool("echo")])

    asyncio.run(create_dynamic_endpoints(app))

    posts = {r.path for r in app.routes if "POST" in getattr(r, "methods", set())}
    assert "/echo" in posts


def test_tool_route_calls_the_session():
    app = FastAPI()
    session = FakeSession([make_tool("echo")])
    app.state.session = session

    asyncio.run(create_dynamic_endpoints(app))
    response = TestClient(app).post("/echo", json={"value": "hi"})

    assert response.status_code == 200
    assert session.calls == [("echo", {"value": "hi"})]


def test_openapi_fetched_before_registration_lists_tools_after_it():
    app = FastAPI()
    app.state.session = FakeSession([make_tool("echo")])
    client = TestClient(app)

    before = client.get("/openapi.json").json()["paths"]
    asyncio.run(create_dynamic_endpoints(app))
    after = client.get("/openapi.json").json()["paths"]

    assert "/echo" not in before
    assert "/echo" in after


def test_openapi_reflects_a_changed_tool_list():
    app = FastAPI()
    client = TestClient(app)
    app.state.session = FakeSession([make_tool("echo")])
    asyncio.run(create_dynamic_endpoints(app))
    client.get("/openapi.json")

    app.state.session = FakeSession([make_tool("echo"), make_tool("shout")])
    asyncio.run(create_dynamic_endpoints(app))

    assert "/shout" in client.get("/openapi.json").json()["paths"]


def echo_tool(required_field):
    tool = make_tool("echo")
    tool.inputSchema = {"type": "object", "properties": {required_field: {"type": "string"}}, "required": [required_field]}
    return tool


def test_reconnects_replace_routes_instead_of_adding_them():
    app = FastAPI()
    for _ in range(5):
        app.state.session = FakeSession([make_tool("echo"), make_tool("shout")])
        asyncio.run(create_dynamic_endpoints(app))

    tool_routes = [r.path for r in app.routes if "POST" in getattr(r, "methods", set())]
    assert sorted(tool_routes) == ["/echo", "/shout"]


def test_after_a_reconnect_the_new_schema_is_the_one_validated():
    app = FastAPI()
    app.state.session = FakeSession([echo_tool("old")])
    asyncio.run(create_dynamic_endpoints(app))
    session = FakeSession([echo_tool("new")])
    app.state.session = session
    asyncio.run(create_dynamic_endpoints(app))
    client = TestClient(app)

    assert client.post("/echo", json={"new": "x"}).status_code == 200
    assert client.post("/echo", json={"old": "x"}).status_code == 422
