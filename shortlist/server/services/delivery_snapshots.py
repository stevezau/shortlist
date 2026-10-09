"""The compact delivered-membership ledger; never depends on retained run logs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import overload

from sqlalchemy.orm import Session

from shortlist.server.db.models import Collection, Delivery, PickRow, RowDeliverySnapshot, Run, User


@overload
def utc(value: datetime) -> datetime: ...


@overload
def utc(value: None) -> None: ...


def utc(value: datetime | None) -> datetime | None:
    """Restore UTC on SQLite timestamps, which come back naive; None passes through."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def delivery_time(entry: dict, fallback: datetime) -> datetime:
    """Read the timestamp captured after a successful library delivery."""
    raw = entry.get("delivered_at")
    try:
        return utc(datetime.fromisoformat(raw)) if isinstance(raw, str) else utc(fallback)
    except ValueError:
        return utc(fallback)


def close_snapshots(
    session: Session,
    *,
    user_slug: str | None = None,
    collection_slug: str | None = None,
    user_slugs: set[str] | None = None,
    libraries: set[str] | None = None,
    at: datetime | None = None,
) -> None:
    """Close exactly the collection intervals whose actual removal was confirmed."""
    query = session.query(RowDeliverySnapshot).filter(RowDeliverySnapshot.ended_at.is_(None))
    if user_slug is not None:
        query = query.filter_by(user_slug=user_slug)
    if collection_slug is not None:
        query = query.filter_by(collection_slug=collection_slug)
    if user_slugs is not None:
        query = query.filter(RowDeliverySnapshot.user_slug.in_(user_slugs))
    if libraries is not None:
        query = query.filter(RowDeliverySnapshot.library_key.in_(libraries))
    when = utc(at or datetime.now(UTC))
    for row in query:
        row.ended_at = max(utc(row.delivered_at), when)


