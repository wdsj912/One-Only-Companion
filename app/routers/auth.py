"""Login/logout for the single-user access token."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.security import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    get_auth_token,
    token_matches,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@router.post("/login")
async def login(payload: LoginRequest, response: Response) -> dict:
    if not token_matches(payload.token):
        raise HTTPException(status_code=401, detail="invalid token")
    response.set_cookie(
        COOKIE_NAME,
        get_auth_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/status")
async def status(request: Request) -> dict:
    """Public: tells the page whether to show the login panel."""
    authenticated = token_matches(request.cookies.get(COOKIE_NAME)) or token_matches(
        request.headers.get("X-API-Token")
    )
    return {"authenticated": authenticated}
