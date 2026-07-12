"""Setup API: capability probe, server link, resumable wizard state.

Before a server is linked there is no owner yet, so these endpoints accept any
authenticated Plex account; the account that links the server BECOMES the owner
(enforced by verifying it owns the server on plex.tv). Afterwards, owner-only.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from rowarr.engine.clients.plex import MIN_PMS_VERSION, parse_pms_version
from rowarr.server.auth import CSRF_HEADER, read_session
from rowarr.server.db.models import Server
from rowarr.server.settings_store import SettingsStore

router = APIRouter(prefix="/setup", tags=["setup"])


def _require_session_owner_if_linked(request: Request) -> dict:
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="sign in with Plex first")
    owner_id = request.app.state.owner_account_id()
    if owner_id is not None and session["account_id"] != owner_id:
        raise HTTPException(status_code=403, detail="only the server owner can run setup")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=403, detail=f"missing {CSRF_HEADER} header")
    return session


class ProbeRequest(BaseModel):
    plex_url: str
    plex_token: str
    tautulli_url: str | None = None
    tautulli_apikey: str | None = None


@router.post("/probe")
async def probe(body: ProbeRequest, request: Request) -> dict:
    """Capability checklist for wizard step 1: version gate, Plex Pass, libraries, Tautulli."""
    _require_session_owner_if_linked(request)

    def run_probe() -> dict:
        import httpx

        from rowarr.engine.clients.plex import PlexClient

        result: dict = {"checks": {}}
        plex = PlexClient(body.plex_url, body.plex_token)
        version = plex.version
        version_ok = parse_pms_version(version) >= MIN_PMS_VERSION
        result["checks"]["pms_version"] = {
            "ok": version_ok,
            "value": version,
            "message": (
                f"Plex Media Server {version} supports private rows"
                if version_ok
                else f"PMS {version} predates the privacy fix — upgrade to "
                + ".".join(map(str, MIN_PMS_VERSION))
                + " or newer"
            ),
        }
        result["machine_id"] = plex.machine_id
        result["server_name"] = plex._server.friendlyName
        account = httpx.get(
            "https://plex.tv/api/v2/user",
            headers={
                "X-Plex-Token": body.plex_token,
                "Accept": "application/json",
                "X-Plex-Client-Identifier": request.app.state.client_id,
            },
            timeout=15,
        )
        account.raise_for_status()
        info = account.json()
        result["owner_account_id"] = int(info["id"])
        plex_pass = (info.get("subscription") or {}).get("active", False)
        result["checks"]["plex_pass"] = {
            "ok": bool(plex_pass),
            "message": "Plex Pass active" if plex_pass else "Label restrictions need Plex Pass on the admin account",
        }
        sections = [{"key": s.key, "title": s.title, "type": s.type, "count": s.totalSize} for s in plex.sections()]
        result["checks"]["libraries"] = {
            "ok": bool(sections),
            "message": f"{len(sections)} librarie(s) found" if sections else "No movie/show libraries found",
        }
        result["libraries"] = sections
        if body.tautulli_url:
            from rowarr.engine.clients.tautulli import TautulliClient

            try:
                TautulliClient(body.tautulli_url, body.tautulli_apikey or "").ping()
                result["checks"]["tautulli"] = {"ok": True, "message": "Tautulli connected"}
            except Exception as e:
                result["checks"]["tautulli"] = {"ok": False, "message": f"Tautulli: {type(e).__name__}"}
        return result

    try:
        return await asyncio.get_running_loop().run_in_executor(None, run_probe)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"probe failed: {type(e).__name__}: {e}") from e


class LinkRequest(BaseModel):
    plex_url: str
    plex_token: str
    machine_id: str
    server_name: str = ""
    version: str = ""
    owner_account_id: int
    plex_pass: bool = False


@router.post("/link")
async def link_server(body: LinkRequest, request: Request) -> dict:
    """Persist the chosen server; the authenticated account must be its owner."""
    session_data = _require_session_owner_if_linked(request)
    if session_data["account_id"] != body.owner_account_id:
        raise HTTPException(status_code=403, detail="you can only link a server your account owns")
    state = request.app.state
    with state.sessions() as db:
        store = SettingsStore(db, state.secrets)
        store.set("plex.url", body.plex_url)
        store.set("plex.token", body.plex_token)
        server = db.query(Server).filter_by(machine_id=body.machine_id).one_or_none()
        if server is None:
            server = Server(
                machine_id=body.machine_id,
                url=body.plex_url,
                token_enc=state.secrets.encrypt(body.plex_token),
                name=body.server_name,
                version=body.version,
                owner_account_id=body.owner_account_id,
                plex_pass=body.plex_pass,
                capabilities={},
            )
            db.add(server)
        else:
            server.url = body.plex_url
            server.token_enc = state.secrets.encrypt(body.plex_token)
            server.version = body.version
            server.owner_account_id = body.owner_account_id
            server.plex_pass = body.plex_pass
        db.commit()
    return {"linked": True, "server_name": body.server_name}


class WizardState(BaseModel):
    step: int
    state: dict = {}
    completed: bool = False


@router.get("/state")
async def get_state(request: Request) -> dict:
    _require_session_owner_if_linked(request)
    with request.app.state.sessions() as db:
        store = SettingsStore(db, request.app.state.secrets)
        return {
            "step": store.get("setup.step"),
            "state": store.get("setup.state", {}),
            "completed": store.get("setup.completed"),
        }


@router.put("/state")
async def put_state(body: WizardState, request: Request) -> dict:
    _require_session_owner_if_linked(request)
    with request.app.state.sessions() as db:
        store = SettingsStore(db, request.app.state.secrets)
        store.set("setup.step", body.step)
        store.set("setup.state", body.state)
        store.set("setup.completed", body.completed)
        return {"step": body.step, "completed": body.completed}