def record_delivery_boundaries(session: Session, user: User, boundaries: list[dict]) -> None:
    """Close only this person's older intervals; a boundary alone proves no current membership."""
    for entry in boundaries:
        slug, library = entry.get("row_slug"), entry.get("library_key")
        if not slug or not library or not isinstance(entry.get("rating_key"), int) or entry["rating_key"] <= 0:
            continue
        try:
            when = utc(datetime.fromisoformat(entry["delivered_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        older = session.query(RowDeliverySnapshot).filter(
            RowDeliverySnapshot.user_id == user.id,
            RowDeliverySnapshot.user_slug == user.slug,
            RowDeliverySnapshot.shared.is_(False),
            RowDeliverySnapshot.collection_slug == slug,
            RowDeliverySnapshot.library_key == str(library),
            RowDeliverySnapshot.delivered_at < when,
        )
        for row in older:
            if row.ended_at is None or utc(row.ended_at) > when:
                row.ended_at = when


def record_snapshots(
    session: Session,
    run_id: int,
    user_slug: str,
    breakdown: list[dict],
    *,
    user_id: int | None,
) -> None:
    """Record only confirmed per-library results, with exact retained personal pick references."""
    run = session.get(Run, run_id)
    if run is None or run.dry_run:
        return
    session.flush()
    personal = session.query(PickRow).filter_by(run_id=run_id, user_id=user_id).all() if user_id else []
    by_identity = {(p.collection_slug, p.section_key, p.tmdb_id, p.media_type): p for p in personal}
    for entry in breakdown or []:
        slug, library = entry.get("row_slug") or "", str(entry.get("library_key") or "")
        rating_key = int(entry.get("rating_key") or 0)
        if not slug or not library or not rating_key or not isinstance(entry.get("picks"), list):
            continue
        shared = user_id is None
        # An unknown legacy audience must not become a public shared row.
        if shared and "audience" not in entry:
            continue
        source_key = entry.get("delivery_id") or (
            f"{run.id}:{utc(run.started_at).isoformat()}:{user_slug}:{slug}:{library}"
        )
        if session.query(RowDeliverySnapshot.id).filter_by(source_key=source_key).first():
            continue
        when = delivery_time(entry, datetime.now(UTC))
        picks = []
        for item in entry["picks"]:
            if not isinstance(item, dict) or not item.get("tmdb_id") or item.get("media_type") not in ("movie", "show"):
                continue
            pick = by_identity.get((slug, library, item["tmdb_id"], item["media_type"]))
            if not shared and pick is None:
                # A later library may fail before the engine finalises its flat pick list. The
                # successful breakdown is still proof of this library's delivered recommendation.
                pick = PickRow(
                    run_id=run_id,
                    user_id=user_id,
                    collection_slug=slug,
                    section_key=library,
                    library=entry.get("library_title") or library,
                    tmdb_id=item["tmdb_id"],
                    media_type=item["media_type"],
                    rating_key=int(item.get("rating_key") or 0),
                    rank=int(item.get("rank") or 1),
                    title=item.get("title") or "",
                    reason=item.get("reason") or "",
                    seed_title=item.get("seed_title") or "",
                    sources=",".join(item.get("sources") or []),
                    created_at=when,
                )
                session.add(pick)
                session.flush()
                by_identity[(slug, library, item["tmdb_id"], item["media_type"])] = pick
            if pick is not None:
                pick.created_at = when
            picks.append(
                {
                    "tmdb_id": item["tmdb_id"],
                    "media_type": item["media_type"],
                    "rating_key": int(item.get("rating_key") or 0),
                    "title": item.get("title") or "",
                    "pick_id": pick.id if pick is not None else None,
                }
            )
        # A real empty result closes the previous membership; a missing result never does.
        close_snapshots(session, user_slug=user_slug, collection_slug=slug, libraries={library}, at=when)
        session.add(
            RowDeliverySnapshot(
                source_key=source_key,
                collection_slug=slug,
                user_slug=user_slug,
                library_key=library,
                user_id=user_id,
                shared=shared,
                rating_key=rating_key,
                delivered_at=when,
                picks=picks,
                audience=entry.get("audience"),
                muted=list(entry.get("muted") or []),
            )
        )
        session.flush()


def current_snapshots(session: Session, *, user_id: int | None = None) -> list[RowDeliverySnapshot]:
    """Confirmed current rows whose recorded Plex collection and owner still exist."""
    query = session.query(RowDeliverySnapshot).filter(RowDeliverySnapshot.ended_at.is_(None))
    if user_id is not None:
        query = query.filter(RowDeliverySnapshot.user_id == user_id)
    enabled = {s for (s,) in session.query(Collection.slug).filter(Collection.enabled.is_(True))}
    ledger = {(d.user_slug, d.collection_slug, d.library_key, d.rating_key) for d in session.query(Delivery)}
    users = {u.id: u for u in session.query(User).filter(User.removed_at.is_(None))}
    current = []
    for row in query:
        owner = users.get(row.user_id)
        if row.collection_slug not in enabled:
            continue
        if (row.user_slug, row.collection_slug, row.library_key, row.rating_key) not in ledger:
            continue
        if row.shared:
            if row.user_slug != f"shared_{row.collection_slug}":
                continue
        elif owner is None or owner.slug != row.user_slug:
            continue
        current.append(row)
    return current


def current_pick_ids(session: Session, *, user_id: int | None = None) -> dict[int, set[int]]:
    """Personal picks in currently delivered snapshots, validated against their exact identity."""
    wanted: dict[int, tuple[int, str, str, int, str]] = {}
    for row in current_snapshots(session, user_id=user_id):
        if row.shared:
            continue
        for item in row.picks:
            if item.get("pick_id"):
                wanted[item["pick_id"]] = (
                    row.user_id,
                    row.collection_slug,
                    row.library_key,
                    item["tmdb_id"],
                    item["media_type"],
                )
    out: dict[int, set[int]] = {}
    if wanted:
        for pick in session.query(PickRow).filter(PickRow.id.in_(wanted)):
            if wanted[pick.id] == (pick.user_id, pick.collection_slug, pick.section_key, pick.tmdb_id, pick.media_type):
                out.setdefault(pick.user_id, set()).add(pick.id)
    return out
