"""Overseerr / Jellyseerr client: file a REQUEST for a missing title instead of adding it to an Arr.

The two products share one API (``/api/v1``, ``X-Api-Key``), so one client serves both. The point of
routing here rather than at Radarr/Sonarr is that the *seerr owns the download apps: quality profile,
root folder, 4K routing and approval are its rules, and Shortlist stops having an opinion about them.

Two consequences worth knowing before reading the code:

* **Shows are keyed by TMDB id**, not TheTVDB — so none of the Arr path's TVDB crossing exists here,
  and neither does its ``skipped_no_tvdb`` outcome.
* **A request carries no tags.** ``POST /request`` accepts only
  ``mediaType, mediaId, tvdbId, seasons, is4k, serverId, profileId, rootFolder, languageProfileId,
  userId`` — there is no tags field, so Shortlist's ``requests.tag`` and per-person ``auto_user_tag``
  cannot travel this route. ``request_as_user_id`` is the attribution that replaces them.
"""

from __future__ import annotations

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry
from shortlist.engine.models import MediaType, SeerrTarget

#: ``MediaInfo.status``, mapped to the vocabulary the request inbox already speaks (the same four
#: words ``clients/arr.py`` produces).
#:
#: Only the codes that mean the same thing across the whole family are mapped, and that restraint is
#: load-bearing, because **the number 6 does not**. Overseerr's published spec calls it DELETED;
#: Seerr's own shipped `seerr-api.yml` says DELETED too — and its running code
#: (`/app/dist/constants/media.js`, read off a live 3.4.1) says:
#:
#:     UNKNOWN=1 PENDING=2 PROCESSING=3 PARTIALLY_AVAILABLE=4 AVAILABLE=5 BLOCKLISTED=6 DELETED=7
#:
#: So 6 is "the owner said never" on one product and "it was removed" on another — opposite meanings
#: for the same number, and the vendor's own spec is wrong about its own code. 7 is undocumented
#: everywhere yet accounted for 821 of 5,000 sampled rows on a real server.
#:
#: Everything unmapped therefore falls through to "not known", i.e. requestable, which is the safe
#: direction for a DELETED title. The blocklist is read from ``/blocklist`` instead of inferred from
#: a number that cannot be trusted — see ``blocklisted()``.
_STATUS_BY_CODE = {
    # Its own word, not "queued". The inbox renders "queued" as **Searching**, which is exactly right
    # for an Arr that is monitoring and hunting — and wrong here, where PENDING means the request is
    # sitting in the *seerr waiting for a person. That is the one state on this route the owner can
    # actually do something about, so it must not be dressed up as the machine working.
    2: "awaiting_approval",  # PENDING
    3: "queued",  # PROCESSING — approved and handed to the download app, which may not have it yet
    4: "queued",  # PARTIALLY_AVAILABLE — some of a show has landed; the rest is still wanted
    5: "downloaded",  # AVAILABLE
}

#: What "downloading" is actually decided by. ``PROCESSING`` is not it: on a real server 76 rows were
#: PROCESSING and exactly ONE was downloading — the rest are approved-but-unreleased films and airing
#: series, resting there indefinitely. ``downloadStatus`` is the download client's own live view
#: (it carries sizeLeft/timeLeft/estimatedCompletionTime), so a non-empty one is the only honest
#: "moving right now" signal the API offers.
_DOWNLOADING = "downloading"

#: ``mediaType`` as Overseerr spells it, per Shortlist ``MediaType``.
_MEDIA_TYPE = {MediaType.MOVIE: "movie", MediaType.SHOW: "tv"}

#: Overseerr's permission bits, read off a live Seerr 3.4.1 (`/app/dist/lib/permissions.js`) rather
#: than guessed — the same source that settled the status enum, and for the same reason: the
#: published spec documents `permissions` only as "a number".
#:
#: ADMIN is special. That file's own comment: "If the user has the admin permission, true will always
#: be returned from this check" — so an admin auto-approves everything without carrying any of the
#: AUTO_APPROVE bits, which is exactly the account most owners' API keys belong to.
_PERM_ADMIN = 2
_PERM_AUTO_APPROVE = 128
_PERM_AUTO_APPROVE_MOVIE = 256
_PERM_AUTO_APPROVE_TV = 512

