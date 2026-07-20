"""Sonarr/Radarr ("Arr") clients: add a title the library is missing so it gets downloaded.

Shortlist never manages what these apps already hold — it only ADDS a title the curator's pool
surfaced that no delivery library has yet, and only when the owner has explicitly turned requests
on. Every add takes ``dry_run`` (logging the would-be request instead of writing), a title already
present is skipped rather than re-added, and writes are throttled so a big run can't hammer the app.

The add path deliberately POSTs the app's own lookup resource back to it (enriched with the target
quality profile / root folder), rather than hand-building the body: that keeps Shortlist correct across
Sonarr/Radarr versions instead of guessing at required metadata fields.
"""

from __future__ import annotations

import re

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry
from shortlist.engine.models import ArrTarget


def _sanitize_tag(label: str | None) -> str:
    """Normalise a tag to Radarr/Sonarr's allowed charset: lowercase ``a-z``, ``0-9`` and ``-``.

    A tag with anything else (a capital, space, dot, or ``+`` — e.g. from a username or a row name)
    is rejected by the Arr with ``HTTP 400: Allowed characters a-z, 0-9 and -``, which fails the whole
    add. Collapses each run of disallowed characters to a single hyphen and trims stray hyphens;
    returns ``""`` for a label that reduces to nothing (the caller drops those).
    """
    if not label:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


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
        self._existing_tags: dict[str, int] | None = None  # lowercased label -> id, fetched once per run
        self._resolved: dict[str, int] = {}  # lowercased label -> id, memoised across titles

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._target.api_key}

    def _get(self, path: str, **params: object) -> object:
        # `params or None`, never a bare `{}`: httpx ≥0.28 REPLACES a URL's existing query string with
        # the params arg, so passing an empty dict alongside a path that carries its own query (e.g.
        # `/movie/lookup/tmdb?tmdbId=…`) silently DROPS the query — Radarr then 500s (no tmdbId) and
        # Sonarr 503s (no term), failing every request. None leaves the in-path query intact.
        try:
            r = http_retry.get(
                f"{self._base}{path}", headers=self._headers(), params=params or None, timeout=self._timeout
            )
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
            # A POST adds a movie/series — retry only when it provably never landed (connect error) or
            # was rate-limited (429), never on a read timeout, so we can't double-add a title.
            r = http_retry.request(
                "POST", f"{self._base}{path}", headers=self._headers(), json=body, timeout=self._timeout
            )
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
        self._last_write = http_retry.throttle(self._last_write, self._min_write_interval)

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

    def _id_set(self, path: str, key: str) -> set[int]:
        """The set of integer ``key`` values across a list endpoint (e.g. every tmdbId in /movie).

        Used to answer "does the Arr already have / has it excluded this title?" without a per-title
        call. Tolerant of missing/oddly-typed ids so one bad row can't poison the whole set.
        """
        return self._ids_from(self._get(path), key)

    @staticmethod
    def _ids_from(data: object, key: str) -> set[int]:
        """Extract the integer ``key`` values from an already-fetched list payload (see ``_id_set``)."""
        out: set[int] = set()
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get(key) not in (None, 0):
                try:
                    out.add(int(item[key]))
                except (TypeError, ValueError):
                    continue
        return out

    def _tag_ids(self, extra: set[str] | None = None) -> list[int]:
        """Resolve the labels to apply for one add: the target's global tag unioned with ``extra``
        (per-user + per-row tags carried on the title). Each distinct label is created in the app if
        it doesn't exist, then referenced by id. Only ever called on a real add, so a dry-run never
        creates a tag. Label lookups and creations are memoised, so N titles never re-query the tag list.
        """
        # Sanitize to the Arr's tag charset (a-z 0-9 -) BEFORE resolving, or a tag with a capital/
        # space/dot (from a username or row name) 400s the whole add — the Evangelion send that failed.
        labels = {tag for label in ({self._target.tag} | (extra or set())) if (tag := _sanitize_tag(label))}
        ids = {self._resolve_tag(label) for label in labels}
        return sorted(i for i in ids if i is not None)

    def _resolve_tag(self, label: str) -> int | None:
        """One label → its Sonarr/Radarr tag id (created if new). Memoised by lowercased label."""
        key = label.lower()
        if key in self._resolved:
            return self._resolved[key]
        if self._existing_tags is None:
            existing = self._get("/api/v3/tag")
            self._existing_tags = {
                str(t["label"]).lower(): int(t["id"])
                for t in (existing if isinstance(existing, list) else [])
                if isinstance(t, dict) and t.get("id") is not None and t.get("label")
            }
        if key in self._existing_tags:
            self._resolved[key] = self._existing_tags[key]
            return self._resolved[key]
        created = self._post("/api/v3/tag", {"label": label})
        tag_id = int(created["id"]) if isinstance(created, dict) and created.get("id") is not None else None
        if tag_id is not None:
            self._resolved[key] = tag_id
            self._existing_tags[key] = tag_id
            # A real write into the operator's arr — leave a trail (this file was previously silent).
            logger.debug("{}: created tag {!r} (id {})", self.app_name, label, tag_id)
        return tag_id


