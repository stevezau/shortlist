"""Wizard step-1 capability probe: version gate, Plex Pass, libraries, Tautulli.

Kept out of the router so the router is only request/token plumbing. This is a sync function
(it drives plexapi and blocking httpx), meant to be run in a thread executor by the endpoint.
"""

from __future__ import annotations

import httpx

from shortlist.engine.clients.plex_pms import MIN_PMS_VERSION, PlexClient, parse_pms_version
from shortlist.engine.clients.plextv import PLEXTV
from shortlist.server.net_guard import check_url


def plextv_account(token: str, client_id: str) -> dict:
    """The plex.tv account behind a token — its id and subscription. Also how login resolves who
    signed in (auth.py); the shape is `GET /api/v2/user`."""
    r = httpx.get(
        f"{PLEXTV}/api/v2/user",
        headers={"X-Plex-Token": token, "Accept": "application/json", "X-Plex-Client-Identifier": client_id},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def run_capability_probe(
    plex_url: str,
    token: str,
    client_id: str,
    *,
    tautulli_url: str | None = None,
    tautulli_apikey: str | None = None,
) -> dict:
    """Build the wizard's step-1 checklist. Raises on unreachable server (endpoint maps to 502).

    Both URLs come straight from the wizard form, and on a FRESH install this runs before any owner
    exists — so it is the one server-side fetch an unauthenticated caller can aim. `check_url` refuses
    a non-HTTP scheme and the cloud metadata addresses; private/LAN addresses stay allowed, because
    pointing at `192.168.1.50:32400` or `http://plex:32400` is the normal configuration for a
    self-hosted app (see `net_guard`).
    """
    check_url(plex_url, what="The Plex URL")
    if tautulli_url:
        check_url(tautulli_url, what="The Tautulli URL")
    result: dict = {"checks": {}}
    # No redirects on this path: it is the one server-side fetch an UNAUTHENTICATED caller can aim,
    # so a permitted host answering 302 must not be able to bounce it onto a blocked address.
    plex = PlexClient(plex_url, token, follow_redirects=False)
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
    result["server_name"] = plex.server_name
    info = plextv_account(token, client_id)
    result["owner_account_id"] = int(info["id"])
    plex_pass = (info.get("subscription") or {}).get("active", False)
    result["checks"]["plex_pass"] = {
        "ok": bool(plex_pass),
        "message": "Plex Pass active" if plex_pass else "Label restrictions need Plex Pass on the admin account",
    }
    sections = [{"key": s.key, "title": s.title, "type": s.type, "count": s.totalSize} for s in plex.sections()]
    result["checks"]["libraries"] = {
        "ok": bool(sections),
        "message": (
            f"{len(sections)} {'library' if len(sections) == 1 else 'libraries'} found"
            if sections
            else "No movie/show libraries found"
        ),
    }
    result["libraries"] = sections
    if tautulli_url:
        from shortlist.engine.clients.tautulli import TautulliClient

        try:
            TautulliClient(tautulli_url, tautulli_apikey or "").ping()
            result["checks"]["tautulli"] = {"ok": True, "message": "Tautulli connected"}
        except Exception as e:
            result["checks"]["tautulli"] = {"ok": False, "message": f"Tautulli: {type(e).__name__}"}
    return result