#: 403 is a WORKING key whose account lacks a permission — not a bad key, which is what a shared
#: "rejected the API key" message said, sending owners off to regenerate a key that was fine.
#:
#: The permission is named PER CALL, because the two that a scoped key actually trips want different
#: ones: filing on behalf of another account needs Manage Requests, while listing the accounts to
#: choose from — the "Request as" dropdown itself — needs Manage Users. One shared message sent the
#: owner to grant the wrong permission on the very screen meant to diagnose it.
_FORBIDDEN = "{app} accepted the API key but refused this — its account needs the {permission} permission"
_MANAGE_REQUESTS = "Manage Requests"
_MANAGE_USERS = "Manage Users"


class SeerrError(RuntimeError):
    """An Overseerr/Jellyseerr call failed — connection, auth, or a rejected request.

    Never carries the URL or api key: the message is surfaced in the UI and written to events, and a
    *seerr api key is a secret like any other (plex-safety rule 9).
    """


class SeerrClient:
    """Talks to one Overseerr/Jellyseerr instance. Mirrors the shape of ``_ArrClient``."""

    app_name = "Overseerr"

    #: ``/media``, ``/user`` and ``/blocklist`` are paged. Sized from a measurement, not a guess: a
    #: real server holds 26,941 media rows, and walking it at Overseerr's own UI page size of 100 took
    #: 270 requests and 5.0s against 27 requests and 1.5s at 1000. The endpoint honours far larger
    #: values still (5,000 in 0.19s), but 1000 is where the request count stops being the cost.
    #:
    #: The cap is a hard stop for a server that ignores ``skip`` and answers with a full page for
    #: ever — 500k rows is far past any real library, so reaching it means paging is broken.
    _PAGE_SIZE = 1000
    _MAX_PAGES = 500

    def __init__(
        self,
        target: SeerrTarget,
        *,
        timeout: float = http_retry.DEFAULT_TIMEOUT_S,
        min_write_interval: float = 1.0,
        write_clock: list[float] | None = None,
    ):
        self._target = target
        self._base = target.url.rstrip("/")
        self._timeout = timeout
        self._min_write_interval = min_write_interval
        # Shared per SERVER by the caller, for the same reason the Arr clients share one: several
        # clients pointing at one instance must not multiply the write rate (plex-safety rule 6).
        self._write_clock = write_clock if write_clock is not None else [0.0]
        # Both memoised per client, and the error deliberately as well: without it a run with an
        # unreachable Overseerr re-walks /media (three HTTP retries deep) once per title it is about
        # to send, and every one of those walks fails for the same reason.
        self._media_state: dict[tuple[str, int], str] | None = None
        self._media_error: SeerrError | None = None
        self._blocklist: set[tuple[str, int]] | None = None

    @property
    def target(self) -> SeerrTarget:
        """Which instance this client talks to — so a caller can key a client cache by it."""
        return self._target

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._target.api_key}

    def _get(self, path: str, *, permission: str = _MANAGE_REQUESTS, **params: object) -> object:
        try:
            r = http_retry.get(
                f"{self._base}/api/v1{path}", headers=self._headers(), params=params or None, timeout=self._timeout
            )
        except httpx.HTTPError as e:
            raise SeerrError(f"{self.app_name} unreachable ({type(e).__name__})") from e
        if r.status_code == 401:
            raise SeerrError(f"{self.app_name} rejected the API key")
        if r.status_code == 403:
            raise SeerrError(_FORBIDDEN.format(app=self.app_name, permission=permission))
        if r.status_code != 200:
            raise SeerrError(f"{self.app_name} GET {path} returned HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            # A 200 carrying HTML is a reverse proxy or SSO interstitial, not the app. Say which,
            # because "expecting value: line 1" sends people to the wrong place entirely.
            raise SeerrError(f"{self.app_name} returned a non-JSON body — check the URL and any proxy") from e

    def _post(self, path: str, body: dict) -> dict:
        self._throttle()
        try:
            # Retried only where it provably never landed (connect error) or was rate-limited, never
            # on a read timeout — a retried request would file the title twice.
            r = http_retry.request(
                "POST", f"{self._base}/api/v1{path}", headers=self._headers(), json=body, timeout=self._timeout
            )
        except httpx.HTTPError as e:
            raise SeerrError(f"{self.app_name} unreachable ({type(e).__name__})") from e
        if r.status_code == 401:
            raise SeerrError(f"{self.app_name} rejected the API key")
        if r.status_code == 403:
            raise SeerrError(_FORBIDDEN.format(app=self.app_name, permission=_MANAGE_REQUESTS))
        if r.status_code >= 300:
            raise SeerrError(f"{self.app_name} refused the request (HTTP {r.status_code}): {_first_error(r)}")
        try:
            return r.json()
        except ValueError:
            return {}

    def _throttle(self) -> None:
        """At most one write per ``min_write_interval`` seconds — be a polite client (rule 6 spirit)."""
        self._write_clock[0] = http_retry.throttle(self._write_clock[0], self._min_write_interval)

    def whoami(self) -> int | None:
        """The id of the account this API key acts as, or None if it cannot be read.

        Needed because "Server default" in the UI means *this* account, and what it does — approve
        instantly or file for review — is the single most consequential thing on that screen. Without
        the id there is no way to look it up in the user list and say so.
        """
        try:
            me = self._get("/auth/me")
        except SeerrError as e:
            logger.debug("{}: could not identify the API key's own account ({})", self.app_name, e)
            return None
        return _int_or_none(me.get("id")) if isinstance(me, dict) else None

    def ping(self) -> str:
        """A tiny AUTHENTICATED call for the settings 'Test' button; returns a friendly line.

        Deliberately not ``/status``, which the API declares ``security: []`` — it answers 200 to an
        empty or wrong key, so testing against it would call a broken connection healthy.
        """
        me = self._get("/auth/me")
        who = _name_of(me) if isinstance(me, dict) else ""
        return f"Connected to {self.app_name} as {who or '?'}"

    def users(self) -> list[dict]:
        """``[{id, name}]`` for the 'request as' dropdown — every account the instance knows."""
        out: list[dict] = []
        for row in self._paged("/user", permission=_MANAGE_USERS):
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            perms = _int_or_none(row.get("permissions")) or 0
            out.append(
                {
                    "id": int(row["id"]),
                    "name": _name_of(row) or f"User {row['id']}",
                    # Whether THIS account's requests skip Overseerr's approval queue. Surfaced so the
                    # owner can see it when picking, instead of discovering it from where their
                    # titles ended up — the difference between "filed" and "already downloading".
                    "auto_approve_movies": _approves(perms, _PERM_AUTO_APPROVE_MOVIE),
                    "auto_approve_tv": _approves(perms, _PERM_AUTO_APPROVE_TV),
                }
            )
        return out

    def media_state(self) -> dict[tuple[str, int], str]:
        """Everything this instance knows about, as ``{(media_type, tmdb_id): status}``.

        One paged walk answers both questions the Arr path needs four calls for — "does it already
        have this?" and "what is this title's state?" — because Overseerr's media table already IS
        the union of the Plex library and everything requested.

        ``media_type`` is Shortlist's own vocabulary (``movie`` / ``show``), not Overseerr's
        (``movie`` / ``tv``), so callers can key it against ``MediaType.value`` directly.

        Memoised: the run asks once for the presence check and the inbox asks once for the status
        column, and one client should not walk the library twice.
        """
        if self._media_error is not None:
            raise self._media_error
        if self._media_state is None:
            try:
                self._media_state = self._fetch_media_state()
            except SeerrError as e:
                self._media_error = e
                raise
        return self._media_state

    def _fetch_media_state(self) -> dict[tuple[str, int], str]:
        state: dict[tuple[str, int], str] = {}
        rows = 0
        typed = 0
        for row in self._paged("/media"):
            if not isinstance(row, dict):
                continue
            rows += 1
            kind = _media_type_of(row)
            tmdb_id = _int_or_none(row.get("tmdbId"))
            if kind is None or tmdb_id is None:
                continue
            typed += 1
            if row.get("downloadStatus"):
                state[(kind, tmdb_id)] = _DOWNLOADING
                continue
            status = _STATUS_BY_CODE.get(_int_or_none(row.get("status")) or 0)
            if status is not None:
                state[(kind, tmdb_id)] = status
        if typed < rows:
            # `mediaType` is on the live response but NOT in the published `MediaInfo` schema, so a
            # fork or a future version dropping it lands exactly here — and an unusable row is
            # indistinguishable from a library that simply does not hold the title. The run still
            # fails open (a redundant request, never a wrong one), but it must not do so silently.
            #
            # Graded, not all-or-nothing: the guard used to fire only when NOT ONE row was usable,
            # so a version that dropped the field on half its rows passed silently — and half a
            # library quietly becoming re-requestable is the case worth hearing about. A handful of
            # odd rows on a healthy server is normal, so that stays at debug.
            level = "WARNING" if typed * 2 < rows else "DEBUG"
            logger.log(
                level,
                "{}: {} of {} media rows carried no usable mediaType + tmdbId — those titles are "
                "invisible to the already-have check, so Overseerr may be asked for them again",
                self.app_name,
                rows - typed,
                rows,
            )
        return state

    def _paged(self, path: str, *, permission: str = _MANAGE_REQUESTS) -> list[object]:
        """Walk a ``{pageInfo, results}`` endpoint to the end, honouring ``pageInfo.pages``."""
        out: list[object] = []
        for page in range(self._MAX_PAGES):
            payload = self._get(path, permission=permission, take=self._PAGE_SIZE, skip=page * self._PAGE_SIZE)
            results = payload.get("results") if isinstance(payload, dict) else None
            batch = results if isinstance(results, list) else []
            out.extend(batch)
            if len(batch) < self._PAGE_SIZE:
                return out
            info = payload.get("pageInfo") if isinstance(payload, dict) else None
            pages = _int_or_none(info.get("pages")) if isinstance(info, dict) else None
            if pages is not None and page + 1 >= pages:
                return out
        logger.warning(
            "{}: {} paging hit the {}-page safety cap — reporting a partial list", self.app_name, path, self._MAX_PAGES
        )
        return out

    def blocklisted(self) -> set[tuple[str, int]]:
        """Titles the owner has told this instance never to fetch, as ``{(media_type, tmdb_id)}``.

        The *seerr equivalent of an Arr import-exclusion list, which this route was documented as
        lacking — it does not lack it, it spells it differently. Read from the endpoint rather than
        inferred from ``MediaInfo.status``: BLOCKLISTED is 6 on Seerr/Jellyseerr and 6 is DELETED on
        Overseerr, so the number cannot tell the two apart while the endpoint always can.

        ``/blocklist`` is the current name and ``/blacklist`` the deprecated alias; older builds and
        classic Overseerr serve neither. Any failure yields an empty set — no exclusions, which is
        this whole module's fail-open direction: a redundant request, never a suppressed title.
        """
        if self._blocklist is not None:
            return self._blocklist
        self._blocklist = self._fetch_blocklist()
        return self._blocklist

    def _fetch_blocklist(self) -> set[tuple[str, int]]:
        for path in ("/blocklist", "/blacklist"):
            try:
                rows = self._paged(path)
            except SeerrError as e:
                logger.debug("{}: {} unavailable ({}) — no blocklist applied", self.app_name, path, e)
                continue
            out: set[tuple[str, int]] = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tmdb_id = _int_or_none(row.get("tmdbId"))
                if tmdb_id is None:
                    continue
                # `media` is a nested MediaInfo and carries the type. A row without one still counts
                # against BOTH types: half-knowing that the owner said never is not a reason to ask.
                media = row.get("media")
                kind = _media_type_of(media) if isinstance(media, dict) else None
                if kind is None:
                    out.update({(MediaType.MOVIE.value, tmdb_id), (MediaType.SHOW.value, tmdb_id)})
                else:
                    out.add((kind, tmdb_id))
            return out
        return set()

    def request_title(self, tmdb_id: int, media_type: MediaType, *, dry_run: bool) -> tuple[str, str, str | None]:
        """File one request. Returns ``(status, detail, slug)``; never raises for a normal skip.

        ``slug`` is None on this target — Overseerr's own URL for a title is
        ``/movie/<tmdbId>`` or ``/tv/<tmdbId>``, which the caller can build from what it already has,
        so there is no app-side slug to carry.

        status is one of: would_request (dry-run), requested, skipped_present, error — the same
        vocabulary the Arr path returns, so the inbox and the run report need no new cases.
        """
        kind = _MEDIA_TYPE.get(media_type)
        if kind is None:
            return "error", f"unsupported media type {media_type!r}", None
        try:
            known = self.media_state().get((media_type.value, tmdb_id))
        except SeerrError as e:
            # Fails OPEN, matching `_apply_seerr_state`. This check only saves a duplicate, and a
            # duplicate is far cheaper than turning every title in the run into an error — which is
            # what raising here did, silently undoing the reconcile's own fail-open a few lines
            # earlier. Overseerr refuses a genuine duplicate itself, and that lands as this one
            # title's outcome.
            logger.warning("{}: could not check what it already has ({}) — requesting anyway", self.app_name, e)
            known = None
        if known is not None:
            return "skipped_present", f"already in {self.app_name} ({known})", None
        if dry_run:
            logger.info("[dry-run] {}: would request {} tmdb {}", self.app_name, kind, tmdb_id)
            return "would_request", f"would request from {self.app_name}", None
        body: dict[str, object] = {"mediaType": kind, "mediaId": tmdb_id}
        if kind == "tv":
            # Without this Overseerr files a show request with no seasons, which it accepts and then
            # never sends to Sonarr — the request sits "approved" forever with nothing behind it.
            body["seasons"] = "all"
        if self._target.request_as_user_id:
            # Filing on behalf of another account needs MANAGE_REQUESTS; an admin key has it, and a
            # key that does not comes back 403 naming the permission (see `_FORBIDDEN`). Omitted
            # entirely when unset, rather than sent as null, so the instance applies its own default.
            body["userId"] = self._target.request_as_user_id
        self._post("/request", body)
        # Whether it lands as pending or auto-approved is the chosen account's permission, not ours —
        # so the detail says what happened here and lets the *seerr own the rest.
        return "requested", f"requested from {self.app_name}", None


def _approves(permissions: int, specific: int) -> bool:
    """Does this permission value auto-approve that media type?

    Three ways to hold it, and missing any one of them would mislabel a real account: ADMIN (which
    Overseerr treats as holding every permission), the blanket AUTO_APPROVE, or the per-type bit.
    """
    return bool(permissions & (_PERM_ADMIN | _PERM_AUTO_APPROVE | specific))


def _name_of(row: dict) -> str:
    """What to call one account, most human first; "" when the row names it in no way at all.

    ``displayName`` is what the live API and the Overseerr UI use, and it is NOT in the published
    ``User`` schema — though `GET /user`'s own `sort` enum offers `displayname`, so the field is
    real. The rest of the chain is what that schema does document, so a fork serving only the
    documented fields still names every account.
    """
    for key in ("displayName", "username", "plexUsername", "email"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _media_type_of(row: dict) -> str | None:
    """Overseerr's ``mediaType`` → Shortlist's ``MediaType.value``; None when absent or unrecognised.

    Undocumented in the published ``MediaInfo`` schema but present on every live response. Treated as
    optional rather than assumed, because getting it wrong would cross a movie's tmdb id with a
    show's — TMDB's two id spaces overlap, so id 550 is both a film and a series.
    """
    raw = row.get("mediaType")
    if raw == "movie":
        return MediaType.MOVIE.value
    if raw == "tv":
        return MediaType.SHOW.value
    return None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _first_error(response: httpx.Response) -> str:
    """Pull the app's own human message out of an error body, if there is one.

    Redacted on every path: this text lands in a ``SeerrError`` message, whose docstring promises no
    URL or api key appears in it.
    """
    try:
        payload = response.json()
    except ValueError:
        return http_retry.redact(response.text)[:200]
    # Redacted BEFORE truncating, never after: slicing can cut a secret pattern in half, and half a
    # pattern matches nothing — so `redact(text[:200])` is exactly how a key survives redaction.
    if isinstance(payload, dict):
        return http_retry.redact(str(payload.get("message") or payload))[:200]
    return http_retry.redact(str(payload))[:200]
