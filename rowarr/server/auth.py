"""Login with Plex (PIN flow) — owner-only sessions, signed httpOnly cookie, CSRF header.

No password ever touches Rowarr: the PIN is created against plex.tv, the user approves it in
their Plex app/browser, and the resulting token's account must match the linked server's
owner. The Plex token from auth is used once to identify the account and (during setup)
stored encrypted; it is never logged.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from loguru import logger

PLEXTV = "https://plex.tv"
PRODUCT = "Rowarr"
SESSION_COOKIE = "rowarr_session"
SESSION_MAX_AGE_S = 14 * 24 * 3600
CSRF_HEADER = "x-rowarr-csrf"

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_headers(client_id: str) -> dict[str, str]:
    return {
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": client_id,
        "Accept": "application/json",
    }


def session_serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="rowarr-session")


def read_session(request: Request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return session_serializer(request.app.state.session_secret).loads(raw, max_age=SESSION_MAX_AGE_S)
    except BadSignature:
        return None


def require_owner(request: Request) -> dict:
    """Dependency: a valid session belonging to the SERVER OWNER; mutations need the CSRF header.

    Owner-ness is re-checked on every request (not just at login): a session issued during
    the pre-link setup window loses all access the moment a different account links a server.
    """
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="not signed in — use Login with Plex")
    owner_id = request.app.state.owner_account_id()
    if owner_id is not None and session.get("account_id") != owner_id:
        raise HTTPException(status_code=403, detail="only the server owner can use Rowarr")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")
    return session


@router.post("/pin")
async def create_pin(request: Request) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{PLEXTV}/api/v2/pins",
            params={"strong": "true"},
            headers=_client_headers(request.app.state.client_id),
            timeout=15,
        )
    r.raise_for_status()
    data = r.json()
    return {"id": data["id"], "code": data["code"], "client_id": request.app.state.client_id}


@router.get("/pin/{pin_id}")
async def poll_pin(pin_id: int, request: Request, response: Response) -> dict:
    """Poll the PIN; once linked, verify the account is the server owner and set the session."""
    state = request.app.state
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{PLEXTV}/api/v2/pins/{pin_id}", headers=_client_headers(state.client_id), timeout=15)
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="PIN expired — start over")
        r.raise_for_status()
        token = r.json().get("authToken")
        if not token:
            return {"linked": False}
        account = await client.get(
            f"{PLEXTV}/api/v2/user", headers={**_client_headers(state.client_id), "X-Plex-Token": token}, timeout=15
        )
    account.raise_for_status()
    info = account.json()
    account_id = int(info["id"])

    owner_id = state.owner_account_id()
    if owner_id is not None and account_id != owner_id:
        logger.warning("login rejected: account {} is not the server owner", account_id)
        raise HTTPException(status_code=403, detail="only the server owner can sign in to Rowarr")

    payload = {"account_id": account_id, "username": info.get("username") or info.get("title") or ""}
    cookie = session_serializer(state.session_secret).dumps(payload)
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    # During first-time setup the authenticated account becomes the owner candidate and the
    # token is returned ONCE so the wizard can probe/link servers. After a server is linked,
    # the token never leaves the backend again (XSS on the SPA must not be able to steal it).
    response_body = {"linked": True, "account_id": account_id, "username": payload["username"]}
    if owner_id is None:
        response_body["token"] = token
    return response_body


@router.get("/session")
async def get_session(request: Request) -> dict:
    session = read_session(request)
    if session is None:
        return {"authenticated": False}
    return {"authenticated": True, **session}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}
