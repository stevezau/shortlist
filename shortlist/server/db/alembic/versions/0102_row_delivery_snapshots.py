"""Keep confirmed row membership independently of disposable run logs.

Revision ID: 0102
Revises: 0101

Only per-library delivery evidence can establish membership. Detached picks have
lost that evidence; migration must not reconstruct it from current Plex state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            # Malformed audience data must remain invalid, never become the
            # NULL that represents a public row.
            return value
    return value


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _date(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def _picks(value: Any) -> list[dict] | None:
    if not isinstance(value, list):
        return None
    picks = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            return None
        tmdb_id, rating_key = _positive_int(item.get("tmdb_id")), _positive_int(item.get("rating_key"))
        media_type = item.get("media_type")
        if tmdb_id is None or rating_key is None or media_type not in ("movie", "show"):
            return None
        key = (tmdb_id, media_type, rating_key)
        if key in seen:
            return None
        seen.add(key)
        picks.append(
            {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "rating_key": rating_key,
                "title": str(item.get("title") or ""),
            }
        )
    return picks


def _entries(value: Any) -> list[dict]:
    breakdown = _json(value)
    if not isinstance(breakdown, list):
        return []
    entries = []
    for entry in breakdown:
        if not isinstance(entry, dict) or entry.get("error") or entry.get("status") not in (None, "ok"):
            continue
        slug = entry.get("row_slug")
        library = entry.get("library_key")
        rating_key = _positive_int(entry.get("rating_key"))
        picks = _picks(entry.get("picks"))
        if not isinstance(slug, str) or not slug or not library or rating_key is None:
            continue
        entries.append({"collection_slug": slug, "library_key": str(library), "rating_key": rating_key, "picks": picks})
    return entries


def _account_ids(value: Any) -> list[int] | None:
    value = _json(value)
    if not isinstance(value, list):
        return None
    ids = [_positive_int(item) for item in value]
    return sorted(set(ids)) if all(item is not None for item in ids) else None


def _insert(connection: Connection, table: sa.TableClause, report: Any, entry: dict, **values: Any) -> None:
    # Length stays bounded even when both slugs are at their column limits. The
    # run's start disambiguates IDs SQLite may reuse after history was cleared.
    identity = [
        report["run_id"],
        report["started_at"],
        values["user_slug"],
        entry["collection_slug"],
        entry["library_key"],
    ]
    source_key = "legacy:" + hashlib.sha256(json.dumps(identity, default=str).encode()).hexdigest()
    connection.execute(table.insert().values(source_key=source_key, ended_at=None, **entry, **values))


def _backfill_personal(connection: Connection, table: sa.TableClause, unclocked: set[tuple[str, str, str]]) -> None:
    reports = connection.execute(
        sa.text(
            "SELECT ru.run_id, ru.user_id, ru.breakdown, r.started_at, r.began_at, u.slug AS user_slug "
            "FROM run_users ru JOIN runs r ON r.id = ru.run_id JOIN users u ON u.id = ru.user_id "
            "WHERE r.dry_run = 0 ORDER BY ru.run_id, ru.user_id"
        )
    ).mappings()
    for report in reports:
        seen = set()
        for entry in _entries(report["breakdown"]):
            identity = (entry["collection_slug"], entry["library_key"])
            if identity in seen:
                continue
            seen.add(identity)
            # Read at most this delivery's picks, never materialize the impact
            # ledger for every user/run in memory during startup.
            rows = connection.execute(
                sa.text(
                    "SELECT id, tmdb_id, media_type, rating_key, created_at FROM picks "
                    "WHERE run_id = :run_id AND user_id = :user_id "
                    "AND collection_slug = :slug AND section_key = :library"
                ),
                {"run_id": report["run_id"], "user_id": report["user_id"], "slug": identity[0], "library": identity[1]},
            ).mappings()
            by_title: dict[tuple, list] = {}
            group_dates = []
            for pick in rows:
                by_title.setdefault((pick["tmdb_id"], pick["media_type"], pick["rating_key"]), []).append(pick)
                if created := _date(pick["created_at"]):
                    group_dates.append(created)
            matched = []
            for item in entry["picks"] or []:
                candidates = by_title.get((item["tmdb_id"], item["media_type"], item["rating_key"]), [])
                if len(candidates) != 1 or (created := _date(candidates[0]["created_at"])) is None:
                    matched = []
                    break
                matched.append({**item, "pick_id": candidates[0]["id"]})
            # A confirmed write with unknown contents still ends its predecessor.
            # started_at is enqueue time and cannot order overlapping queued runs.
            delivered_at = min(group_dates) if group_dates else _date(report["began_at"])
            if delivered_at is None:
                unclocked.add((report["user_slug"], *identity))
                delivered_at = _date(report["started_at"])
            if delivered_at is None:
                continue
            entry["picks"] = matched
            _insert(
                connection,
                table,
                report,
                entry,
                user_slug=report["user_slug"],
                user_id=report["user_id"],
                shared=False,
                delivered_at=delivered_at,
                audience=None,
                muted=[],
            )


def _backfill_shared(connection: Connection, table: sa.TableClause, unclocked: set[tuple[str, str, str]]) -> None:
    reports = connection.execute(
        sa.text(
            "SELECT sr.run_id, sr.collection_slug, sr.breakdown, sr.picks, "
            "sr.audience, sr.muted, sr.delivered_at, r.started_at, r.began_at "
            "FROM run_shared_rows sr JOIN runs r ON r.id = sr.run_id "
            "WHERE r.dry_run = 0 ORDER BY sr.run_id, sr.collection_slug"
        )
    ).mappings()
    for report in reports:
        delivery_clock = _date(report["delivered_at"]) or _date(report["began_at"])
        delivered_at = delivery_clock or _date(report["started_at"])
        if delivered_at is None:
            continue
        raw_audience, raw_muted = _json(report["audience"]), _json(report["muted"])
        audience = _account_ids(raw_audience) if raw_audience is not None else None
        muted = _account_ids(raw_muted) if raw_muted is not None else []
        audience_valid = not (raw_audience is not None and audience is None)
        flat_picks = _picks(_json(report["picks"]))
        flat_keys = {(p["tmdb_id"], p["media_type"], p["rating_key"]) for p in flat_picks or []}
        seen = set()
        for entry in _entries(report["breakdown"]):
            if entry["collection_slug"] != report["collection_slug"] or entry["library_key"] in seen:
                continue
            seen.add(entry["library_key"])
            if delivery_clock is None:
                unclocked.add((f"shared_{report['collection_slug']}", report["collection_slug"], entry["library_key"]))
            # Only FLAT shared picks acquired media_type after audience snapshots
            # existed. Breakdown picks were typed earlier and cannot prove that
            # NULL audience means public. A stored delivery clock is also newer
            # than the audience schema; a valid explicit audience needs no proxy.
            keys = {(p["tmdb_id"], p["media_type"], p["rating_key"]) for p in entry["picks"] or []}
            provenance = (
                audience is not None
                or _date(report["delivered_at"]) is not None
                or (flat_picks is not None and bool(keys) and keys <= flat_keys)
            )
            if not audience_valid or muted is None or not provenance or entry["picks"] is None:
                entry["picks"] = []
            _insert(
                connection,
                table,
                report,
                entry,
                user_slug=f"shared_{report['collection_slug']}",
                user_id=None,
                shared=True,
                delivered_at=delivered_at,
                audience=audience if entry["picks"] else [],
                muted=muted or [],
            )


def upgrade() -> None:
    connection = op.get_bind()
    if sa.inspect(connection).has_table("row_delivery_snapshots"):
        return
    op.create_table(
        "row_delivery_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_key", sa.String(255), nullable=False, unique=True),
        sa.Column("collection_slug", sa.String(255), nullable=False),
        sa.Column("user_slug", sa.String(255), nullable=False),
        sa.Column("library_key", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("shared", sa.Boolean(), nullable=False),
        sa.Column("rating_key", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("picks", sa.JSON(), nullable=False),
        sa.Column("audience", sa.JSON(), nullable=True),
        sa.Column("muted", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_row_delivery_identity_time",
        "row_delivery_snapshots",
        ["user_slug", "collection_slug", "library_key", "delivered_at"],
    )
    table = sa.table(
        "row_delivery_snapshots",
        sa.column("source_key", sa.String()),
        sa.column("collection_slug", sa.String()),
        sa.column("user_slug", sa.String()),
        sa.column("library_key", sa.String()),
        sa.column("user_id", sa.Integer()),
        sa.column("shared", sa.Boolean()),
        sa.column("rating_key", sa.Integer()),
        sa.column("delivered_at", sa.DateTime()),
        sa.column("ended_at", sa.DateTime()),
        sa.column("picks", sa.JSON()),
        sa.column("audience", sa.JSON()),
        sa.column("muted", sa.JSON()),
    )
    unclocked: set[tuple[str, str, str]] = set()
    _backfill_personal(connection, table, unclocked)
    _backfill_shared(connection, table, unclocked)
    # No reliable boundary means no reliable ordering against other deliveries.
    # Fail closed for that row/library until a new actual delivery establishes it.
    for owner, slug, library in unclocked:
        connection.execute(
            table.update()
            .where(
                table.c.user_slug == owner,
                table.c.collection_slug == slug,
                table.c.library_key == library,
            )
            .values(picks=[])
        )
    # Ordering by the delivery clock, not the run ID, also handles slow earlier
    # runs and recycled IDs. Equal-time successors make the prior interval empty.
    connection.execute(
        sa.text(
            "UPDATE row_delivery_snapshots AS current SET ended_at = ("
            "SELECT next.delivered_at FROM row_delivery_snapshots AS next "
            "WHERE next.user_slug = current.user_slug "
            "AND next.collection_slug = current.collection_slug "
            "AND next.library_key = current.library_key "
            "AND (next.delivered_at > current.delivered_at "
            "OR (next.delivered_at = current.delivered_at AND next.id > current.id)) "
            "ORDER BY next.delivered_at, next.id LIMIT 1)"
        )
    )


def downgrade() -> None:
    op.drop_table("row_delivery_snapshots")