class RadarrClient(_ArrClient):
    app_name = "Radarr"

    def library_tmdb_ids(self) -> set[int]:
        """Every tmdbId Radarr already tracks — so a title it has (or is still downloading) isn't
        re-surfaced as 'missing' just because it isn't in Plex yet."""
        return self._id_set("/api/v3/movie", "tmdbId")

    def excluded_tmdb_ids(self) -> set[int]:
        """tmdbIds on Radarr's import-exclusion list (usually left by a past delete)."""
        return self._id_set("/api/v3/exclusions", "tmdbId")

    def add_movie(self, tmdb_id: int, *, dry_run: bool, extra_tags: set[str] | None = None) -> tuple[str, str]:
        """Request one movie by TMDB id. Returns (status, detail); never raises for a normal skip.

        ``extra_tags`` are per-user/per-row labels layered onto the target's global tag.
        status is one of: would_request (dry-run), requested, skipped_present, error.
        """
        resource = self._get(f"/api/v3/movie/lookup/tmdb?tmdbId={tmdb_id}")
        if not isinstance(resource, dict) or not resource.get("tmdbId"):
            return "error", "Radarr could not find this title"
        if resource.get("id"):  # a non-zero id means Radarr already tracks it
            return "skipped_present", "already in Radarr"
        if dry_run:
            logger.info("[dry-run] Radarr: would add tmdb {}", tmdb_id)
            return "would_request", "would add to Radarr"
        body = {
            **resource,
            "qualityProfileId": self._target.quality_profile_id,
            "rootFolderPath": self._target.root_folder,
            "monitored": True,
            "minimumAvailability": "released",
            "tags": self._tag_ids(extra_tags),
            "addOptions": {"searchForMovie": True},
        }
        self._post("/api/v3/movie", body)
        return "requested", "added to Radarr and searching"


class SonarrClient(_ArrClient):
    app_name = "Sonarr"

    def library_tvdb_ids(self) -> set[int]:
        """Every tvdbId Sonarr already tracks (Sonarr keys shows on TVDB, not TMDB)."""
        return self._id_set("/api/v3/series", "tvdbId")

    def library_ids(self) -> tuple[set[int], set[int]]:
        """(tvdbIds, tmdbIds) Sonarr already tracks, from ONE /series fetch.

        TVDB is Sonarr's native key — what presence/exclusion matching uses. The TMDB set (Sonarr v4
        puts ``tmdbId`` on every series; empty on v3) lets callers reconcile tmdb-keyed records —
        the request inbox — against Sonarr without a per-title TVDB lookup.
        """
        data = self._get("/api/v3/series")
        return self._ids_from(data, "tvdbId"), self._ids_from(data, "tmdbId")

    def excluded_tvdb_ids(self) -> set[int]:
        """tvdbIds on Sonarr's import-exclusion list (usually left by a past delete)."""
        return self._id_set("/api/v3/importlistexclusion", "tvdbId")

    def add_series(self, tvdb_id: int, *, dry_run: bool, extra_tags: set[str] | None = None) -> tuple[str, str]:
        """Request one series by TVDB id. Returns (status, detail); never raises for a normal skip.

        ``extra_tags`` are per-user/per-row labels layered onto the target's global tag.
        status is one of: would_request (dry-run), requested, skipped_present, error.
        """
        results = self._get(f"/api/v3/series/lookup?term=tvdb:{tvdb_id}")
        resource = _match_tvdb(results, tvdb_id)
        if resource is None:
            return "error", "Sonarr could not find this title"
        if resource.get("id"):  # a non-zero id means Sonarr already tracks it
            return "skipped_present", "already in Sonarr"
        if dry_run:
            logger.info("[dry-run] Sonarr: would add tvdb {}", tvdb_id)
            return "would_request", "would add to Sonarr"
        body = {
            **resource,
            "qualityProfileId": self._target.quality_profile_id,
            "rootFolderPath": self._target.root_folder,
            "monitored": True,
            "seasonFolder": True,
            "tags": self._tag_ids(extra_tags),
            "addOptions": {"searchForMissingEpisodes": True, "monitor": "all"},
        }
        self._post("/api/v3/series", body)
        return "requested", "added to Sonarr and searching"


def make_arr_client(service: str, target: ArrTarget) -> _ArrClient:
    """Radarr for ``"radarr"``, Sonarr for ``"sonarr"`` — the one place that maps the name to a client."""
    return (RadarrClient if service == "radarr" else SonarrClient)(target)


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
