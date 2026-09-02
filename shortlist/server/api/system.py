"""System API: the operator's toolbox.

Health (the one unauthenticated endpoint, for Docker's HEALTHCHECK), version and update check, the
sync/backup schedule summary, the owner API token, the log viewer and its zip export, the diagnostics
bundle, the Plex library/collection reads the Rows editor offers, backups, the background-job queue,
and the full uninstall.

**Auth is declared once, at router construction**, exactly like every sibling router: ``_authed``
carries ``require_owner`` and everything hangs off it. ``/health`` is the sole exception — Docker's
HEALTHCHECK has no session — and FastAPI has no way to *drop* a router-level dependency for a single
route, so it lives on ``_public``, a bare router with nothing else on it.

Both are mounted onto the exported ``router`` at the BOTTOM of this module, which is deliberate:
``router`` does not exist while the handlers below are being defined, so a ``@router.get(...)``
written among them fails at import instead of quietly shipping an unauthenticated endpoint. This is
the router with ``POST /system/uninstall``, ``GET /system/debug`` and ``GET /system/api-token`` on it
— a forgotten gate here is the worst one in the app. Add new endpoints to ``_authed``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import secrets as pysecrets
import threading
import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger
from pydantic import BaseModel

import shortlist
from shortlist.engine.clients.http_retry import redact
from shortlist.logging_config import normalize_level
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import API_TOKEN_KEY, API_TOKEN_PREFIX, require_owner
from shortlist.server.db.models import Collection, Event, RestrictionSnapshotRow, User, iso_utc
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.scheduler import rebuild_schedule
from shortlist.server.services import jobs, log_reader
from shortlist.server.settings_store import SettingsStore

_TOKEN_CREATED_KEY = "api.token_created_at"

#: Owner-gated. Everything except `/health` goes here — see the module docstring.
_authed = APIRouter(dependencies=[Depends(require_owner)])
#: Unauthenticated, and it holds exactly one route. Nothing else may be added to it.
_public = APIRouter()


class VersionOut(PassthroughModel):
    """`GET /version`.

    ``extra="allow"`` is on EVERY response model in this file, nested ones included, and is not
    optional: a strict Pydantic response model silently DROPS any key the handler returned but the
    model does not declare. With it, an undeclared key passes through untouched — the model
    documents the shape without filtering it, so a field missed here cannot vanish from the payload
    and blank out a page.
    """

    current_version: str
    latest_version: str | None
    update_available: bool
    install_type: str
    #: Commit and ref this build came from — empty on a source checkout. A `:dev` image reports the
    #: same version number for every push between two releases, so these are the only fields that
    #: identify WHICH build is running.
    git_sha: str
    git_branch: str


@_authed.get("/version", response_model=VersionOut)
async def version(request: Request) -> dict:
    """Current + latest version and whether an update is available."""
    from shortlist.server.version_check import version_info

    return version_info()


class HealthOut(PassthroughModel):
    status: str


@_public.get("/health", response_model=HealthOut)
async def health() -> dict:
    """Liveness only — this is the one unauthenticated endpoint, and Docker's HEALTHCHECK is its
    consumer. The version used to be here too; an unauthenticated caller does not need to know which
    build to look up advisories for. The UI reads it from `/system/version`, which is owner-gated."""
    return {"status": "ok"}


class SyncStateOut(PassthroughModel):
    """One sync's schedule summary: when it last ran, when it fires next, and on what cron."""

    last: str | None
    next: str | None
    cron: str


class BackupScheduleOut(PassthroughModel):
    """Backups have no "last ran" line on the Tools page — the backup list itself is that answer."""

    next: str | None
    cron: str
    max_keep: int


class SyncsOut(PassthroughModel):
    watched: SyncStateOut
    users: SyncStateOut
    backup: BackupScheduleOut


@_authed.get("/syncs", response_model=SyncsOut)
async def syncs(request: Request) -> dict:
    """When each sync last ran and when it next fires — for the Tools page "last synced" lines."""
    from shortlist.server.scheduler import BACKUP_JOB_ID, USER_SYNC_JOB_ID, WATCH_SYNC_JOB_ID
    from shortlist.server.services.backup import DEFAULT_MAX_BACKUPS

    with request.app.state.sessions() as session:
        store = SettingsStore(session)
        last_watched = store.get("report.watch_synced_at")
        last_users = store.get("report.users_synced_at")
        watch_cron = store.get("sync.watch_cron")
        users_cron = store.get("sync.users_cron")
        backup_cron = store.get("backup.cron")
        backup_max_keep = store.get("backup.max_keep")
    scheduler = getattr(request.app.state, "scheduler", None)
    watch_job = scheduler.get_job(WATCH_SYNC_JOB_ID) if scheduler else None
    users_job = scheduler.get_job(USER_SYNC_JOB_ID) if scheduler else None
    backup_job = scheduler.get_job(BACKUP_JOB_ID) if scheduler else None
    return {
        "watched": {
            "last": last_watched,
            "next": iso_utc(watch_job.next_run_time) if watch_job and watch_job.next_run_time else None,
            "cron": watch_cron or "",
        },
        "users": {
            "last": last_users,
            "next": iso_utc(users_job.next_run_time) if users_job and users_job.next_run_time else None,
            "cron": users_cron or "",
        },
        "backup": {
            "next": iso_utc(backup_job.next_run_time) if backup_job and backup_job.next_run_time else None,
            "cron": backup_cron or "",
            "max_keep": backup_max_keep if isinstance(backup_max_keep, int) else DEFAULT_MAX_BACKUPS,
        },
    }


class ApiTokenStatusOut(PassthroughModel):
    """`GET /api-token`. No example or default is declared on `token` — a schema is documentation
    that ships to the browser, and a credential has no business in one (plex-safety rule 9)."""

    enabled: bool
    created_at: str | None
    token: str | None


@_authed.get("/api-token", response_model=ApiTokenStatusOut)
async def api_token_status(request: Request) -> dict:
    """The owner API token itself (decrypted, for the owner to reveal/copy — like Sonarr/Radarr's key),
    plus whether one exists and when it was made. Owner-gated; never exposed via GET /api/settings."""
    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        token = store.get(API_TOKEN_KEY)
        return {
            "enabled": bool(token),
            "created_at": store.get(_TOKEN_CREATED_KEY) or None,
            "token": token or None,
        }


class ApiTokenCreatedOut(PassthroughModel):
    token: str
    created_at: str


