"""A destructive tool runs only after a person approved that exact call, once."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import types

from mcpo import approvals
from mcpo.main import build_app, create_dynamic_endpoints


class FakeSession:
    def __init__(self, tools):
        self._tools = tools
        self.calls = []

    async def initialize(self):
        return SimpleNamespace(serverInfo=None)

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments, None))
        return SimpleNamespace(isError=False, content=[types.TextContent(type="text", text="done")])

    async def send_request(self, request, result_type):
        params = request.root.params
        meta = params.meta.model_extra if params.meta else None
        self.calls.append((params.name, params.arguments, meta))
        return SimpleNamespace(isError=False, content=[types.TextContent(type="text", text="done")])


def tool(name, annotations=None, meta=None):
    return SimpleNamespace(
        name=name,
        description=name,
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "confirmed": {"type": "boolean", "default": False}},
            "required": ["name"],
        },
        outputSchema=None,
        annotations=annotations,
        meta=meta,
    )


DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}
PREVIEW_FIRST = {approvals.READ_ONLY_WHEN_META_KEY: [{"confirmed": [False, None]}]}


@pytest.fixture
def stack():
    approvals.configure(b"k" * 32)
    app = FastAPI()
    app.include_router(approvals.management_router())
    session = FakeSession([
        tool("app_delete", DESTRUCTIVE, PREVIEW_FIRST),
        tool("app_info", {"readOnlyHint": True, "destructiveHint": False}),
        tool("third_party"),
    ])
    app.state.session = session
    asyncio.run(create_dynamic_endpoints(app))
    yield TestClient(app), session
    approvals.configure(None)


def held(client, args):
    response = client.post("/app_delete", json=args)
    assert response.status_code == 428
    return response.json()["detail"]["pending_approval_id"]


def approve(client, pending_id, user=""):
    response = client.post(f"/api/v1/approvals/{pending_id}/approve", json={"user": user})
    assert response.status_code == 200
    return response.json()["approval"]


def test_tool_approval_a_destructive_call_without_approval_gets_428(stack):
    client, session = stack
    response = client.post("/app_delete", json={"name": "web", "confirmed": True})

    assert response.status_code == 428
    pending_id = response.json()["detail"]["pending_approval_id"]
    listed = client.get("/api/v1/approvals").json()["pending"]
    assert [(p["id"], p["tool"], p["arguments"]) for p in listed] == [
        (pending_id, "app_delete", {"name": "web", "confirmed": True})
    ]
    assert session.calls == []


def test_tool_approval_a_valid_approval_runs_once_and_its_replay_is_refused(stack):
    client, session = stack
    args = {"name": "web", "confirmed": True}
    token = approve(client, held(client, args))

    ran = client.post("/app_delete", json=args, headers={approvals.APPROVAL_HEADER: token})
    assert ran.status_code == 200
    assert ran.json() == {"untrusted_tool_output": "done"}
    assert session.calls == [("app_delete", args, {approvals.APPROVAL_META_KEY: token})]

    replay = client.post("/app_delete", json=args, headers={approvals.APPROVAL_HEADER: token})
    assert replay.status_code == 403
    assert "already used" in replay.json()["detail"]["message"]
    assert len(session.calls) == 1


def test_tool_approval_changed_args_break_the_approval(stack):
    client, session = stack
    token = approve(client, held(client, {"name": "web", "confirmed": True}))

    other = client.post("/app_delete", json={"name": "db", "confirmed": True},
                        headers={approvals.APPROVAL_HEADER: token})
    assert other.status_code == 403
    assert "different call" in other.json()["detail"]["message"]
    assert session.calls == []


def test_tool_approval_a_forged_approval_is_refused(stack):
    client, session = stack
    forged = approvals.ApprovalStore(b"x" * 32)
    pending = forged.hold("app_delete", "/app_delete", {"name": "web", "confirmed": True}, "")
    _, token = forged.approve(pending.id, "")

    response = client.post("/app_delete", json={"name": "web", "confirmed": True},
                           headers={approvals.APPROVAL_HEADER: token})
    assert response.status_code == 403
    assert session.calls == []


def test_tool_approval_an_approval_bound_to_a_user_needs_that_user(stack):
    client, _ = stack
    response = client.post("/app_delete", json={"name": "web", "confirmed": True},
                           headers={approvals.USER_HEADER: "alice"})
    pending_id = response.json()["detail"]["pending_approval_id"]

    assert client.post(f"/api/v1/approvals/{pending_id}/approve", json={"user": "mallory"}).status_code == 403
    token = approve(client, pending_id, user="alice")
    without_user = client.post("/app_delete", json={"name": "web", "confirmed": True},
                               headers={approvals.APPROVAL_HEADER: token})
    assert without_user.status_code == 403


def test_tool_approval_read_only_tools_and_previews_are_unaffected(stack):
    client, session = stack
    assert client.post("/app_info", json={"name": "web"}).status_code == 200
    assert client.post("/third_party", json={"name": "web"}).status_code == 200
    assert client.post("/app_delete", json={"name": "web"}).status_code == 200
    assert [name for name, _, _ in session.calls] == ["app_info", "third_party", "app_delete"]
    assert client.get("/api/v1/approvals").json()["pending"] == []


def test_tool_approval_the_tool_key_cannot_reach_the_approval_api():
    app = build_app(api_key="manage", tool_api_key="tools", server_type="streamablehttp",
                    server_command=["http://backend/mcp"])
    client = TestClient(app)

    assert client.get("/api/v1/approvals", headers={"Authorization": "Bearer tools"}).status_code == 403
    assert client.get("/api/v1/approvals", headers={"Authorization": "Bearer manage"}).status_code == 200
    approvals.configure(None)
