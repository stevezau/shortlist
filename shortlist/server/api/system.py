"""System API: health (unauthenticated, for Docker HEALTHCHECK), version, full uninstall."""

from __future__ import annotations

import asyncio
import os
import platform
import secrets as pysecrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger
from pydantic import BaseModel

import shortlist
from shortlist.logging_config import normalize_level
from shortlist.server.auth import API_TOKEN_KEY, API_TOKEN_PREFIX, require_owner
from shortlist.server.db.models import Collection, Event, RestrictionSnapshotRow, User, iso_utc
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.scheduler import rebuild_schedule
from shortlist.server.services import log_reader
from shortlist.server.settings_store import SettingsStore

_TOKEN_CREATED_KEY = "api.token_created_at"

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": shortlist.__version__}


@router.get("/version", dependencies=[Depends(require_owner)])
async def version() -> dict:
    return {"version": shortlist.__version__}


@router.get("/api-token", dependencies=[Depends(require_owner)])
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


@router.post("/api-token", dependencies=[Depends(require_owner)])
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


@router.delete("/api-token", dependencies=[Depends(require_owner)])
async def revoke_api_token(request: Request) -> dict:
    """Revoke the API token — any script still using it starts getting 401s on the next call."""
    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        store.set(API_TOKEN_KEY, "")
        store.set(_TOKEN_CREATED_KEY, "")
        session.add(Event(scope="api_token.revoke", level="warn", message={"at": datetime.now(UTC).isoformat()}))
        session.commit()
    logger.info("owner API token revoked")
    return {"enabled": False}


@router.get("/image-provider", dependencies=[Depends(require_owner)])
async def image_provider(request: Request) -> dict:
    """Whether the configured AI provider can generate poster images (and a plain-English reason if
    not) — so the row editor can enable/disable the "Generate" poster option honestly."""
    from shortlist.server.services.poster_service import image_provider_status
    from shortlist.server.settings_store import SettingsStore

    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        return image_provider_status(store)


@router.get("/logs", dependencies=[Depends(require_owner)])
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


@router.get("/logs/download", dependencies=[Depends(require_owner)])
async def logs_download(request: Request) -> Response:
    """Every log file as a redacted zip — the attachment for a bug report."""
    payload = await asyncio.get_running_loop().run_in_executor(
        None, lambda: log_reader.build_zip(request.app.state.config_dir)
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="shortlist-logs-{stamp}.zip"'},
    )


@router.get("/debug", dependencies=[Depends(require_owner)], response_class=PlainTextResponse)
async def debug_bundle(request: Request) -> str:
    """A pasteable diagnostics bundle for bug reports: version, DB migration head, scheduler jobs,
    connection status, and record counts. Deliberately plain text and secrets-free — every connection
    is reported as a yes/no, never a token or key (plex-safety rule 9)."""
    from sqlalchemy import func, text

    from shortlist.server.db.models import PickRow, RequestCandidate, Run
    from shortlist.server.settings_store import SettingsStore

    lines: list[str] = ["=== Shortlist debug bundle ===", f"version: {shortlist.__version__}"]
    lines.append(f"python: {platform.python_version()} on {platform.system()} {platform.machine()}")
    lines.append(f"time: {datetime.now(UTC).isoformat()}  TZ={os.environ.get('TZ', '(unset)')}")

    with request.app.state.sessions() as session:
        store = SettingsStore(session)
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


@router.get("/libraries", dependencies=[Depends(require_owner)])
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
        return [{"key": str(s.key), "title": s.title, "type": s.type} for s in PlexClient(url, token).sections()]

    return await asyncio.get_running_loop().run_in_executor(None, read)


@router.get("/libraries/{key}/collections", dependencies=[Depends(require_owner)])
async def library_collections(key: str, request: Request) -> list[dict]:
    """A library's managed (orderable) collections — the candidate ANCHORS for placing Shortlist rows
    in the Recommended shelf. Shortlist's own rows are excluded (you don't anchor a row to itself)."""
    from shortlist.engine.clients.plex_pms import PlexClient
    from shortlist.server.settings_store import SettingsStore

    state = request.app.state

    def read() -> list[dict]:
        with state.sessions() as session:
            store = SettingsStore(session, state.secrets)
            url, token = store.get("plex.url"), store.get("plex.token")
        if not url or not token:
            raise HTTPException(status_code=409, detail="Plex isn't connected yet")
        section = next((s for s in PlexClient(url, token).sections() if str(s.key) == key), None)
        if section is None:
            raise HTTPException(status_code=404, detail="library not found")
        ours = {
            c.title
            for c in section.collections()
            if any(lbl.tag.lower().startswith("shortlist_") for lbl in (c.labels or []))
        }
        titles: list[str] = []
        for hub in section.managedHubs():
            title = getattr(hub, "title", "") or ""
            if title and title not in ours and title not in titles:
                titles.append(title)
        return [{"title": t} for t in titles]

    return await asyncio.get_running_loop().run_in_executor(None, read)