@_authed.post("/api-token", response_model=ApiTokenCreatedOut)
async def create_api_token(request: Request) -> dict:
    """Generate (or replace) the owner API token. Stored encrypted at rest; regenerating invalidates
    the previous token immediately."""
    token = API_TOKEN_PREFIX + pysecrets.token_urlsafe(32)
    created = datetime.now(UTC).isoformat()
    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        store.set(API_TOKEN_KEY, token)  # encrypted at rest via SECRET_KEYS
        store.set(_TOKEN_CREATED_KEY, created)
        # Audit the mint of an owner-level, CSRF-exempt credential — timestamp only, never the token
        # (plex-safety rule 10).
        session.add(Event(scope="api_token.create", level="info", message={"at": created}))
        session.commit()
    logger.info("owner API token (re)generated")  # NEVER log the token itself
    return {"token": token, "created_at": created}


class ApiTokenRevokedOut(PassthroughModel):
    enabled: bool


@_authed.delete("/api-token", response_model=ApiTokenRevokedOut)
async def revoke_api_token(request: Request) -> dict:
    """Revoke the API token — any script still using it starts getting 401s on the next call."""
    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        store.set(API_TOKEN_KEY, "")
        store.set(_TOKEN_CREATED_KEY, "")
        session.add(Event(scope="api_token.revoke", level="warning", message={"at": datetime.now(UTC).isoformat()}))
        session.commit()
    logger.info("owner API token revoked")
    return {"enabled": False}


class ImageProviderOut(PassthroughModel):
    capable: bool
    provider: str
    reason: str  # plain-English, user-facing; "" when capable


@_authed.get("/image-provider", response_model=ImageProviderOut)
async def image_provider(request: Request) -> dict:
    """Whether the configured AI provider can generate poster images (and a plain-English reason if
    not) — so the row editor can enable/disable the "Generate" poster option honestly."""
    from shortlist.server.services.poster_service import image_provider_status
    from shortlist.server.settings_store import SettingsStore

    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        return image_provider_status(store)


class LogLineOut(PassthroughModel):
    """One parsed log entry. `ts` is None for a line the parser could not date (a raw traceback)."""

    ts: str | None
    level: str
    source: str
    message: str


class LogsOut(PassthroughModel):
    lines: list[LogLineOut]
    total_matched: int
    truncated: bool
    file: str | None  # None when there is no log file yet


