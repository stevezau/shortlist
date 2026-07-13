"""Sonarr/Radarr ("Arr") clients: add a title the library is missing so it gets downloaded.

Rowarr never manages what these apps already hold — it only ADDS a title the curator's pool
surfaced that no delivery library has yet, and only when the owner has explicitly turned requests
on. Every add takes ``dry_run`` (logging the would-be request instead of writing), a title already
present is skipped rather than re-added, and writes are throttled so a big run can't hammer the app.

The add path deliberately POSTs the app's own lookup resource back to it (enriched with the target
quality profile / root folder), rather than hand-building the body: that keeps Rowarr correct across
Sonarr/Radarr versions instead of guessing at required metadata fields.
"""

from __future__ import annotations

import time

import httpx

from rowarr.engine.models import ArrTarget


class ArrError(RuntimeError):
    """A Sonarr/Radarr call failed — connection, auth, or a rejected add.

    Never carries the URL or api key: the message is surfaced in the UI and written to events, and
    an Arr api key is a secret like any other (plex-safety rule 9).
    """


class _ArrClient:
    """Shared HTTP plumbing for the two apps; subclasses add the movie/series specifics."""

    app_name = "Arr"

    def __init__(self, target: ArrTarget, *, timeout: float = 30.0, min_write_interval: float = 1.0):
        self._target = target
        self._base = target.url.rstrip("/")
        self._timeout = timeout
        self._min_write_interval = min_write_interval
        self._last_write = 0.0

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._target.api_key}

    def _get(self, path: str, **params: object) -> object:
        try:
            r = httpx.get(f"{self._base}{path}", headers=self._headers(), params=params, timeout=self._timeout)
        except httpx.HTTPError as e:
            # str(e) can embed the request URL but never the api key (that's a header) — still, keep
            # the message generic so no target detail leaks into events.
            raise ArrError(f"{self.app_name} unreachable ({type(e).__name__})") from e
        if r.status_code == 401:
            raise ArrError(f"{self.app_name} rejected the API key")
        if r.status_code != 200:
            raise ArrError(f"{self.app_name} GET {path} returned HTTP {r.status_code}")
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        self._throttle()
        try:
            r = httpx.post(f"{self._base}{path}", headers=self._headers(), json=body, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise ArrError(f"{self.app_name} unreachable ({type(e).__name__})") from e
        if r.status_code == 401:
            raise ArrError(f"{self.app_name} rejected the API key")
        # Radarr/Sonarr answer a duplicate add with 400 and a validation body; treat every non-2xx
        # as an error the caller records, but keep the app's own message (it never contains secrets).
        if r.status_code >= 300:
            raise ArrError(f"{self.app_name} refused the add (HTTP {r.status_code}): {_first_error(r)}")
        return r.json()

    def _throttle(self) -> None:
        """At most one write per ``min_write_interval`` seconds — be a polite client (rule 6 spirit)."""
        elapsed = time.monotonic() - self._last_write
        if elapsed < self._min_write_interval:
            time.sleep(self._min_write_interval - elapsed)
        self._last_write = time.monotonic()

    def ping(self) -> str:
        """A tiny authenticated call for the settings 'Test' button; returns a friendly version line."""
        status = self._get("/api/v3/system/status")
        version = status.get("version", "?") if isinstance(status, dict) else "?"
        return f"Connected to {self.app_name} {version}"

    def quality_profiles(self) -> list[dict]:
        """[{id, name}] so the UI can offer a dropdown instead of asking for a raw profile id."""
        data = self._get("/api/v3/qualityprofile")
        return [{"id": p["id"], "name": p["name"]} for p in data] if isinstance(data, list) else []

    def root_folders(self) -> list[dict]:
        """[{id, path}] so the UI can offer a dropdown instead of asking for a raw path."""
        data = self._get("/api/v3/rootfolder")
        return [{"id": f["id"], "path": f["path"]} for f in data] if isinstance(data, list) else []


class RadarrClient(_ArrClient):
    app_name = "Radarr"

    def add_movie(self, tmdb_id: int, *, dry_run: bool) -> tuple[str, str]:
        """Request one movie by TMDB id. Returns (status, detail); never raises for a normal skip.

        status is one of: would_request (dry-run), requested, skipped_present, error.
        """
        resource = self._get(f"/api/v3/movie/lookup/tmdb?tmdbId={tmdb_id}")
        if not isinstance(resource, dict) or not resource.get("tmdbId"):
            return "error", "Radarr could not find this title"
        if resource.get("id"):  # a non-zero id means Radarr already tracks it
            return "skipped_present", "already in Radarr"
        if dry_run:
            return "would_request", "would add to Radarr"
        body = {
            **resource,
            "qualityProfileId": self._target.quality_profile_id,
            "rootFolderPath": self._target.root_folder,
            "monitored": True,
            "minimumAvailability": "released",
            "addOptions": {"searchForMovie": True},
        }
        self._post("/api/v3/movie", body)
        return "requested", "added to Radarr and searching"


class SonarrClient(_ArrClient):
    app_name = "Sonarr"

    def add_series(self, tvdb_id: int, *, dry_run: bool) -> tuple[str, str]:
        """Request one series by TVDB id. Returns (status, detail); never raises for a normal skip.

        status is one of: would_request (dry-run), requested, skipped_present, error.
        """
        results = self._get(f"/api/v3/series/lookup?term=tvdb:{tvdb_id}")
        resource = _match_tvdb(results, tvdb_id)
        if resource is None:
            return "error", "Sonarr could not find this title"
        if resource.get("id"):  # a non-zero id means Sonarr already tracks it
            return "skipped_present", "already in Sonarr"
        if dry_run:
            return "would_request", "would add to Sonarr"
        body = {
            **resource,
            "qualityProfileId": self._target.quality_profile_id,
            "rootFolderPath": self._target.root_folder,
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {"searchForMissingEpisodes": True, "monitor": "all"},
        }
        self._post("/api/v3/series", body)
        return "requested", "added to Sonarr and searching"


def _match_tvdb(results: object, tvdb_id: int) -> dict | None:
    """The lookup result whose tvdbId matches — a term search can return near-matches too."""
    if not isinstance(results, list):
        return None
    for item in results:
        if isinstance(item, dict) and item.get("tvdbId") == tvdb_id:
            return item
    return None


def _first_error(response: httpx.Response) -> str:
    """Pull the app's own human message out of a validation-error body, if there is one."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return str(first.get("errorMessage") or first.get("message") or first)
    if isinstance(payload, dict):
        return str(payload.get("message") or payload)
    return str(payload)[:200]