@router.get("/owned-collections", dependencies=[Depends(require_owner)])
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


@router.post("/uninstall", dependencies=[Depends(require_owner)])
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
    loop = asyncio.get_running_loop()

    def emit(label: str, **extra: object) -> None:
        # Stream one live step to the SSE bus from the executor thread, so the Uninstall page shows
        # exactly what's happening (like the run activity log). Real uninstall only — the dry-run
        # preview is instant and needs no stream.
        if not body.dry_run:
            loop.call_soon_threadsafe(state.bus.publish, "uninstall.progress", {"label": label, **extra})

    def do_uninstall() -> tuple[dict, list[dict]]:
        from shortlist.engine.models import FilterSnapshot
        from shortlist.engine.privacy import restore_user_restrictions

        service = state.run_service
        ctx = service.build_context(dry_run=body.dry_run)
        per_user_events: list[dict] = []
        restored = 0
        with state.sessions() as session:
            users = {u.id: u for u in session.query(User).all()}
            snapshots = session.query(RestrictionSnapshotRow).filter_by(reason="initial").all()
            total = len(snapshots)
            emit(
                f"Restoring share filters for {total} user{'' if total == 1 else 's'} via plex.tv "
                f"(as fast as plex.tv accepts; backs off only if rate-limited)…"
            )
            i = 0
            for row in snapshots:
                user = users.get(row.user_id)
                if user is None:
                    continue
                i += 1
                emit(f"[{i}/{total}] Restoring {user.username}'s share filter on plex.tv…")
                snapshot = FilterSnapshot(
                    plex_account_id=user.plex_account_id,
                    username=user.username,
                    taken_at=row.taken_at,
                    filters=row.filters_before,
                )
                if restore_user_restrictions(ctx.plextv, snapshot, dry_run=body.dry_run):
                    restored += 1
                    per_user_events.append(
                        {"user": user.username, "restored_to": row.filters_before, "dry_run": body.dry_run}
                    )
                    emit(f"    ✓ {user.username} restored", done=restored, total=total)
        emit("Reading your Plex libraries to find Shortlist collections…")
        deleted = []
        for section in ctx.plex.sections():
            for collection in section.collections():
                if any(label.tag.lower().startswith("shortlist_") for label in collection.labels):
                    deleted.append(collection.title)
                    if not body.dry_run:
                        emit(f"Deleting collection “{collection.title}” from Plex…")
                        ctx.plex.delete_owned_collection(collection, "shortlist")
        # Disable every row too — otherwise the next scheduled run would rebuild the collections we
        # just removed and re-apply the restrictions we just undid, silently "reinstalling" Shortlist.
        with state.sessions() as session:
            enabled_rows = session.query(Collection).filter_by(enabled=True).all()
            rows_disabled = len(enabled_rows)
            if not body.dry_run:
                for row in enabled_rows:
                    row.enabled = False
                session.commit()
                emit(f"Switched off {rows_disabled} row{'' if rows_disabled == 1 else 's'} and cleared their schedules")
        return {
            "filters_restored": restored,
            "collections_deleted": deleted,
            "rows_disabled": rows_disabled,
            "dry_run": body.dry_run,
        }, per_user_events

    result, per_user = await asyncio.get_running_loop().run_in_executor(None, do_uninstall)
    if not body.dry_run:
        # Rows are now all disabled, so this clears every per-row cron job — no run fires again until
        # Shortlist is set up afresh.
        rebuild_schedule(request.app)
    with state.sessions() as session:
        for entry in per_user:
            session.add(Event(scope="uninstall.user", level="warn", message=entry))
        session.add(
            Event(scope="system.uninstall", level="warn", message={**result, "at": datetime.now(UTC).isoformat()})
        )
        session.commit()
    logger.warning("UNINSTALL {}: {}", "preview" if body.dry_run else "executed", result)
    message = "Preview only — nothing was changed." if body.dry_run else "Your server is as we found it."
    return {**result, "message": message}