@_authed.get("/logs", response_model=LogsOut)
async def logs(request: Request, level: str = "DEBUG", q: str = "", limit: int = 1000) -> dict:
    """The rotating log file, parsed and filtered — so a problem can be diagnosed from the app
    instead of `docker logs`.

    Every line is redacted before it leaves the server: this view exists to be copied and shared
    (that is what the export button is for), so it must never be the thing that leaks a token.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: log_reader.read_lines(
            request.app.state.config_dir,
            level=normalize_level(level),
            query=q,
            limit=max(1, min(limit, 5000)),
        ),
    )


@_authed.get("/logs/download")
async def logs_download(request: Request) -> Response:
    """Every log file as a redacted zip — the attachment for a bug report."""
    from shortlist.server.services.redaction import known_identifiers

    # Read here and passed in: `build_zip` redacts this server's own machine id and address by exact
    # match — the only pass a novel escaping cannot slip past — and it has no session to look them up
    # with. A comment rather than docstring prose: FastAPI publishes the docstring as this endpoint's
    # OpenAPI description, and internal plumbing does not belong in the API contract.
    with request.app.state.sessions() as session:
        literals = known_identifiers(session)
    payload = await asyncio.get_running_loop().run_in_executor(
        None, lambda: log_reader.build_zip(request.app.state.config_dir, literals)
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="shortlist-logs-{stamp}.zip"'},
    )


@_authed.get("/debug", response_class=PlainTextResponse)
async def debug_bundle(request: Request) -> str:
    """A pasteable diagnostics bundle for bug reports: version, DB migration head, scheduler jobs,
    connection status, and record counts. Deliberately plain text and secrets-free — every connection
    is reported as a yes/no, never a token or key (plex-safety rule 9)."""
    from sqlalchemy import func, text

    from shortlist.server.db.models import PickRow, RequestCandidate, Run
    from shortlist.server.settings_store import SettingsStore
    from shortlist.server.version_check import build_provenance

    lines: list[str] = ["=== Shortlist debug bundle ===", f"version: {shortlist.__version__}"]
    # The FIRST question on any `:dev` bug report — the version number is identical for every push
    # between two releases, so without the commit there is no way to know which code is running.
    git_sha, git_branch = build_provenance()
    lines.append(f"build: {git_branch or '(source checkout)'} {git_sha or ''}".rstrip())
    lines.append(f"python: {platform.python_version()} on {platform.system()} {platform.machine()}")
    lines.append(f"time: {datetime.now(UTC).isoformat()}  TZ={os.environ.get('TZ', '(unset)')}")

    with request.app.state.sessions() as session:
        # With the secret box: `tmdb.apikey` below is a SECRET_KEY, and a boxless store now refuses
        # those outright rather than silently handing back ciphertext. The value is only ever
        # `bool()`-ed here — it is never rendered into the bundle (rule 9).
        store = SettingsStore(session, request.app.state.secrets)
        head = session.execute(text("select version_num from alembic_version")).scalar()
        lines.append(f"db migration head: {head}")

        counts = {
            "users": session.query(func.count(User.id)).scalar(),
            "rows": session.query(func.count(Collection.id)).scalar(),
            "runs": session.query(func.count(Run.id)).scalar(),
            "picks": session.query(func.count(PickRow.id)).scalar(),
            "requests": session.query(func.count(RequestCandidate.id)).scalar(),
            "restriction snapshots": session.query(func.count(RestrictionSnapshotRow.user_id)).scalar(),
        }
        lines.append("counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

        # Connections — configured yes/no ONLY, never the value; the sole exception is the curator
        # PROVIDER NAME ("anthropic"/"openai"/…), which is non-secret and useful in a bug report (the
        # curator API key is never read here).
        conns = {
            "plex": bool(store.get("plex.url")),
            "tautulli": bool(store.get("tautulli.url")),
            "tmdb": bool(store.get("tmdb.apikey")),
            "curator": store.get("curator.provider"),
            "requests": bool(store.get("requests.enabled")),
            "request_target": store.get("requests.target"),
            "overseerr": bool(store.get("requests.overseerr.url")),
            "radarr": bool(store.get("requests.radarr.url")),
            "sonarr": bool(store.get("requests.sonarr.url")),
        }
        lines.append("connections: " + ", ".join(f"{k}={v}" for k, v in conns.items()))
        lines.append(f"paused: {bool(store.get('paused_all'))}  log level: {store.get('log.level')}")

        last = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
        if last:
            lines.append(f"last run: #{last.id} {last.status} at {iso_utc(last.finished_at)} ({last.stats or {}})")

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        jobs = [f"{j.id}→{iso_utc(j.next_run_time)}" for j in scheduler.get_jobs()]
        lines.append("scheduled jobs: " + (", ".join(jobs) if jobs else "(none)"))

    lines.append("=== end ===")
    return "\n".join(lines)


class LibraryOut(PassthroughModel):
    key: str
    title: str
    #: Closed by the READ, not by Plex: `PlexClient.sections()` defaults to `("movie", "show")` and
    #: filters everything else out, so a music or photo library never reaches this response.
    type: Literal["movie", "show"]


#: How long a Plex library read is served from memory before going back to the PMS.
#:
#: These two endpoints are on the PAGE-LOAD path — `/libraries` backs every row card on the Rows
#: page, the library picker, and the placement settings — and each call is a fresh PlexServer
#: handshake plus a `/library/sections` read. That is fine against an idle PMS and ruinous against a
#: busy one: Plex serialises against its own database, so while a job was deleting collections one
#: DELETE took 15.8s and every page that wanted a library list waited behind it (SFLIX 2026-08-04).
#: The list itself changes when someone adds a library — minutes of staleness costs nothing.
_PLEX_READ_TTL_S = 120.0

#: A far shorter timeout than a run's `plex.timeout_s` (default 45s). A run is right to wait out a
#: slow PMS; a page is not — past a few seconds the tab looks broken, and the person retries, which
#: is the last thing an overloaded server needs.
_INTERACTIVE_TIMEOUT_S = 8


def invalidate_plex_reads(state) -> None:
    """Forget every cached Plex read. Called when the connected server may have changed.

    The cache key is the READ, not the server — so after re-pointing Shortlist at a different PMS the
    old server's library list would have been served for up to the TTL. Harmless (both endpoints are
    owner-only and read the owner's own server, and nothing cached decides a write) but confusing:
    the picker offers libraries the new server does not have.
    """
    state.__dict__.pop("_plex_read_cache", None)
    state.__dict__.pop("_plex_read_locks", None)


def _cached_plex_read(state, key: str, read):
    """Read from the PMS at most once per `_PLEX_READ_TTL_S` per key, and never twice at once.

    Three behaviours, each earning its keep on a server that is busy rather than one that is idle:

    * **TTL** — the common case never touches Plex at all.
    * **Single-flight** — concurrent misses collapse into ONE read. Without it a slow PMS makes
      things worse the more people look: ten page loads become ten enumerations of a server that is
      already the bottleneck.
    * **Serve-stale-on-failure** — if the refresh raises (a timeout on a busy server), the previous
      value is returned rather than an error. A library list a couple of minutes old is a much better
      answer than a broken page, and the next call retries. Nothing here is used to decide a write.

    Only for READS whose staleness is harmless. Never cache something a mutation is about to act on.
    """
    cache = state.__dict__.setdefault("_plex_read_cache", {})
    locks = state.__dict__.setdefault("_plex_read_locks", {})
    lock = locks.setdefault(key, threading.Lock())

    entry = cache.get(key)
    if entry and entry[0] > time.monotonic():
        return entry[1]

    with lock:
        # Re-checked with the lock held: whoever we queued behind has just refreshed it.
        entry = cache.get(key)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        try:
            value = read()
        except HTTPException:
            # "Plex isn't connected" / "no such library" is an answer, not a failure to paper over.
            # The lock goes with it: `key` carries a caller-supplied path segment, so keeping one per
            # value ever asked for would grow this dict for as long as the process lives. Anyone
            # already waiting holds their own reference, so dropping it here is safe — the worst case
            # is one extra concurrent read of a key that just 404'd.
            if key not in cache:
                locks.pop(key, None)
            raise
        except Exception as e:
            if entry is None:
                # Same reasoning as the HTTPException arm: nothing was cached, so this key leaves no
                # entry behind and its lock must go with it. The failure that actually grows the dict
                # lands HERE rather than there — a bogus library key while the PMS is timing out
                # raises a plexapi error, not an HTTPException.
                if key not in cache:
                    locks.pop(key, None)
                raise
            logger.warning("plex read {} failed ({}) — serving the cached copy", key, type(e).__name__)
            return entry[1]
        cache[key] = (time.monotonic() + _PLEX_READ_TTL_S, value)
        return value


@_authed.get("/libraries", response_model=list[LibraryOut])
async def libraries(request: Request) -> list[dict]:
    """The server's movie/show libraries, so the Rows editor can offer them as delivery targets."""
    from shortlist.engine.clients.plex_pms import PlexClient
    from shortlist.server.settings_store import SettingsStore

    state = request.app.state

    def read() -> list[dict]:
        with state.sessions() as session:
            store = SettingsStore(session, state.secrets)
            url, token = store.get("plex.url"), store.get("plex.token")
        if not url or not token:
            raise HTTPException(status_code=409, detail="Plex isn't connected yet")
        client = PlexClient(url, token, timeout=_INTERACTIVE_TIMEOUT_S)
        return [{"key": str(s.key), "title": s.title, "type": s.type} for s in client.sections()]

    return await asyncio.get_running_loop().run_in_executor(None, lambda: _cached_plex_read(state, "libraries", read))


class LibraryCollectionOut(PassthroughModel):
    """A candidate anchor. Title, because the shelf is ordered by title, not by rating key — plus
    whether it has a position on a Plex shelf at all, which decides if it can anchor anything."""

    title: str
    #: False ONLY for a collection Plex reports as promoted nowhere. Plex's own built-in hubs are
    #: always True: the engine never refuses one, so the editor must not grey one out either.
    on_shelf: bool


