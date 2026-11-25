"""Operator authentication.

POST /api/login checks the operators table with verify_password and returns a signed token.
`require_operator` protects the write endpoints and returns the operator's username.

Tokens are `<base64 payload>.<HMAC-SHA256 signature>` signed with API_SECRET_KEY (stdlib only).
The payload holds the username and an expiry time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from transit.api.deps import get_session
from transit.api.schemas import LoginRequest, LoginResponse
from transit.common.models import Operator
from transit.common.security import verify_password

TOKEN_TTL_SECONDS = 12 * 3600
router = APIRouter(tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


def _secret() -> bytes:
    return os.environ.get("API_SECRET_KEY", "change-me").encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def create_token(username: str, ttl: int = TOKEN_TTL_SECONDS, now: float | None = None) -> tuple[str, int]:
    expires = int((now or time.time()) + ttl)
    payload = _b64(json.dumps({"sub": username, "exp": expires}).encode())
    signature = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}", expires


def verify_token(token: str, now: float | None = None) -> str | None:
    """Return the username if the token is genuine and not expired."""
    try:
        payload, signature = token.split(".")
        expected = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("exp", 0) < (now or time.time()):
        return None
    return data.get("sub")


def require_operator(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    username = verify_token(credentials.credentials) if credentials else None
    if not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required",
                            headers={"WWW-Authenticate": "Bearer"})
    return username


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, session: Session = Depends(get_session)) -> LoginResponse:
    operator = session.scalar(select(Operator).where(Operator.username == body.username))
    if operator is None or not verify_password(body.password, operator.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    token, expires = create_token(operator.username)
    return LoginResponse(token=token, username=operator.username,
                         expires_at=datetime.fromtimestamp(expires, timezone.utc))


@router.get("/me")
def me(username: str = Depends(require_operator)) -> dict:
    return {"username": username}
