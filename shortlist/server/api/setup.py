"""Setup API: server discovery, capability probe, server link, resumable wizard state.

The owner's Plex token never reaches the browser. It is minted by the PIN flow and held
server-side (``app.state.pending_plex_tokens``) until a server is linked, after which it lives
encrypted in the settings table. Every endpoint here takes the token from one of those two
places — the SPA never sends or sees it.

Before a server is linked there is no owner yet. `GET /state` is open on a truly empty instance
so the wizard renders before you sign in; every endpoint that touches a Plex token (`/servers`,
`/probe`, `/link`) and `PUT /state` require a signed-in account. The account that links the
server becomes the owner; afterwards, owner-only.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from shortlist.engine.clients.plextv import PLEXTV
from shortlist.server.auth import require_setup_access
from shortlist.server.db.models import Server
from shortlist.server.services.setup_probe import run_capability_probe
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/setup", tags=["setup"])


def _plex_token(request: Request, session: dict) -> str:
    """The token for this setup session: the one THIS caller minted at PIN login, or — only for the
    confirmed owner — the one already stored.

    This is the line that decides who can borrow a Plex token, and it is the exfiltration primitive
    if it is wrong. `/setup/probe` sends the token to a URL the caller supplies, so a token handed
    to the wrong person is a token mailed to an attacker's host. The rules:

    * A caller's OWN pending token (from their PIN sign-in this run) is always theirs to use.
    * The STORED token is the owner's. It is returned only when the caller IS the owner — never to
      a signed-in stranger on an unclaimed, secret-seeded instance, and never (as before) to an
      anonymous one. `pending_plex_tokens` is a per-process dict, so a stranger's pending entry is
      routinely absent (a restart, or another worker) — falling back to the stored token for them
      would be the whole hole.
    """
    account_id = session.get("account_id")
    if account_id is None:
        raise HTTPException(status_code=401, detail="not signed in — use Login with Plex")
    pending = request.app.state.pending_plex_tokens.get(account_id)
    if pending:
        return pending
    if request.app.state.owner_account_id() != account_id:
        raise HTTPException(status_code=409, detail="sign in with Plex again — the setup session expired")
    with request.app.state.sessions() as db:
        token = SettingsStore(db, request.app.state.secrets).get("plex.token")
    if not token:
        raise HTTPException(status_code=409, detail="sign in with Plex again — the setup session expired")
    return token


@router.get("/servers")
async def list_servers(request: Request) -> list[dict]:
    """Every Plex server this account can reach, with each advertised address tested.

    This is what makes the URL field a picker instead of a guess: Plex advertises several
    addresses per server (LAN, hostname, relay), and only the owner's network knows which one
    actually works from where Shortlist runs — so we try them all and report what answered.
    """
    session = require_setup_access(request)
    token = _plex_token(request, session)
    client_id = request.app.state.client_id

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{PLEXTV}/api/v2/resources?includeHttps=1&includeRelay=1",
            headers={"X-Plex-Token": token, "Accept": "application/json", "X-Plex-Client-Identifier": client_id},
        )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"plex.tv returned HTTP {response.status_code}")

    servers = [r for r in response.json() if "server" in (r.get("provides") or "")]

    async def test(uri: str) -> dict:
        """One cheap /identity call — does this address actually answer from inside the container?

        No token: /identity is Plex's unauthenticated reachability endpoint, so the probe sends
        nothing secret. That matters because `verify=False` is required here (local connections
        advertise self-signed certs) — with the token attached, an on-path attacker on any advertised
        address could have MITM'd it off the wire (plex-safety rule 9). Without it, there's nothing
        to steal, so relaxed TLS only costs us a truthful reachability answer."""
        try:
            async with httpx.AsyncClient(timeout=4, verify=False) as client:
                r = await client.get(f"{uri}/identity")
            return {"uri": uri, "ok": r.status_code == 200}
        except Exception as e:
            # "Shortlist can't reach my Plex URL" is the #1 first-run question — record the reason
            # (refused vs TLS vs timeout vs DNS) so it's answerable from the log, not just a bare False.
            logger.debug("setup probe: {} unreachable ({})", uri, type(e).__name__)
            return {"uri": uri, "ok": False}

    out = []
    for server in servers:
        connections = server.get("connections") or []
        results = await asyncio.gather(*(test(c["uri"]) for c in connections if c.get("uri")))
        reachable = {r["uri"]: r["ok"] for r in results}
        out.append(
            {
                "name": server.get("name") or "Plex Media Server",
                "machine_id": server.get("clientIdentifier"),
                "owned": bool(server.get("owned")),
                "version": server.get("productVersion") or "",
                "connections": [
                    {
                        "uri": c["uri"],
                        "local": bool(c.get("local")),
                        "relay": bool(c.get("relay")),
                        "ok": reachable.get(c["uri"], False),
                    }
                    for c in connections
                    if c.get("uri")
                ],
            }
        )
    logger.info("server picker: {} server(s) discovered for setup", len(out))
    return out


class ProbeRequest(BaseModel):
    plex_url: str
    tautulli_url: str | None = None
    tautulli_apikey: str | None = None


@router.post("/probe")
async def probe(body: ProbeRequest, request: Request) -> dict:
    """Capability checklist for wizard step 1: version gate, Plex Pass, libraries, Tautulli."""
    session = require_setup_access(request)
    token = _plex_token(request, session)
    client_id = request.app.state.client_id

    def run_probe() -> dict:
        return run_capability_probe(
            body.plex_url,
            token,
            client_id,
            tautulli_url=body.tautulli_url,
            tautulli_apikey=body.tautulli_apikey,
        )

    try:
        return await asyncio.get_running_loop().run_in_executor(None, run_probe)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"could not reach that server: {type(e).__name__}") from e


class LinkRequest(BaseModel):
    plex_url: str
    machine_id: str
    server_name: str = ""
    version: str = ""
    owner_account_id: int
    plex_pass: bool = False


@router.post("/link")
async def link_server(body: LinkRequest, request: Request) -> dict:
    """Persist the chosen server; the authenticated account must be its owner."""
    session_data = require_setup_access(request)
    if "account_id" not in session_data:
        # Claiming an instance is the one thing that MUST be attributable to a Plex account: it is
        # what decides who owns it forever.
        raise HTTPException(status_code=401, detail="sign in with Plex to claim this instance")
    if session_data["account_id"] != body.owner_account_id:
        raise HTTPException(status_code=403, detail="you can only link a server your account owns")
    state = request.app.state
    token = _plex_token(request, session_data)

    with state.sessions() as db:
        store = SettingsStore(db, state.secrets)
        store.set("plex.url", body.plex_url)
        store.set("plex.token", token)
        server = db.query(Server).filter_by(machine_id=body.machine_id).one_or_none()
        if server is None:
            server = Server(
                machine_id=body.machine_id,
                url=body.plex_url,
                token_enc=state.secrets.encrypt(token),
                name=body.server_name,
                version=body.version,
                owner_account_id=body.owner_account_id,
                plex_pass=body.plex_pass,
                capabilities={},
            )
            db.add(server)
        else:
            server.url = body.plex_url
            server.token_enc = state.secrets.encrypt(token)
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
    require_setup_access(request)
    with request.app.state.sessions() as db:
        store = SettingsStore(db, request.app.state.secrets)
        return {
            "step": store.get("setup.step"),
            "state": store.get("setup.state", {}),
            "completed": store.get("setup.completed"),
        }


@router.put("/state")
async def put_state(body: WizardState, request: Request) -> dict:
    session = require_setup_access(request)
    if "account_id" not in session:
        # Nothing worth saving happens before you connect Plex (that's step 1), and this keeps an
        # anonymous caller from scribbling wizard progress — or flipping setup.completed — on an
        # empty instance. GET /state stays open so the wizard still renders pre-sign-in.
        raise HTTPException(status_code=401, detail="sign in with Plex first")
    with request.app.state.sessions() as db:
        store = SettingsStore(db, request.app.state.secrets)
        store.set("setup.step", body.step)
        store.set("setup.state", body.state)
        store.set("setup.completed", body.completed)
        return {"step": body.step, "completed": body.completed}
