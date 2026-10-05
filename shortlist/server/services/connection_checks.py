"""Shared non-generating probes of saved service connections."""

from __future__ import annotations

from collections.abc import Callable

READ_ONLY_PROBES = frozenset({"plex", "tautulli", "tmdb", "radarr", "sonarr", "overseerr", "mdblist", "trakt"})


def probe_read_only_connection(service: str, get: Callable[[str], object]) -> str:
    """Perform a named API read with values from an owner-controlled settings snapshot.

    Provider generation, paid search and notification delivery have deliberately separate paths.
    """
    if service == "plex":
        from shortlist.engine.clients.plex_pms import PlexClient
        from shortlist.server.services.plex_reachability import explained

        url = get("plex.url")
        with explained(url):
            plex = PlexClient(url, get("plex.token"))
        return f"Connected to {plex.server_name} (Plex Media Server {plex.version})"
    if service == "tautulli":
        from shortlist.engine.clients.tautulli import TautulliClient

        TautulliClient(get("tautulli.url"), get("tautulli.apikey")).ping()
        return "Tautulli responded"
    if service == "tmdb":
        from shortlist.engine.clients.tmdb import TmdbClient

        if not TmdbClient(get("tmdb.apikey")).ping():
            raise RuntimeError("TMDB rejected the key")
        return "TMDB key works"
    if service in ("radarr", "sonarr"):
        from shortlist.engine.clients.arr import make_arr_client
        from shortlist.engine.models import ArrTarget

        prefix = f"requests.{service}"
        url = (get(f"{prefix}.url") or "").strip()
        api_key = get(f"{prefix}.apikey") or ""
        if not url or not api_key:
            raise RuntimeError(f"{service.title()} URL and API key are both required")
        return make_arr_client(
            service, ArrTarget(url=url, api_key=api_key, quality_profile_id=0, root_folder="")
        ).ping()
    if service == "overseerr":
        from shortlist.engine.clients.seerr import SeerrClient
        from shortlist.engine.models import SeerrTarget

        url = (get("requests.overseerr.url") or "").strip()
        api_key = get("requests.overseerr.apikey") or ""
        if not url or not api_key:
            raise RuntimeError("Overseerr URL and API key are both required")
        return SeerrClient(SeerrTarget(url=url, api_key=api_key)).ping()
    if service == "mdblist":
        from shortlist.engine.clients.mdblist import MdbListClient

        api_key = get("requests.mdblist.apikey") or ""
        if not api_key:
            raise RuntimeError("An MDBList API key is required for IMDb/Trakt/RT/Metacritic ratings")
        return MdbListClient(api_key).ping()
    if service == "trakt":
        from shortlist.engine.clients.trakt import TraktClient

        client_id = get("trakt.client_id") or ""
        if not client_id:
            raise RuntimeError("A Trakt API key (client id) is required")
        return TraktClient(client_id).ping()
    raise ValueError("This connection has no non-generating read-only probe.")