@_authed.get("/libraries/{key}/collections", response_model=list[LibraryCollectionOut])
async def library_collections(key: str, request: Request) -> list[dict]:
    """A library's FOREIGN managed (orderable) collections — the candidate anchors for placing
    Shortlist rows in the Recommended shelf that are not themselves ours.

    Our own collections are excluded, because a Shortlist row is never anchored by TITLE: it is one
    collection PER PERSON, so a title names one account's copy and would place the row for that one
    account and nobody else. Anchoring to another Shortlist row is done by row slug instead
    (`hub_anchor[library].row`), which the editor offers alongside this list — that is issue #81, and
    forty identical-looking "Picked for You" entries in this list was the symptom.

    Excluded by the invisible title MARKER, never by reading labels. `collection.labels` makes plexapi
    silently re-read each collection and a read that comes back carrying no <Label> is
    indistinguishable from an unlabelled row (plex-safety rule 4) — on those page loads the old
    label-based filter emptied and every Shortlist row appeared here as a selectable anchor. That is
    how the reporter came to have one saved: the option was a flicker, and it never placed anything.
    The marker is in the title we already have, so it cannot fail that way.
    """
    from shortlist.engine.clients.plex_pms import PlexClient, can_anchor, has_shortlist_marker
    from shortlist.server.settings_store import SettingsStore

    state = request.app.state

    def read() -> list[dict]:
        with state.sessions() as session:
            store = SettingsStore(session, state.secrets)
            url, token = store.get("plex.url"), store.get("plex.token")
        if not url or not token:
            raise HTTPException(status_code=409, detail="Plex isn't connected yet")
        client = PlexClient(url, token, timeout=_INTERACTIVE_TIMEOUT_S)
        section = next((s for s in client.sections() if str(s.key) == key), None)
        if section is None:
            raise HTTPException(status_code=404, detail="library not found")
        # `on_shelf` decides whether an anchor can work at all. `managedHubs()` lists every hub the
        # library CAN manage, and a COLLECTION promoted nowhere has no position on the shelf —
        # following it buries the row (issue #106), which is why the engine now refuses to. The editor
        # still shows them, greyed out and labelled, rather than dropping them: an owner who cannot
        # see the collection they picked last week has no way to tell "not on the shelf" from
        # "deleted".
        #
        # `can_anchor` is the ENGINE's own predicate, imported rather than restated: this endpoint
        # exists to predict what the ordering pass will do, and a second copy of the rule is a
        # disagreement waiting to happen.
        #
        # OR-accumulated per title, because the engine scans every hub with that title and takes the
        # first that can anchor. Two hubs CAN share one ("Top Rated" is both a stock Plex hub and a
        # stock Kometa collection), and first-hub-wins would grey out an anchor that places fine.
        seen: dict[str, bool] = {}
        for hub in section.managedHubs():
            title = getattr(hub, "title", "") or ""
            if not title or has_shortlist_marker(title):
                continue
            seen[title] = seen.get(title, False) or can_anchor(hub)
        return [{"title": t, "on_shelf": on_shelf} for t, on_shelf in seen.items()]

    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: _cached_plex_read(state, f"collections:{key}", read)
    )


class OwnedCollectionOut(PassthroughModel):
    library: str
    title: str
    label: str
    rating_key: int
    kind: Literal["user", "shared"]  # a per-person row's label, or a shared row's own slug
    slug: str
    orphan: bool  # its user or shared row is gone from the app — safe to remove


class OwnedCollectionsOut(PassthroughModel):
    collections: list[OwnedCollectionOut]
    total: int
    orphans: int


@_authed.get("/owned-collections", response_model=OwnedCollectionsOut)
async def owned_collections_audit(request: Request) -> dict:
    """Read-only cleanup audit: every Shortlist-labelled collection currently on Plex, one per entry.
    Each is flagged ``orphan`` when the label's owner is gone from the app — the USER for a per-person
    row (all of a user's rows share their one label, so this is user-level), or the SHARED ROW for a
    shared collection (1:1 with its slug). Independent of the database — this is exactly what a
    cleanup/uninstall finds and removes, so the owner can eyeball nothing has drifted (rule 10)."""
    from shortlist.engine.clients.plex_pms import PlexClient
    from shortlist.engine.delivery import strip_marker
    from shortlist.engine.models import SHARED_LABEL_PREFIX
    from shortlist.server.db.models import Collection as Coll
    from shortlist.server.db.models import User
    from shortlist.server.settings_store import SettingsStore

    state = request.app.state

    def read() -> dict:
        with state.sessions() as session:
            store = SettingsStore(session, state.secrets)
            url, token = store.get("plex.url"), store.get("plex.token")
            user_slugs = {u.slug for u in session.query(User).all()}
            coll_slugs = {c.slug for c in session.query(Coll).all()}
        if not url or not token:
            raise HTTPException(status_code=409, detail="Plex isn't connected yet")

        shared_prefix = SHARED_LABEL_PREFIX.lower()
        out: list[dict] = []
        for row in PlexClient(url, token).list_owned_collections("shortlist"):
            label = row["label"].lower()
            if label.startswith(shared_prefix):
                slug, kind, known = label[len(shared_prefix) :], "shared", label[len(shared_prefix) :] in coll_slugs
            else:
                slug = label[len("shortlist_") :]
                kind, known = "user", slug in user_slugs
            out.append(
                {
                    "library": row["library"],
                    "title": strip_marker(row["title"]),
                    "label": row["label"],
                    "rating_key": row["rating_key"],
                    "kind": kind,
                    "slug": slug,
                    "orphan": not known,  # its user (per-person) or shared row is gone from the app — safe to remove
                }
            )
        # Orphans first (the ones worth a look), then by library and title.
        out.sort(key=lambda x: (not x["orphan"], x["library"], x["title"]))
        return {"collections": out, "total": len(out), "orphans": sum(1 for x in out if x["orphan"])}

    return await asyncio.get_running_loop().run_in_executor(None, read)


class UninstallRequest(BaseModel):
    confirm: str = ""
    dry_run: bool = False  # preview: report what WOULD be restored/deleted (rule 8)


class UninstallSkippedOut(PassthroughModel):
    """An account that has left this Plex server, so its snapshot can never be restored."""

    user: str
    plex_account_id: int
    reason: str


class UninstallFailedOut(PassthroughModel):
    """An account plex.tv refused. The rest of the uninstall still ran (issue #96)."""

    user: str
    error: str


class UninstallUnreachableOut(PassthroughModel):
    """An account plex.tv's roster did not list, but that our own records say IS on this server.

    Its own field rather than folded into `filters_failed`, because the two need different things
    from the operator — a refused write is worth retrying, an absent roster entry means plex.tv gave
    an answer we don't believe — and a consumer should never have to match on an error string to
    tell them apart.
    """

    user: str
    plex_account_id: int
    reason: str


class UninstallOut(PassthroughModel):
    filters_restored: int
    filters_skipped: list[UninstallSkippedOut]  # gone for good — named so the report is honest
    filters_unreachable: list[UninstallUnreachableOut]  # roster disagreed with us — worth retrying
    filters_failed: list[UninstallFailedOut]
    collections_deleted: list[str]  # titles, so the preview names what would go
    rows_disabled: int
    dry_run: bool
    message: str


