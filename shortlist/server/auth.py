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
from pydantic import BaseModel, ConfigDict

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

    Raises ``httpx.HTTPError`` on ANY failure to get a usable answer — transport, status, or a body
    that is not the list of resources we expect. Callers fail closed on that one exception type, so
    a malformed 200 must not escape as something else: a captive portal or proxy answering
    ``200 text/html`` used to surface as an unhandled 500 with nothing in the log.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{PLEXTV}/api/v2/resources?includeHttps=1",
            headers={**_client_headers(client_id), "X-Plex-Token": token},
        )
    response.raise_for_status()
    try:
        resources = response.json()
    except ValueError as e:  # HTML/XML from a portal or proxy, not JSON
        raise httpx.HTTPError(f"plex.tv returned a non-JSON resources body: {type(e).__name__}") from e
    if not isinstance(resources, list):
        raise httpx.HTTPError(f"plex.tv returned {type(resources).__name__}, expected a list of resources")
    # An EMPTY list is a legitimate answer (this account has no resources). A non-empty list with no
    # objects in it is not a resources payload at all, and must not be reported as "owns nothing" —
    # that tells the owner they don't own a Plex server when plex.tv actually returned garbage.
    if resources and not any(isinstance(entry, dict) for entry in resources):
        raise httpx.HTTPError("plex.tv returned a list with no resource objects")

    owned: set[str] = set()
    for entry in resources:
        if not isinstance(entry, dict):
            continue
        machine_id = entry.get("clientIdentifier")
        provides = entry.get("provides")
        # `owned` is compared to True, NOT tested for truthiness: the string "0" and the string
        # "false" are both truthy in Python, so a plex.tv response that ever serialised the flag as
        # a string would hand back someone else's server as OWNED — failing OPEN on the one check
        # that decides who may write to a stranger's PMS. `1` is accepted because JSON booleans and
        # Plex's older 0/1 integers are both legitimate; a string never is.
        if entry.get("owned") not in (True, 1):
            continue
        # "server" as its own capability, not a substring: `provides` is a comma-separated list, so
        # matching loosely would accept a hypothetical "media-server-client".
        if not (isinstance(provides, str) and "server" in provides.split(",")):
            continue
        if isinstance(machine_id, str) and machine_id:
            owned.add(machine_id)
    return owned


async def _seeded_token_account_id(state) -> int | None:
    """The Plex account id that this instance's env-seeded ``PLEX_TOKEN`` belongs to, or None.

    Only meaningful BEFORE a server is linked. `docker-compose` can seed a real, working Plex token
    with no server row — and that token is the thing worth stealing here, so it is also the thing
    that says whose instance this is.

    Returns None in the two cases where nothing is identified, and those are deliberately different
    from an error:

    * **No token seeded** — nothing names an owner; the caller falls back to a weaker bar.
    * **The token is dead** (plex.tv answers 401/403) — a revoked token grants nobody anything, so it
      is not a secret worth locking the wizard over. Failing closed here would BRICK first-run login
      for anyone whose seeded token had since been rotated, and protect nothing by doing it.

    Raises ``HTTPException(503)`` if plex.tv cannot be reached at all: "couldn't ask" is not "no
    owner", and treating it as one would reopen the very hole this closes.
    """
    from shortlist.server.settings_store import SettingsStore

    with state.sessions() as session:
        seeded = SettingsStore(session, state.secrets).get("plex.token")
    if not seeded:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{PLEXTV}/api/v2/user",
                headers={**_client_headers(state.client_id), "X-Plex-Token": seeded},
            )
    except httpx.HTTPError as e:
        logger.warning("login deferred: could not identify the seeded Plex token ({})", type(e).__name__)
        raise HTTPException(
            status_code=503, detail="could not reach plex.tv to confirm this instance — try again in a moment"
        ) from e

    if response.status_code in (401, 403):
        logger.info("seeded Plex token is no longer valid — falling back to the owns-a-server check")
        return None
    try:
        response.raise_for_status()
        return int(response.json()["id"])
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
        # A body we can't read is not an answer. Fail closed rather than silently downgrading.
        logger.warning("login deferred: plex.tv gave an unreadable account for the seeded token")
        raise HTTPException(
            status_code=503, detail="could not reach plex.tv to confirm this instance — try again in a moment"
        ) from e


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


