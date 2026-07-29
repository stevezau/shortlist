"""Login with Plex (PIN flow) — owner-only sessions, signed httpOnly cookie, CSRF header.

No password ever touches Shortlist: the PIN is created against plex.tv, the user approves it in
their Plex app/browser, and the resulting token's account must match the linked server's
owner. The Plex token from auth is used once to identify the account and (during setup)
stored encrypted; it is never logged.
"""

from __future__ import annotations

import time
from collections import deque

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from loguru import logger

PLEXTV = "https://plex.tv"
PRODUCT = "Shortlist"
SESSION_COOKIE = "shortlist_session"
SESSION_MAX_AGE_S = 14 * 24 * 3600
CSRF_HEADER = "x-shortlist-csrf"

# Programmatic API access: an owner-generated Bearer token. Stored ENCRYPTED at rest (Fernet, same as
# the Plex/curator keys) so the owner can reveal it later — like Sonarr/Radarr's API key — while a bare
# config/DB dump without /config/secret.key still can't read it. Managed via /api/system/api-token.
API_TOKEN_KEY = "api.token"
API_TOKEN_PREFIX = "shl_"  # human-recognizable so a leaked token is spotted in logs/history


def _bearer_token(request: Request) -> str | None:
    """The token from an ``Authorization: Bearer <token>`` header, or None if absent/malformed."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


router = APIRouter(prefix="/auth", tags=["auth"])

# Sliding-window limiter for the unauthenticated PIN endpoint, so it can't be spammed to hammer
# plex.tv. In-memory is fine: a single self-hosted process, and a restart resetting the window is
# harmless. Two ceilings, because behind `--proxy-headers` with FORWARDED_ALLOW_IPS=* the per-IP key
# (`request.client.host`, from X-Forwarded-For) is client-spoofable — so a GLOBAL cap across all IPs
# is the real backstop that bounds total plex.tv load even if an attacker rotates the header; the
# per-IP cap is the finer-grained control for the honest-proxy case.
_PIN_HITS: dict[str, deque[float]] = {}
_PIN_ALL: deque[float] = deque()
_PIN_MAX_PER_WINDOW = 10
_PIN_MAX_GLOBAL = 60
_PIN_WINDOW_S = 60.0
_PIN_BUSY = "Too many sign-in attempts — wait a minute and try again."


def _rate_limit_pin(request: Request) -> None:
    now = time.monotonic()
    while _PIN_ALL and now - _PIN_ALL[0] > _PIN_WINDOW_S:
        _PIN_ALL.popleft()
    if len(_PIN_ALL) >= _PIN_MAX_GLOBAL:  # global ceiling: unspoofable, bounds total plex.tv load
        raise HTTPException(status_code=429, detail=_PIN_BUSY)

    ip = (request.client.host if request.client else None) or "unknown"
    hits = _PIN_HITS.setdefault(ip, deque())
    while hits and now - hits[0] > _PIN_WINDOW_S:
        hits.popleft()
    if len(hits) >= _PIN_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail=_PIN_BUSY)

    hits.append(now)
    _PIN_ALL.append(now)
    # Bound memory: when the table grows large, drop other IPs whose window has fully expired.
    # Mutated in place (no reassignment), so it stays the module-level dict; only runs when big.
    if len(_PIN_HITS) > 4096:
        for stale in [k for k, v in _PIN_HITS.items() if k != ip and (not v or now - v[-1] > _PIN_WINDOW_S)]:
            del _PIN_HITS[stale]


# poll_pin proxies to plex.tv on every call and cannot require auth (it IS the login handshake), so a
# GLOBAL-only cap bounds total plex.tv amplification if someone spams it with random pin ids.
# Deliberately NOT per-IP and set generously: the legit client polls the PIN every ~1.5s while the
# owner authorizes in Plex, and a tight per-IP cap would break a normal login. A single owner (even a
# few concurrent devices) never comes near this; it only ever trips under abuse.
_POLL_ALL: deque[float] = deque()
_POLL_MAX_GLOBAL = 600


def _rate_limit_poll() -> None:
    now = time.monotonic()
    while _POLL_ALL and now - _POLL_ALL[0] > _PIN_WINDOW_S:
        _POLL_ALL.popleft()
    if len(_POLL_ALL) >= _POLL_MAX_GLOBAL:
        raise HTTPException(status_code=429, detail=_PIN_BUSY)
    _POLL_ALL.append(now)


def _client_headers(client_id: str) -> dict[str, str]:
    return {
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": client_id,
        "Accept": "application/json",
    }


async def owned_machine_ids(client_id: str, token: str) -> set[str]:
    """The machine ids of every Plex server this token's account **owns**.

    plex.tv's ``owned`` flag on ``/api/v2/resources`` is the only thing that separates a server you
    own from one merely shared with you — both appear in the listing, and Shortlist writes
    collections, labels and share filters, which is owner-level work on someone else's server.

    Raises on any plex.tv failure. Callers must fail closed: "couldn't ask plex.tv" is never
    "yes, they own it".
    """
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{PLEXTV}/api/v2/resources?includeHttps=1",
            headers={**_client_headers(client_id), "X-Plex-Token": token},
        )
    response.raise_for_status()
    return {
        r["clientIdentifier"]
        for r in response.json()
        if r.get("owned") and r.get("clientIdentifier") and "server" in (r.get("provides") or "")
    }


def session_serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="shortlist-session")


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


# Failed API-token attempts, globally. The token is a 32-char urlsafe secret so brute force is not a
# realistic threat, but an unthrottled `Bearer` check is a free oracle: it answers on every request,
# at network speed, with no lockout and nothing in the log to notice. A global cap is the right shape
# here rather than per-IP — behind `--forwarded-allow-ips=*` (the shipped default, so a reverse proxy
# on any host works out of the box) `request.client.host` comes from a header the caller controls, so
# a per-IP bucket is trivially evaded by rotating it.
_TOKEN_FAILS: deque[float] = deque()
_TOKEN_MAX_FAILS = 20
_TOKEN_WINDOW_S = 60.0


def _rate_limit_token_failures() -> None:
    """Raise 429 once failed token attempts exceed the window's budget.

    Only FAILURES are counted, so a busy legitimate integration is never throttled — the limit is
    invisible unless something is guessing.
    """
    now = time.monotonic()
    while _TOKEN_FAILS and now - _TOKEN_FAILS[0] > _TOKEN_WINDOW_S:
        _TOKEN_FAILS.popleft()
    if len(_TOKEN_FAILS) >= _TOKEN_MAX_FAILS:
        raise HTTPException(status_code=429, detail="Too many failed API-token attempts — wait a minute.")


def require_owner(request: Request) -> dict:
    """The owner, and nobody else. The default gate for everything except the setup wizard.

    An unclaimed instance has no owner, so this refuses everyone until a server is linked — which
    is correct for settings, runs, privacy, users and system: none of them make sense, or should
    be reachable, before setup is done. Only the wizard itself may run before there is an owner,
    and it uses `require_setup_access` for that.

    Owner-ness is re-checked on every request, not just at login: a session issued during the
    pre-link window loses all access the moment a different account links a server.
    """
    owner_id = request.app.state.owner_account_id()
    # Programmatic access: a valid Bearer API token grants owner-level access. A browser never sends
    # it automatically, so this path needs no CSRF check (unlike the cookie below). An invalid or
    # revoked token is rejected outright — it must never fall through to the cookie path.
    bearer = _bearer_token(request)
    if bearer is not None:
        if owner_id is not None and request.app.state.verify_api_token(bearer):
            return {"account_id": owner_id, "via": "api_token"}
        _TOKEN_FAILS.append(time.monotonic())
        logger.warning("rejected an invalid or revoked API token")
        _rate_limit_token_failures()
        raise HTTPException(status_code=401, detail="invalid or revoked API token")
    _check_csrf(request)
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="not signed in — use Login with Plex")
    if owner_id is None or session.get("account_id") != owner_id:
        raise HTTPException(status_code=403, detail="only the server owner can use Shortlist")
    return session


def require_setup_access(request: Request) -> dict:
    """Who may drive the setup wizard. Three states, and conflating the first two is how an earlier
    version of this became a way to steal the owner's Plex token:

    * **Empty** — no server linked AND no secret stored. Nothing to protect and nobody to protect
      it for, so it is open: a fresh install lands in the wizard instead of a login screen.
      Connecting Plex IS step 1, and it is what claims the instance.
    * **Holds secrets but unclaimed** — the environment can seed a real Plex/Tautulli/curator
      credential with no server row. "Nobody has claimed it" is NOT "there is nothing to steal": an
      anonymous caller here could point `/setup/probe` at a host they control and have Shortlist send
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
    _rate_limit_pin(request)
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
    _rate_limit_poll()
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
    if owner_id is not None:
        if account_id != owner_id:
            logger.warning("login rejected: Plex account {} is not the owner of the linked server", account_id)
            raise HTTPException(status_code=403, detail="only the server owner can sign in to Shortlist")
    else:
        # Unclaimed instance: nobody to compare against yet, so ownership is checked against plex.tv
        # directly. Someone who owns NO Plex server can never legitimately finish setup — Shortlist
        # only ever writes to a server you own — so reject the sign-in outright instead of handing
        # out a session that can browse the wizard. A friend who merely has a share on the owner's
        # server lands here, and this is the line that turns them away.
        try:
            owned = await owned_machine_ids(state.client_id, token)
        except httpx.HTTPError as e:
            # Fail CLOSED. An unreachable plex.tv must never read as "sure, they own a server".
            logger.warning("login deferred: could not confirm server ownership with plex.tv ({})", type(e).__name__)
            raise HTTPException(
                status_code=503, detail="could not reach plex.tv to confirm your server — try again in a moment"
            ) from e
        if not owned:
            logger.warning("login rejected: Plex account {} does not own a Plex server", account_id)
            raise HTTPException(
                status_code=403,
                detail="Shortlist has to be set up by the owner of a Plex server, and this account does not own one.",
            )

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