def _uninstall_message(result: dict, *, dry_run: bool) -> str:
    """The one line the Uninstall page shows, and the only one an API consumer gets.

    "Your server is as we found it" is the whole claim the operator is trusting, so it is said only
    when it is TRUE. Every caveat composes rather than short-circuits: an uninstall with both
    failures and departures used to report only the failures, so the departed accounts vanished from
    every consumer reading this line rather than the page — the API and the audit event included.
    """
    skipped = len(result["filters_skipped"])
    unreachable = len(result["filters_unreachable"])
    failed = len(result["filters_failed"])

    def accounts(n: int) -> str:
        return f"{n} account{'' if n == 1 else 's'}"

    # plex.tv listed NONE of the accounts on file. Two readings, and nothing here can tell them
    # apart: the owner really has stopped sharing with everybody, or the roster read failed. Saying
    # "run it again to retry" sends an owner in the first case round a loop that can never close —
    # `user_sync` never stamps `departed_at` from an empty roster, so their last account can never
    # become corroborated and would report this on every attempt, for ever.
    #
    # Gated on how many accounts the ROSTER carried, never on how many were restored. `restored`
    # counts accounts that NEEDED a write, so an account already matching its snapshot doesn't
    # increment it — and a second uninstall run (the retry the page instructs) has everyone matching
    # already. Keyed on `restored`, that run told an operator whose roster was perfectly healthy that
    # they might have unshared everybody, which is the opposite of the truth and stops them retrying
    # the one account that still carries our labels.
    if unreachable and not result["accounts_listed"]:
        lead = "Preview only — nothing was changed. " if dry_run else ""
        on_file = accounts(unreachable + skipped)
        return (
            f"{lead}plex.tv listed none of the {on_file} on file. If you have already stopped sharing "
            "this server with those people, there is nothing left to put back — otherwise this looks "
            "like a failed read, so try again."
        )

    parts = []
    if failed:
        parts.append(f"{failed} share filter{'' if failed == 1 else 's'} could not be restored — see the event log.")
    if unreachable:
        says = "is" if unreachable == 1 else "are"
        parts.append(f"plex.tv did not list {accounts(unreachable)} our records say {says} on this server.")
    if skipped:
        parts.append(f"{accounts(skipped)} {'has' if skipped == 1 else 'have'} since left this server.")

    if dry_run:
        # The preview is the rehearsal the FAQ tells people to trust (rule 8). Swallowing "plex.tv
        # could not see N of your accounts" here hides the one signal that should make an operator
        # wait before typing UNINSTALL.
        return " ".join(["Preview only — nothing was changed.", *parts])
    if not parts:
        return "Your server is as we found it."
    if failed or unreachable:
        return " ".join(["Finished, with some accounts left over:", *parts])
    return " ".join(["Your server is as we found it, apart from this:", *parts])


