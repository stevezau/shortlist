"""Prevent deleted row IDs from becoming another row's assistant authority.

SQLite's ordinary INTEGER PRIMARY KEY can reuse the last deleted ID. Grants and
durable records retain those IDs, so collections require AUTOINCREMENT. Seed its
sequence above retained references as well as live rows: a deleted ID must not be
reissued immediately after upgrading an existing installation.
"""

import json
from collections.abc import Iterator

import sqlalchemy as sa
from alembic import op
from loguru import logger
from sqlalchemy.engine import Connection

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None

_JSON_COLUMNS = {
    "assistant_grants": ("constraints",),
    "assistant_changes": ("intent", "requirements", "summary", "effects"),
    "assistant_operations": ("result",),
    "runs": ("stats",),
    "jobs": ("payload", "result"),
    "events": ("message",),
}
_SINGULAR_KEYS = {"row_id", "collection_id"}
_PLURAL_KEYS = {"row_ids", "collection_ids", "affected_row_ids", "selected_row_ids"}


def _positive_id(value: object) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is int and 0 < value <= 9223372036854775807:
        return value
    return 0


def _canonical_ids(value: object) -> Iterator[int]:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if key in _SINGULAR_KEYS:
                    yield _positive_id(child)
                elif key in _PLURAL_KEYS and isinstance(child, list):
                    yield from (_positive_id(identifier) for identifier in child)
                if isinstance(child, (dict, list)):
                    pending.append(child)
        elif isinstance(item, list):
            pending.extend(child for child in item if isinstance(child, (dict, list)))


def _snapshot_ids(diff: object) -> Iterator[int]:
    if not isinstance(diff, dict):
        return
    for key in ("created", "deleted"):
        snapshot = diff.get(key)
        if isinstance(snapshot, dict):
            yield _positive_id(snapshot.get("id"))
    row = diff.get("row")
    if isinstance(row, dict):
        for key in ("created", "deleted"):
            snapshot = row.get(key)
            if isinstance(snapshot, dict):
                yield _positive_id(snapshot.get("id"))


#: History the app only ever writes and reads back as JSON. A value it cannot parse (a hand edit) cannot point
#: anything at a row either, so it is skipped rather than blocking startup. Assistant records are authority:
#: one that cannot be read still stops the upgrade.
_SKIPPABLE = {"runs", "jobs", "events"}


def _json(value: object, table: str, column: str) -> object:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        if table in _SKIPPABLE:
            logger.warning("row identity migration: skipped a value in {}.{} that is not valid JSON", table, column)
            return None
        raise RuntimeError(f"Cannot reserve historical row IDs: invalid JSON in {table}.{column}") from exc


def _historical_floor(connection: Connection, tables: set[str]) -> int:
    floor = 0
    for table, column in (
        ("collections", "id"),
        ("collection_audience", "collection_id"),
        ("collection_user_overrides", "collection_id"),
        ("theme_history", "collection_id"),
    ):
        if table in tables:
            statement = sa.select(sa.func.max(sa.column(column))).select_from(sa.table(table))
            floor = max(floor, _positive_id(connection.scalar(statement)))

    has_sequence = connection.scalar(sa.text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"))
    if has_sequence:
        sequence = connection.scalar(sa.text("SELECT seq FROM sqlite_sequence WHERE name = 'collections'"))
        floor = max(floor, _positive_id(sequence))

    for table, columns in _JSON_COLUMNS.items():
        if table not in tables:
            continue
        context_column = "kind" if table == "assistant_changes" else "scope" if table == "events" else None
        selected = (*columns, context_column) if context_column else columns
        statement = sa.select(*(sa.column(column) for column in selected)).select_from(sa.table(table))
        for record in connection.execute(statement).mappings():
            for column in columns:
                value = _json(record[column], table, column)
                floor = max(floor, max(_canonical_ids(value), default=0))
                if not isinstance(value, dict):
                    continue
                if table == "assistant_changes" and column == "summary" and record["kind"] in {"row", "setup"}:
                    floor = max(floor, max(_snapshot_ids(value.get("configuration_diff")), default=0))
                elif table == "events" and record["scope"] == "assistant.applied":
                    floor = max(floor, max(_snapshot_ids(value.get("diff")), default=0))

    if "poster_assets" in tables:
        statement = sa.select(sa.column("key")).select_from(sa.table("poster_assets"))
        for key in connection.scalars(statement):
            if isinstance(key, str) and key.startswith("upload:"):
                floor = max(floor, _positive_id(key.removeprefix("upload:")))
    return floor


def upgrade() -> None:
    connection = op.get_bind()
    # Rebuilding a referenced table with foreign keys enabled can cascade-delete
    # its children. The normal Alembic runner uses a separate connection with FK
    # enforcement off; reject other callers rather than silently losing records.
    if connection.scalar(sa.text("PRAGMA foreign_keys")):
        raise RuntimeError("Row identity migration requires the dedicated Alembic connection (foreign_keys=OFF)")
    # Python's legacy sqlite3 transaction mode does not BEGIN for DDL. Include
    # creation/copy/rename and the sequence seed in the same rollback boundary.
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    tables = set(sa.inspect(connection).get_table_names())
    floor = _historical_floor(connection, tables)
    with op.batch_alter_table("collections", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass
    result = connection.execute(
        sa.text("UPDATE sqlite_sequence SET seq = :floor WHERE name = 'collections'"), {"floor": floor}
    )
    if result.rowcount == 0:
        connection.execute(
            sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('collections', :floor)"), {"floor": floor}
        )
    if connection.execute(sa.text("PRAGMA foreign_key_check")).first() is not None:
        raise RuntimeError("Row identity migration found inconsistent foreign keys")


def downgrade() -> None:
    # Older application builds can read this schema. Keep the non-reuse property
    # on downgrade so retained permissions cannot become authority over new rows.
    pass
