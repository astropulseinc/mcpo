"""
Human approval for tool calls that change something.

A tool whose MCP annotations say it may write destructively (readOnlyHint not true,
destructiveHint not false) is not called on the model's word. Its call is answered 428 with a
pending-approval id and kept here. A person approves it through the management API (the Nova
UI's Approve action), which signs ``{tool, args_hash, user, exp, nonce}`` with a key the model
never sees. The call is then repeated with that token in ``X-Nova-Approval``: it runs once, for
exactly the approved arguments, within five minutes. The token also travels to the MCP server
in the call's ``_meta``, so a server behind Nova can check it again.

A tool may list, in its ``_meta`` under ``READ_ONLY_WHEN_META_KEY``, argument values under which
a call changes nothing (a preview: ``{"confirmed": [false, null]}``). Those calls run freely.
Tools without annotations are not gated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, HTTPException

APPROVAL_HEADER = "X-Nova-Approval"
USER_HEADER = "X-OpenWebUI-User-Id"
APPROVAL_META_KEY = "io.astropulse/approval"
READ_ONLY_WHEN_META_KEY = "io.astropulse/readOnlyWhen"
# The key the MCP servers check approvals with is derived from the management key with this label.
KEY_DERIVATION_LABEL = b"nova-tool-approval-v1"
APPROVAL_TTL_SECONDS = 300
MAX_PENDING = 256


def derive_key(api_key: str) -> bytes:
    return hmac.new(api_key.encode("utf-8"), KEY_DERIVATION_LABEL, hashlib.sha256).digest()


def args_hash(arguments: dict) -> str:
    canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _hint(annotations: Any, name: str) -> Any:
    if annotations is None:
        return None
    if isinstance(annotations, dict):
        return annotations.get(name)
    return getattr(annotations, name, None)


@dataclass
class ApprovalPolicy:
    """When a tool's call needs a person's approval."""

    read_only_when: list = field(default_factory=list)

    def applies(self, arguments: dict) -> bool:
        for condition in self.read_only_when:
            if isinstance(condition, dict) and condition and all(
                arguments.get(name) in (values if isinstance(values, list) else [values])
                for name, values in condition.items()
            ):
                return False
        return True


def approval_policy(tool: Any) -> Optional[ApprovalPolicy]:
    """The policy for a listed MCP tool, or None when its calls are never held."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return None
    if _hint(annotations, "readOnlyHint") is True or _hint(annotations, "destructiveHint") is False:
        return None
    meta = getattr(tool, "meta", None) or {}
    conditions = meta.get(READ_ONLY_WHEN_META_KEY) if isinstance(meta, dict) else None
    return ApprovalPolicy(conditions if isinstance(conditions, list) else [])


@dataclass
class PendingCall:
    id: str
    tool: str
    path: str
    arguments: dict
    args_hash: str
    user: str
    expires_at: float

    def view(self, now: float) -> dict:
        return {
            "id": self.id,
            "tool": self.tool,
            "path": self.path,
            "arguments": self.arguments,
            "user": self.user,
            "expires_in": max(0, int(self.expires_at - now)),
        }