@_authed.post("/uninstall", response_model=UninstallOut)
async def uninstall(body: UninstallRequest, request: Request) -> dict:
    """Trust feature: restore every snapshot, delete every shortlist collection, disable every row
    and clear its schedule so nothing rebuilds, and report.

    dry_run=true previews the plan; the real thing requires the literal confirmation
    string UNINSTALL — this is the one deliberately scary button in the product.
    """
    if force_dry_run():
        # Safe mode (a demo/test instance pointed at a real server): uninstall only ever previews —
        # never restores share filters or deletes collections on the real server.
        body.dry_run = True
    if not body.dry_run and body.confirm != "UNINSTALL":
        raise HTTPException(status_code=422, detail='type "UNINSTALL" to confirm')
    state = request.app.state
    # An ALREADY-RUNNING engine run is the one case row-disable-first cannot cover: clearing the
    # schedule does not cancel a run in flight, and that run holds a pre-loaded roster and row list
    # it will keep merging `label!=shortlist_*` from — into accounts this uninstall has just
    # restored.
    #
    # The policy is: 409 for a RUN, the writer lock (below) for everything else. A run can last many
    # minutes — one TV-library write alone costs ~16s on a real server — so waiting on the lock would
    # leave the request hanging with nothing on screen to say why. A writer JOB is short enough to
    # wait for, and the lock covers it.
    if not body.dry_run and state.run_service.is_running():
        raise HTTPException(
            status_code=409,
            detail="A run is in progress — wait for it to finish or cancel it, then uninstall.",
        )
    loop = asyncio.get_running_loop()

    def emit(label: str, **extra: object) -> None:
        # Stream one live step to the SSE bus from the executor thread, so the Uninstall page shows
        # exactly what's happening (like the run activity log). Real uninstall only — the dry-run
        # preview is instant and needs no stream.
        if not body.dry_run:
            loop.call_soon_threadsafe(state.bus.publish, "uninstall.progress", {"label": label, **extra})

    # PHASE 1, before anything reaches Plex: switch every row off and clear its schedule.
    #
    # First, not last, and that ordering is load-bearing. It is a local DB write — the cheapest and
    # most reversible step in the whole flow — and doing it first means every failure below lands on
    # a Shortlist that is genuinely switched off. Run it last (as this did) and a crash anywhere in
    # the Plex phases leaves `rows_disabled == 0` with the schedule still armed, so the nightly run
    # rebuilds the collections and re-merges the excludes the operator just paid to remove. The 500
    # says "run it again to finish the rest"; the scheduler could get there first.
    with state.sessions() as session:
        enabled_rows = session.query(Collection).filter_by(enabled=True).all()
        rows_disabled = len(enabled_rows)
        if not body.dry_run:
            for row in enabled_rows:
                row.enabled = False
            session.commit()
    if not body.dry_run:
        rebuild_schedule(request.app)
        emit(f"Switched off {rows_disabled} row{'' if rows_disabled == 1 else 's'} and cleared their schedules")

    def do_uninstall() -> tuple[dict, list[dict], Exception | None]:
        from shortlist.engine.models import FilterSnapshot
        from shortlist.engine.privacy import (
            RestoreVerificationError,
            resolve_restore_targets,
            restore_user_restrictions,
        )

        per_user_events: list[dict] = []
        restored = 0
        skipped: list[dict] = []
        unreachable_out: list[dict] = []
        failed: list[dict] = []
        accounts_listed = 0
        deleted: list[str] = []

        def report() -> dict:
            # Built from whatever has actually happened so far, so a run that dies partway still
            # returns a truthful report to be audited (rule 10) instead of losing it with the frame.
            return {
                "filters_restored": restored,
                "filters_skipped": skipped,
                "filters_unreachable": unreachable_out,
                "filters_failed": failed,
                "collections_deleted": deleted,
                "rows_disabled": rows_disabled,
                "dry_run": body.dry_run,
                # How many of our snapshotted accounts plex.tv's roster carried. Stripped from the
                # API response, but KEPT in the audit event on purpose: it is what distinguishes a
                # failed roster read from a no-op run after the fact. It exists because the summary
                # line has to tell "the roster listed nobody" apart from "nobody needed a write",
                # which produce the same number of restores.
                "accounts_listed": accounts_listed,
            }

        # Built here, inside the guard, rather than at the top: Phase 1 has ALREADY switched every
        # row off by this point, so a failure to build a Plex client (no server row, a token that
        # will not decrypt) must still reach the audit log and the "your rows are switched off"
        # message — not escape the executor as a bare 500 that says nothing about what it left behind.
        try:
            ctx = state.run_service.build_context(dry_run=body.dry_run)
        except Exception as e:
            return report(), per_user_events, e

        # PHASE 2: delete the rows BEFORE restoring the filters that hide them.
        #
        # Rule 1 in reverse. Restoring first strips each account's `label!=shortlist_*` while the rows
        # still exist and are still promoted to Shared Home — issue #88's exact state, for as long as
        # a full section+collection walk takes. Deleting first cannot leak: a failure here leaves the
        # excludes in place, which is an omission, not exposure. There is no failure mode where
        # restore-first wins.
        try:
            emit("Reading your Plex libraries to find Shortlist collections…")
            for section in ctx.plex.sections():
                for collection in section.collections():
                    if any(label.tag.lower().startswith("shortlist_") for label in collection.labels):
                        deleted.append(collection.title)
                        if not body.dry_run:
                            emit(f"Deleting collection “{collection.title}” from Plex…")
                            ctx.plex.delete_owned_collection(collection, "shortlist")
        except Exception as e:
            return report(), per_user_events, e

        # PHASE 3: put every account's share filters back.
        with state.sessions() as session:
            users = {u.id: u for u in session.query(User).all()}
            snapshots = [
                FilterSnapshot(
                    plex_account_id=user.plex_account_id,
                    username=user.username,
                    taken_at=row.taken_at,
                    filters=row.filters_before,
                )
                for row in session.query(RestrictionSnapshotRow).filter_by(reason="initial").all()
                # A snapshot whose users row has gone: nothing left to name it by (migration 0064).
                if (user := users.get(row.user_id)) is not None
            ]
            # What our OWN records say about who has left, so "the roster omits them" can be
            # corroborated rather than guessed at. See `resolve_restore_targets`.
            believed_departed = frozenset(
                u.plex_account_id
                for u in session.query(User).filter((User.departed_at.isnot(None)) | (User.removed_at.isnot(None)))
            )

        if snapshots:
            # Narrated BEFORE the read, not after: this is a plex.tv round-trip. Resolving first left
            # the page silent through it — on the scariest button in the product a wordless pause
            # reads as a hang.
            emit("Checking which of these accounts are still on your Plex server…")
        try:
            targets, departed, unreachable = resolve_restore_targets(
                ctx.plextv, snapshots, believed_departed=believed_departed
            )
        except Exception as e:
            return report(), per_user_events, e
        # Set the moment `targets` exists, so nothing between here and the summary line can leave it
        # reading a stale 0 — which would report "plex.tv listed nobody" over a healthy roster.
        accounts_listed = len(targets)

        for snapshot in departed:
            logger.info("uninstall: {} is no longer on this Plex server — nothing to restore", snapshot.username)
            skipped.append(
                {
                    "user": snapshot.username,
                    "plex_account_id": snapshot.plex_account_id,
                    "reason": "no longer on this Plex server",
                }
            )
        for snapshot in unreachable:
            # Distinct from departed on purpose: our records say this account IS here, so a roster
            # that omits it is a disagreement, not a departure. Their filters keep Shortlist's
            # entries, and saying so is the only thing that makes a partial read actionable.
            logger.warning("uninstall: plex.tv did not list {}, who our records say is here", snapshot.username)
            unreachable_out.append(
                {
                    "user": snapshot.username,
                    "plex_account_id": snapshot.plex_account_id,
                    "reason": "plex.tv did not list this account, but our records say it is on this server",
                }
            )

        total = len(targets)
        if total:
            emit(
                f"Restoring share filters for {total} user{'' if total == 1 else 's'} via plex.tv "
                f"(as fast as plex.tv accepts; backs off only if rate-limited)…"
            )
        if skipped:
            emit(
                f"Skipping {len(skipped)} account{'' if len(skipped) == 1 else 's'} no longer on your server — "
                "their settings can't be reached to restore"
            )
        if unreachable_out:
            # Narrated too, not only reported at the end. The live log is what the operator watches;
            # without this, accounts in this bucket are simply missing from it — "restoring 38 users"
            # on a 40-account server, with nothing saying where the other two went.
            emit(
                f"plex.tv did not list {len(unreachable_out)} account"
                f"{'' if len(unreachable_out) == 1 else 's'} our records say are on this server — "
                "their filters keep Shortlist's entries until a retry"
            )
        for i, (snapshot, remote) in enumerate(targets, 1):
            emit(f"[{i}/{total}] Restoring {snapshot.username}'s share filter on plex.tv…")
            try:
                if restore_user_restrictions(ctx.plextv, snapshot, remote, dry_run=body.dry_run):
                    restored += 1
                    per_user_events.append(
                        {"user": snapshot.username, "restored_to": snapshot.filters, "dry_run": body.dry_run}
                    )
                    emit(f"    ✓ {snapshot.username} restored", done=restored, total=total)
            except Exception as e:
                # One account must never abort the rest. The operator has already typed UNINSTALL, and
                # every account the loop does not reach keeps Shortlist's excludes for ever (#96).
                detail = redact(f"{type(e).__name__}: {e}")
                logger.error("uninstall: {} could not be restored — {}", snapshot.username, detail)
                failed.append({"user": snapshot.username, "error": detail})
                # A write that plex.tv ACCEPTED and we then failed to verify still changed that
                # account, so it is audited as a write we could not confirm rather than dropped
                # (rule 10). Only RestoreVerificationError means the write may have reached plex.tv;
                # a failure before the PUT changed nothing and has nothing to record.
                if isinstance(e, RestoreVerificationError):
                    per_user_events.append(
                        {
                            "user": snapshot.username,
                            "attempted": e.attempted,
                            "verified": False,
                            "error": detail,
                            "dry_run": body.dry_run,
                        }
                    )
                emit(f"    ✗ {snapshot.username} could not be restored — {detail}")

        return report(), per_user_events, None

    # The one-writer lock for Plex/plex.tv, the same one engine runs and writer jobs take. A real
    # uninstall deletes collections and merges share filters, so it is a writer like any other —
    # without this a privacy sync firing mid-uninstall re-merges the `label!=shortlist_*` excludes
    # onto accounts the restore loop has already put back, silently, with nothing to catch it since
    # the Privacy Check was removed (2026-07-16). Held across the whole Plex phase, not per user.
    #
    # A PREVIEW never takes it. It writes nothing, so it has no business holding the one-writer lock
    # — and taking it would quietly undo the `not body.dry_run` gate on the 409 above, leaving the
    # preview to spin for the length of a run with nothing on screen to explain why.
    writer = jobs.plex_writer_lock() if not body.dry_run else contextlib.nullcontext()
    async with writer:
        result, per_user, fatal = await asyncio.get_running_loop().run_in_executor(None, do_uninstall)
    with state.sessions() as session:
        for entry in per_user:
            session.add(Event(scope="uninstall.user", level="warning", message=entry))
        summary = {**result, "at": datetime.now(UTC).isoformat()}
        if fatal is not None:
            # Without this the audit row asserts a clean uninstall that actually died partway.
            summary["stopped_by"] = redact(f"{type(fatal).__name__}: {fatal}")
        session.add(Event(scope="system.uninstall", level="warning", message=summary))
        session.commit()
    if fatal is not None:
        detail = redact(f"{type(fatal).__name__}: {fatal}")
        logger.error("UNINSTALL stopped partway: {} — partial result {}", detail, result)
        done = result["filters_restored"]
        raise HTTPException(
            status_code=500,
            detail=(
                f"Uninstall stopped after restoring {done} share filter{'' if done == 1 else 's'}: {detail}. "
                "Your rows are switched off and what it did change is in the event log — "
                "run it again to finish the rest."
            ),
        ) from fatal
    logger.warning("UNINSTALL {}: {}", "preview" if body.dry_run else "executed", result)
    message = _uninstall_message(result, dry_run=body.dry_run)
    # `accounts_listed` is diagnostics for the message above, not part of the contract. Response
    # models here are `extra="allow"` on purpose (an undeclared key must never be silently dropped),
    # which means an internal key would ship as a public field unless it is taken out by hand.
    return {**{k: v for k, v in result.items() if k != "accounts_listed"}, "message": message}