class PinOut(BaseModel):
    """A freshly created plex.tv PIN. The `code` is what the owner types into plex.tv/link — it is
    short-lived and useless without the same `client_id`, and it is not a Shortlist credential.

    ``extra="allow"`` is on every response model in this file: a strict Pydantic response model
    silently DROPS any key it does not declare, so a field missed here would vanish from the payload
    rather than fail loudly. The model documents the shape; it never filters it.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    code: str
    client_id: str


@router.post("/pin", response_model=PinOut)
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


class PinStatusOut(BaseModel):
    """The poll result. Until the owner approves in Plex it is `linked: false` and the identity
    fields are null; the Plex auth token is NEVER part of this payload (it is held server-side,
    keyed to the session, so an XSS in the SPA cannot steal it)."""

    model_config = ConfigDict(extra="allow")

    linked: bool
    account_id: int | None = None
    username: str | None = None


@router.get("/pin/{pin_id}", response_model=PinStatusOut)
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
        # Unclaimed instance: there is no stored owner to compare against, so who may claim it is
        # decided here. Two bars, strongest first.
        seeded_owner = await _seeded_token_account_id(state)
        if seeded_owner is not None:
            # The environment seeded a WORKING `PLEX_TOKEN`. That token names exactly one Plex
            # account, and it is the secret an attacker would come here to steal — so the bar is
            # "you ARE that account", not merely "you own some Plex server somewhere". Without this,
            # anyone running their own PMS cleared the ownership check, took a session, linked their
            # own machine id, and owned an instance holding someone else's live Plex token.
            if account_id != seeded_owner:
                logger.warning(
                    "login rejected: Plex account {} is not the account this instance's seeded token belongs to",
                    account_id,
                )
                raise HTTPException(
                    status_code=403,
                    detail="This Shortlist was set up with another account's Plex token. Sign in as that account.",
                )
        else:
            # Nothing here identifies an owner (no seeded token, or the seeded one is dead and so
            # worth nothing to a thief). Fall back to the weaker bar: someone who owns NO Plex server
            # can never legitimately finish setup, since Shortlist only ever writes to a server you
            # own. A friend who merely has a share on someone's server lands here and is turned away.
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
                    detail=(
                        "Shortlist has to be set up by the owner of a Plex server, and this account does not own one."
                    ),
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


class SessionOut(BaseModel):
    """Who the caller is, and whether this instance demands a sign-in at all.

    The signed cookie's contents are spread into the response, so `account_id`/`username` are
    present only once signed in — they are declared optional rather than left to `extra`, so the
    SPA gets real types for the two fields it actually reads.
    """

    model_config = ConfigDict(extra="allow")

    authenticated: bool
    # Not "has someone claimed it": an instance holding a Plex token seeded from the environment has
    # no owner and still holds something worth stealing, so it demands a sign-in too.
    login_required: bool
    account_id: int | None = None
    username: str | None = None


@router.get("/session", response_model=SessionOut)
async def get_session(request: Request) -> dict:
    # `login_required` is what tells the SPA whether to open the wizard or the login screen. It is
    # NOT "has someone claimed it" — an instance with a secret seeded from the environment has no
    # owner and still holds something worth stealing, so it demands a sign-in too.
    login_required = request.app.state.owner_account_id() is not None or request.app.state.holds_secrets()
    session = read_session(request)
    if session is None:
        return {"authenticated": False, "login_required": login_required}
    return {"authenticated": True, "login_required": login_required, **session}


class LogoutOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool


@router.post("/logout", response_model=LogoutOut)
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}
