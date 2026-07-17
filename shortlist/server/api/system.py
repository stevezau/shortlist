"""System API: health (unauthenticated, for Docker HEALTHCHECK), version, full uninstall."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

import shortlist
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Collection, Event, RestrictionSnapshotRow, User
from shortlist.server.scheduler import rebuild_schedule

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": shortlist.__version__}


@router.get("/version", dependencies=[Depends(require_owner)])
async def version() -> dict:
    return {"version": shortlist.__version__}


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
            emit(f"Restoring {total} user share filter{'' if total == 1 else 's'} (Plex allows ~1/sec)…")
            for row in snapshots:
                user = users.get(row.user_id)
                if user is None:
                    continue
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
                    emit(f"Restored {user.username}'s share filter", done=restored, total=total)
        deleted = []
        for section in ctx.plex.sections():
            for collection in section.collections():
                if any(label.tag.lower().startswith("shortlist_") for label in collection.labels):
                    deleted.append(collection.title)
                    if not body.dry_run:
                        ctx.plex.delete_owned_collection(collection, "shortlist")
                        emit(f"Deleted collection “{collection.title}”")
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