# -- Backups -----------------------------------------------------------------------------------


class BackupOut(PassthroughModel):
    name: str  # the filename, and the id `POST /backups/restore` takes
    size_bytes: int
    created_at: str


@_authed.get("/backups", response_model=list[BackupOut])
async def get_backups(request: Request) -> list[dict]:
    """List available DB backups, newest first."""
    from shortlist.server.services.backup import list_backups

    return list_backups(request.app.state.config_dir)


class BackupCreatedOut(PassthroughModel):
    """A manual backup carries no `created_at`: it was just taken, which is the answer."""

    name: str
    size_bytes: int


@_authed.post("/backups", response_model=BackupCreatedOut)
async def create_backup(request: Request) -> dict:
    """Take a manual backup now."""
    from shortlist.server.services.backup import take_backup

    path = await asyncio.get_running_loop().run_in_executor(
        None, lambda: take_backup(request.app.state.config_dir, label="manual")
    )
    if path is None:
        raise HTTPException(status_code=500, detail="backup failed")
    return {"name": path.name, "size_bytes": path.stat().st_size}


class RestoreRequest(BaseModel):
    name: str


class BackupRestoredOut(PassthroughModel):
    restored: str
    message: str
    # Named apart from `message` so the UI renders it as a warning rather than a receipt — a restore
    # also restores who could see which rows (see the handler).
    privacy_note: str


@_authed.post("/backups/restore", response_model=BackupRestoredOut)
async def restore_backup_endpoint(body: RestoreRequest, request: Request) -> dict:
    """Restore from a named backup. The app will need to be restarted after.

    A restore is not a neutral rollback: the database is what decides WHO MAY SEE WHAT. Restoring a
    copy taken before a shared row's audience was narrowed puts the wider audience back, and the
    shared-exclude prune — the only un-hiding path Shortlist has — then removes the `label!=` excludes
    that were hiding that row. That is correct for the config being restored, and it is exactly the
    kind of change an operator does not expect from a button labelled "restore".

    So it is stated, in the response and in the audit trail (rule 10), rather than left to be
    discovered on someone's Home screen.
    """
    from shortlist.server.services.backup import restore_backup

    state = request.app.state
    ok = await asyncio.get_running_loop().run_in_executor(None, lambda: restore_backup(state.config_dir, body.name))
    if not ok:
        raise HTTPException(status_code=404, detail="backup not found")
    with state.sessions() as session:
        session.add(
            Event(
                scope="backup.restore",
                level="warning",
                message={"backup": body.name, "at": datetime.now(UTC).isoformat()},
            )
        )
        session.commit()
    return {
        "restored": body.name,
        "message": "Restored. Restart the container to pick up the restored database.",
        # Named separately from `message` so the UI can render it as a warning rather than a receipt.
        "privacy_note": (
            "This also restores who could see which rows at the time of the backup. If you have "
            "narrowed a shared row's audience since then, those people can see it again after the "
            "next run — check Rows before restarting."
        ),
    }


# Strong references to in-flight background drains. asyncio holds only a weak reference to a task,
# so without this the garbage collector can cancel one mid-job.
_BACKGROUND_DRAINS: set[asyncio.Task] = set()


#: A job's whole lifecycle: queued -> running -> done | failed, plus the boot recovery that puts a
#: `running` row left by a dead process back to `queued`. `services/jobs.py` writes all four and
#: nothing else, so both job shapes below share this one declaration.
JobStatus = Literal["queued", "running", "done", "failed"]


class JobOut(PassthroughModel):
    """One background job row — the shape `_job_dict` builds, on both `/jobs` and `/jobs/catalog`."""

    id: int
    kind: str
    status: JobStatus
    attempts: int
    max_attempts: int
    detail: str
    error: str | None
    payload: dict  # data by design (a slug, a row), never a secret — see `_job_dict`
    result: dict
    created_at: str
    started_at: str | None
    finished_at: str | None


def _job_dict(job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "detail": job.detail,
        "error": job.error,
        # What it was asked to do and what came back — the two things an operator needs to judge a
        # failure without going to the container log. `payload` is data by design (a slug, a row),
        # never a secret: job kinds that touch tokens read them from the settings store at run time.
        "payload": job.payload or {},
        "result": job.result or {},
        "created_at": iso_utc(job.created_at),
        "started_at": iso_utc(job.started_at),
        "finished_at": iso_utc(job.finished_at),
    }


