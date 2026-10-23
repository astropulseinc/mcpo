from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import base64
import hmac
import logging

from passlib.context import CryptContext
from datetime import UTC, datetime, timedelta

import jwt
from typing import Optional, Union, List, Dict


logger = logging.getLogger(__name__)

ALGORITHM = "HS256"

bearer_security = HTTPBearer(auto_error=False)

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
CSRF_HEADER = "X-Requested-With"

# Basic first, so a browser opening the UI prompts for the key as the password.
WWW_AUTHENTICATE = 'Basic realm="mcpo", Bearer'


def key_matches(candidate: str, api_key: str) -> bool:
    """Compare a presented key with the configured one in constant time."""
    return hmac.compare_digest(candidate.encode("utf-8"), api_key.encode("utf-8"))


def any_key_matches(candidate: str, keys) -> bool:
    """True when the candidate matches one of the configured keys."""
    return any(key_matches(candidate, key) for key in keys if key)


# Routes only the management key may use; every other path is a mounted
# MCP server, where the tool key is accepted too.
MANAGEMENT_SEGMENTS = {"api", "static", "docs", "redoc", "openapi.json"}


def is_management_path(path: str) -> bool:
    """True for / and for paths whose first segment is a management route."""
    first = path.lstrip("/").split("/", 1)[0]
    return first == "" or first in MANAGEMENT_SEGMENTS


def read_key_file(path: str) -> str:
    """The key stored in a file (surrounding whitespace removed); refuses an empty file."""
    with open(path, "r", encoding="utf-8") as handle:
        key = handle.read().strip()
    if not key:
        raise ValueError(f"key file {path} is empty")
    return key


def get_verify_api_key(api_key: str, tool_api_key: str | None = None):
    async def verify_api_key(
        authorization: HTTPAuthorizationCredentials = Depends(bearer_security),
    ):
        if not authorization or not authorization.credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid Authorization header",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization.credentials
        if not any_key_matches(token, (api_key, tool_api_key)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid API key",
            )

    return verify_api_key


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces Basic or Bearer token authentication for all requests.
    """

    def __init__(self, app, api_key: str, tool_api_key: str | None = None):
        super().__init__(app)
        self.api_key = api_key
        self.tool_api_key = tool_api_key

    def _keys_for(self, path: str):
        if is_management_path(path):
            return (self.api_key,)
        return (self.api_key, self.tool_api_key)

    async def dispatch(self, request: Request, call_next):
        # Skip authentication for OPTIONS requests
        if request.method == "OPTIONS":
            return await call_next(request)

        # Get authorization header
        authorization = request.headers.get("Authorization")

        # Verify API key
        try:
            # Use the same function that the dependency uses
            if not authorization:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid Authorization header"},
                    headers={"WWW-Authenticate": WWW_AUTHENTICATE},
                )

            # Handle Bearer token auth
            if authorization.startswith("Bearer "):
                token = authorization[7:]  # Remove "Bearer " prefix
                if not any_key_matches(token, self._keys_for(request.url.path)):
                    return JSONResponse(
                        status_code=403, content={"detail": "Invalid API key"}
                    )
            # Handle Basic auth
            elif authorization.startswith("Basic "):
                # Decode the base64 credentials
                credentials = authorization[6:]  # Remove "Basic " prefix
                try:
                    decoded = base64.b64decode(credentials).decode("utf-8")
                    # Basic auth format is username:password
                    username, password = decoded.split(":", 1)
                    # Any username is allowed, but password must match api_key
                    if not any_key_matches(password, self._keys_for(request.url.path)):
                        return JSONResponse(
                            status_code=403, content={"detail": "Invalid credentials"}
                        )
                    # A browser sends cached Basic credentials on cross-site
                    # requests too; a custom header cannot be added cross-site
                    # without a CORS preflight, so changes must carry one.
                    if request.method not in SAFE_METHODS and not request.headers.get(CSRF_HEADER):
                        return JSONResponse(
                            status_code=403,
                            content={"detail": f"Changes with Basic auth need the {CSRF_HEADER} header"},
                        )
                except Exception:
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Invalid Basic Authentication format"},
                        headers={"WWW-Authenticate": WWW_AUTHENTICATE},
                    )
            else:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unsupported authorization method"},
                    headers={"WWW-Authenticate": WWW_AUTHENTICATE},
                )

            return await call_next(request)
        except Exception:
            logger.exception("Request failed behind the API-key middleware")
            return JSONResponse(status_code=500, content={"detail": "Internal error"})


# def create_token(data: dict, expires_delta: Union[timedelta, None] = None) -> str:
#     payload = data.copy()

#     if expires_delta:
#         expire = datetime.now(UTC) + expires_delta
#         payload.update({"exp": expire})

#     encoded_jwt = jwt.encode(payload, SESSION_SECRET, algorithm=ALGORITHM)
#     return encoded_jwt


# def decode_token(token: str) -> Optional[dict]:
#     try:
#         decoded = jwt.decode(token, SESSION_SECRET, algorithms=[ALGORITHM])
#         return decoded
#     except Exception:
#         return None
