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


def _check_csrf(request: Request) -> None:
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")


def require_owner(request: Request) -> dict:
    """The owner, and nobody else. The default gate for everything except the setup wizard.

    An unclaimed instance has no owner, so this refuses everyone until a server is linked — which
    is correct for settings, runs, privacy, users and system: none of them make sense, or should
    be reachable, before setup is done. Only the wizard itself may run before there is an owner,
    and it uses `require_setup_access` for that.

    Owner-ness is re-checked on every request, not just at login: a session issued during the
    pre-link window loses all access the moment a different account links a server.
    """
    _check_csrf(request)
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="not signed in — use Login with Plex")
    owner_id = request.app.state.owner_account_id()
    if owner_id is None or session.get("account_id") != owner_id:
        raise HTTPException(status_code=403, detail="only the server owner can use Rowarr")
    return session


def require_setup_access(request: Request) -> dict:
    """Who may drive the setup wizard. Three states, and conflating the first two is how an earlier
    version of this became a way to steal the owner's Plex token:

    * **Empty** — no server linked AND no secret stored. Nothing to protect and nobody to protect
      it for, so it is open: a fresh install lands in the wizard instead of a login screen.
      Connecting Plex IS step 1, and it is what claims the instance.
    * **Holds secrets but unclaimed** — the environment can seed a real Plex/Tautulli/curator
      credential with no server row. "Nobody has claimed it" is NOT "there is nothing to steal": an
      anonymous caller here could point `/setup/probe` at a host they control and have Rowarr send
      them the seeded secret. So this requires a sign-in — any Plex account, because we do not yet
      know whose instance it is; whoever links the server becomes the owner.
    * **Claimed** — it belongs to the account that linked the server, and to nobody else.

    CSRF is required for mutations in every state — otherwise any page you visited could drive a
    stranger's wizard.
    """
    _check_csrf(request)
    session = read_session(request)
    owner_id = request.app.state.owner_account_id()
    if owner_id is not None:
        if session is None or session.get("account_id") != owner_id:
            raise HTTPException(status_code=403, detail="only the server owner can run setup")
        return session
    if request.app.state.holds_secrets():
        if session is None:
            raise HTTPException(status_code=401, detail="not signed in — use Login with Plex")
        return session  # any Plex account: the one that links the server becomes the owner
    return session or {"unclaimed": True}


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
    # The Plex token NEVER goes to the browser. During first-time setup we hold it server-side,
    # keyed to this session, so the wizard can enumerate/probe/link servers without the SPA ever
    # touching it (an XSS anywhere in the UI must not be able to steal the owner's Plex token).
    if owner_id is None:
        state.pending_plex_tokens[account_id] = token
    return {"linked": True, "account_id": account_id, "username": payload["username"]}


@router.get("/session")
async def get_session(request: Request) -> dict:
    # `login_required` is what tells the SPA whether to open the wizard or the login screen. It is
    # NOT "has someone claimed it" — an instance with a secret seeded from the environment has no
    # owner and still holds something worth stealing, so it demands a sign-in too.
    login_required = request.app.state.owner_account_id() is not None or request.app.state.holds_secrets()
    session = read_session(request)
    if session is None:
        return {"authenticated": False, "login_required": login_required}
    return {"authenticated": True, "login_required": login_required, **session}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}