@_authed.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    request: Request,
    limit: int = Query(25, ge=1, le=200),
    kind: str | None = None,
    before_id: int | None = None,
    status: JobStatus | None = None,
) -> list[dict]:
    """Recent background jobs, newest first — the "did that actually happen?" answer.

    Maintenance work used to be fire-and-forget: it landed in the logs and the events table and
    nowhere an operator would look. Runs keep their own page; this is everything else.

    `kind` narrows it to one job type, which is how the Jobs page shows a single job's own history
    without pulling every other kind's rows down with it. `before_id` pages backwards — pass the id
    of the oldest job you already have.

    `status` narrows it to one outcome, and it exists because the Jobs page's "N failed" badge
    counts every failed row in the table while the feed behind it could only fetch the newest N.
    Measured on a real server: 8 failures, all `privacy.sync`, at ids 587-596 — the newest hundred
    jobs started at id 680, so filtering a fetched page client-side answered "8 failed" with an
    empty list. A count over the whole table needs a filter over the whole table.
    """
    from shortlist.server.db.models import Job

    with request.app.state.sessions() as session:
        query = session.query(Job)
        if kind:
            query = query.filter(Job.kind == kind)
        if status:
            query = query.filter(Job.status == status)
        if before_id is not None:
            query = query.filter(Job.id < before_id)
        rows = query.order_by(Job.created_at.desc(), Job.id.desc()).limit(min(limit, 200)).all()
        return [_job_dict(job) for job in rows]


class JobCatalogEntryOut(PassthroughModel):
    """One card on the Jobs page: what the kind does, when it next runs, and how it went last time."""

    kind: str
    label: str
    description: str
    manual: bool  # may the UI trigger it from a button?
    trigger: str  # what causes it, for the kinds no button can start
    scheduled: bool  # "can run on a timer", NOT "currently does" — `next_run` answers that
    schedule_optional: bool
    schedule_setting: str  # the settings key holding this kind's cron; "" when it has none
    next_run: str | None
    last: JobOut | None
    total: int
    queued: int
    running: int
    failed: int


@_authed.get("/jobs/catalog", response_model=list[JobCatalogEntryOut])
async def jobs_catalog(request: Request) -> list[dict]:
    """Every job Shortlist can run: what it does, when it next runs, and how it went last time.

    The Jobs page is organised BY JOB, not by chronology — "is the roster sync healthy?" was
    unanswerable from a flat list of the last 25 rows mixing every kind together. Each entry
    carries enough to render a card without a second request per kind.
    """
    from sqlalchemy import func

    from shortlist.server.db.models import Job
    from shortlist.server.services.jobs import CATALOG

    scheduler = getattr(request.app.state, "scheduler", None)

    def next_run(job_id: str | None) -> str | None:
        if not (scheduler and job_id):
            return None
        scheduled = scheduler.get_job(job_id)
        return iso_utc(scheduled.next_run_time) if scheduled and scheduled.next_run_time else None

    with request.app.state.sessions() as session:
        # One grouped scan for the counts, rather than a query per kind per status.
        tallies: dict[tuple[str, str], int] = {
            (kind, status): n
            for kind, status, n in session.query(Job.kind, Job.status, func.count(Job.id)).group_by(
                Job.kind, Job.status
            )
        }
        latest = {
            job.kind: job
            for job in session.query(Job).filter(Job.id.in_(session.query(func.max(Job.id)).group_by(Job.kind))).all()
        }
        out = []
        for entry in CATALOG:
            counts = {status: n for (kind, status), n in tallies.items() if kind == entry.kind}
            last = latest.get(entry.kind)
            out.append(
                {
                    "kind": entry.kind,
                    "label": entry.label,
                    "description": entry.description,
                    "manual": entry.manual,
                    "trigger": entry.trigger,
                    # "can run on a timer", NOT "currently does" — `next_run` answers that. A job with
                    # an optional schedule left blank has a job_id but no trigger.
                    "scheduled": bool(entry.schedule_job_id),
                    "schedule_optional": entry.schedule_optional,
                    # The settings key holding this job's cron, so the UI can offer an editor for any
                    # schedulable job rather than each being wired by hand — which is how privacy sync
                    # and the drift check ended up with no way to set a schedule at all.
                    "schedule_setting": entry.schedule_setting or "",
                    "next_run": next_run(entry.schedule_job_id),
                    "last": _job_dict(last) if last else None,
                    "total": sum(counts.values()),
                    "queued": counts.get("queued", 0),
                    "running": counts.get("running", 0),
                    "failed": counts.get("failed", 0),
                }
            )
        return out


class RunJobRequest(BaseModel):
    kind: str
    payload: dict = {}
    # Return as soon as the job is queued instead of waiting out the drain. The Jobs page sets this
    # and polls: `sync.history` on a large server takes minutes, and holding an HTTP request open
    # that long only ever ends in a proxy timeout — which reads to the operator as a failed job when
    # the job is in fact still running fine. The default stays False so the Tools page's Sync Check
    # card keeps getting its `fixed`/`orphans` preview inline.
    background: bool = False


class JobRunOut(PassthroughModel):
    """The receipt for `POST /jobs` — a subset of `JobOut` plus the sync-check preview lists."""

    id: int
    kind: str
    status: JobStatus
    detail: str
    error: str | None
    fixed: list[str]  # labels the check corrected (or, on a dry run, would correct)
    orphans: list[str]  # labels it would DELETE — kept apart from `fixed`, it cannot be undone


@_authed.post("/jobs", response_model=JobRunOut)
async def run_job(body: RunJobRequest, request: Request) -> dict:
    """Queue a maintenance job and drain immediately, so pressing a button still feels instant.

    The queue is the SAFETY NET, not a delay: if this attempt fails the job stays queued and the
    worker retries it with backoff, which is exactly what fire-and-forget could never do.
    """
    from shortlist.server.services.jobs import KINDS, enqueue, run_pending

    if body.kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"unknown job kind; valid: {sorted(KINDS)}")
    state = request.app.state
    job_id = enqueue(state.sessions, body.kind, body.payload)
    if body.background:
        # Fire and forget on THIS loop. Losing the task to a restart is not a lost job — the row is
        # committed, and the drain tick picks it up regardless.
        task = asyncio.create_task(run_pending(state))
        _BACKGROUND_DRAINS.add(task)  # a bare create_task can be garbage-collected mid-flight
        task.add_done_callback(_BACKGROUND_DRAINS.discard)
    else:
        await run_pending(state)
    from shortlist.server.db.models import Job

    with state.sessions() as session:
        job = session.get(Job, job_id)
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "detail": job.detail,
            "error": job.error,
            # What it actually changed (or, on a dry run, would change) — the preview an operator
            # reads before authorising the live pass. `orphans` is kept apart from `fixed` because
            # deleting a collection is the one thing here that cannot be undone.
            "fixed": (job.result or {}).get("fixed", []),
            "orphans": (job.result or {}).get("orphans", []),
        }


# The exported router, assembled LAST on purpose: see the module docstring. `_public` carries only
# `/health`; `_authed` carries its `require_owner` dependency with it through include_router, so
# every route below /api/system except the health check is owner-gated by construction.
router = APIRouter(prefix="/system", tags=["system"])
router.include_router(_public)
router.include_router(_authed)