class ApprovalStore:
    """Calls waiting for a person, and the approvals already used."""

    def __init__(self, key: bytes, clock: Callable[[], float] = time.time):
        self._key = key
        self._clock = clock
        self._pending: dict[str, PendingCall] = {}
        self._used_nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for pending_id, pending in list(self._pending.items()):
            if pending.expires_at <= now:
                del self._pending[pending_id]
        for nonce, exp in list(self._used_nonces.items()):
            if exp <= now:
                del self._used_nonces[nonce]

    def hold(self, tool: str, path: str, arguments: dict, user: str) -> PendingCall:
        now = self._clock()
        with self._lock:
            self._prune(now)
            while len(self._pending) >= MAX_PENDING:
                oldest = min(self._pending.values(), key=lambda p: p.expires_at)
                del self._pending[oldest.id]
            pending = PendingCall(
                id=secrets.token_urlsafe(16), tool=tool, path=path, arguments=arguments,
                args_hash=args_hash(arguments), user=user, expires_at=now + APPROVAL_TTL_SECONDS,
            )
            self._pending[pending.id] = pending
            return pending

    def pending(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            calls = sorted(self._pending.values(), key=lambda p: p.expires_at, reverse=True)
            return [p.view(now) for p in calls]

    def approve(self, pending_id: str, approver: str) -> Optional[tuple[PendingCall, str]]:
        """Sign the held call; it leaves the pending list. None when it is unknown or expired."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            pending = self._pending.get(pending_id)
            if pending is None:
                return None
            if pending.user and approver != pending.user:
                raise PermissionError("only the person whose chat made the call can approve it")
            del self._pending[pending_id]
        payload = {
            "tool": pending.tool, "args_hash": pending.args_hash, "user": pending.user,
            "exp": now + APPROVAL_TTL_SECONDS, "nonce": secrets.token_urlsafe(16),
        }
        part = _b64(json.dumps(payload, sort_keys=True).encode("utf-8"))
        signature = _b64(hmac.new(self._key, part.encode("ascii"), hashlib.sha256).digest())
        return pending, f"v1.{part}.{signature}"

    def redeem(self, token: str, tool: str, arguments: dict, user: str) -> Optional[str]:
        """Use an approval for this call. Returns why it is refused, or None (it is then spent)."""
        try:
            version, part, signature = token.split(".")
            expected = hmac.new(self._key, part.encode("ascii"), hashlib.sha256).digest()
            if version != "v1" or not hmac.compare_digest(expected, _unb64(signature)):
                return "the approval is not valid"
            payload = json.loads(_unb64(part))
        except (ValueError, TypeError):
            return "the approval is not valid"
        if payload.get("tool") != tool or payload.get("args_hash") != args_hash(arguments):
            return "the approval was given for a different call"
        if payload.get("user", "") != user:
            return "the approval was given to someone else"
        now = self._clock()
        exp = float(payload.get("exp", 0))
        if exp <= now:
            return "the approval has expired"
        nonce = str(payload.get("nonce", ""))
        with self._lock:
            self._prune(now)
            if nonce in self._used_nonces:
                return "the approval was already used"
            self._used_nonces[nonce] = exp
        return None


_store: Optional[ApprovalStore] = None


def configure(key: Optional[bytes]) -> ApprovalStore:
    """Set the process-wide store (one for every mounted server, also hot-added ones)."""
    global _store
    _store = ApprovalStore(key or secrets.token_bytes(32))
    return _store


def get_store() -> ApprovalStore:
    return _store if _store is not None else configure(None)


def approval_required_detail(pending: PendingCall) -> dict:
    return {
        "message": (
            f"{pending.tool} changes infrastructure and waits for the user's approval. Tell the user "
            "to review the call and press Approve under your message. Do not call it again yourself."
        ),
        "pending_approval_id": pending.id,
        "expires_in": APPROVAL_TTL_SECONDS,
    }


def management_router() -> APIRouter:
    """The approval API. Mounted under /api, so only the management key reaches it."""
    router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

    @router.get("")
    async def list_pending():
        return {"pending": get_store().pending()}

    @router.post("/{pending_id}/approve")
    async def approve(pending_id: str, body: dict = Body(default_factory=dict)):
        approver = str(body.get("user") or "")
        try:
            approved = get_store().approve(pending_id, approver)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        if approved is None:
            raise HTTPException(status_code=404, detail="No such call waits for approval (it may have expired)")
        pending, token = approved
        return {
            "approval": token,
            "header": APPROVAL_HEADER,
            "tool": pending.tool,
            "path": pending.path,
            "arguments": pending.arguments,
            "user": pending.user,
            "expires_in": APPROVAL_TTL_SECONDS,
        }

    return router
