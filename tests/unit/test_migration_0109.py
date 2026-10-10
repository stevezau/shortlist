"""Row identity survives deletion and migration without inheriting old authority."""

from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.orm import Session

from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation
from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.db.models import (
    Collection,
    CollectionAudience,
    Event,
    Job,
    PosterAsset,
    Run,
    Theme,
    ThemeHistory,
    User,
)
from shortlist.server.db.session import make_engine
from tests.db_helpers import disposing_engine
from tests.unit.test_migrations import _alembic

pytestmark = pytest.mark.real_migrations


def _legacy(tmp_path):
    command.upgrade(_alembic(tmp_path), "0108")
    return make_engine(tmp_path)


def _insert_row(engine, name):
    with Session(engine) as session:
        row = Collection(slug=name, name=name, enabled=False)
        session.add(row)
        session.commit()
        return row.id


@pytest.mark.parametrize(
    "source",
    [
        "grant",
        "change_intent",
        "change_requirements",
        "change_summary",
        "change_effects",
        "operation",
        "run_contract",
        "run_actor",
        "job_payload",
        "job_result",
        "event_collection",
        "event_created",
        "event_setup_created",
        "poster_upload",
    ],
)
def test_0109_sequence_exceeds_each_retained_historical_row_reference(tmp_path, source):
    with disposing_engine(_legacy(tmp_path)) as engine:
        retired_id = 809
        now = datetime.now(UTC)
        with Session(engine) as session:
            if source == "grant":
                item = AssistantGrant(
                    id="historical-grant",
                    owner_account_id=42,
                    client_id="client",
                    name="Old row only",
                    preset="manage_selected_rows",
                    constraints={"row_ids": [retired_id]},
                )
            elif source.startswith("change_"):
                values = {"intent": {}, "requirements": {}, "summary": {}, "effects": []}
                if source == "change_intent":
                    values["intent"] = {"row_overrides": [{"row_id": retired_id, "muted": True}]}
                elif source == "change_requirements":
                    values["requirements"] = {"row_ids": [retired_id]}
                elif source == "change_summary":
                    values["summary"] = {"configuration_diff": {"deleted": {"id": retired_id, "slug": "retired"}}}
                else:
                    values["effects"] = [{"payload": {"collection_ids": [retired_id]}}]
                item = AssistantChange(
                    id="historical-change",
                    grant_id="old",
                    owner_account_id=42,
                    client_id="client",
                    grant_revision=1,
                    kind="row",
                    dependencies={},
                    content_hash="old",
                    expires_at=now + timedelta(hours=1),
                    **values,
                )
            elif source == "operation":
                item = AssistantOperation(
                    id="historical-operation",
                    grant_id="old",
                    owner_account_id=42,
                    client_id="client",
                    change_id="old-change",
                    idempotency_key="old",
                    request_hash="old",
                    authorization_basis="standing_grant",
                    result={"row_id": retired_id},
                )
            elif source in {"run_contract", "run_actor"}:
                stats = (
                    {"assistant_contract": {"intent": {"row_ids": [retired_id]}}}
                    if source == "run_contract"
                    else {"assistant_actor": {"requirements": {"row_ids": [retired_id]}}}
                )
                item = Run(trigger="assistant", status="ok", stats=stats)
            elif source in {"job_payload", "job_result"}:
                item = Job(
                    kind="assistant.converge", status="done", **{source.removeprefix("job_"): {"row_ids": [retired_id]}}
                )
            elif source.startswith("event_"):
                message = {
                    "event_collection": {"collection_id": retired_id},
                    "event_created": {"diff": {"created": {"id": retired_id, "slug": "retired"}}},
                    "event_setup_created": {"diff": {"row": {"created": {"id": retired_id, "slug": "retired"}}}},
                }[source]
                item = Event(scope="assistant.applied", message=message)
            else:
                item = PosterAsset(key=f"upload:{retired_id}", image=b"retained-poster", content_type="image/png")
            session.add(item)
            session.commit()

        command.upgrade(_alembic(tmp_path), "head")

        assert _insert_row(engine, "new-unrelated-row") > retired_id


@pytest.mark.parametrize("empty", [False, True])
def test_0109_preserves_existing_rows_indexes_and_foreign_keys_and_prevents_reuse(tmp_path, empty):
    with disposing_engine(_legacy(tmp_path)) as engine:
        if not empty:
            with Session(engine) as session:
                row = Collection(id=42, slug="preserved", name="Preserved", enabled=False, poster={"mode": "text"})
                user = User(id=12, plex_account_id=112, username="Person", slug="person")
                theme = Theme(id=7, slug="saved-theme", name="Saved theme")
                session.add_all([row, user, theme])
                session.flush()
                session.add_all(
                    [
                        CollectionAudience(collection_id=42, user_id=12),
                        ThemeHistory(
                            collection_id=42,
                            user_id=12,
                            theme_id=7,
                            theme_name="Saved theme",
                            state="current",
                            started_at=datetime.now(UTC),
                        ),
                        PosterAsset(key="upload:42", image=b"poster", content_type="image/png"),
                    ]
                )
                # Raw SQL: at 0108 the table still has the NOT NULL `prompt` column the model no longer maps.
                session.execute(
                    text(
                        "INSERT INTO collection_user_overrides (collection_id, user_id, muted, row_size, prompt, "
                        "updated_at) VALUES (42, 12, 1, 9, '{}', '2026-01-01 00:00:00')"
                    )
                )
                session.commit()
        else:
            with Session(engine) as session:
                session.query(Collection).delete()
                session.commit()
        tables = ("collections", "collection_audience", "collection_user_overrides", "theme_history", "poster_assets")
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
            before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
            indexes = db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='collections'"
            ).fetchall()
            foreign_keys = {table: db.execute(f"PRAGMA foreign_key_list({table})").fetchall() for table in tables}

        command.upgrade(_alembic(tmp_path), "0109")

        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0109",)
            assert (
                "AUTOINCREMENT"
                in db.execute("SELECT sql FROM sqlite_master WHERE name='collections'").fetchone()[0].upper()
            )
            assert {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before
            assert (
                db.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='collections'"
                ).fetchall()
                == indexes
            )
            assert {
                table: db.execute(f"PRAGMA foreign_key_list({table})").fetchall() for table in tables
            } == foreign_keys
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        first = _insert_row(engine, "temporary")
        with Session(engine) as session:
            session.delete(session.get(Collection, first))
            session.commit()
        assert _insert_row(engine, "replacement") > first


def test_0109_downgrade_keeps_identity_protection_for_the_previous_binary(tmp_path):
    with disposing_engine(_legacy(tmp_path)) as engine:
        command.upgrade(_alembic(tmp_path), "head")
        first = _insert_row(engine, "created-by-new-version")
        with Session(engine) as session:
            session.delete(session.get(Collection, first))
            session.commit()

        command.downgrade(_alembic(tmp_path), "0108")

        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0108",)
            assert (
                "AUTOINCREMENT"
                in db.execute("SELECT sql FROM sqlite_master WHERE name='collections'").fetchone()[0].upper()
            )
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _insert_row(engine, "created-after-downgrade") > first


def test_0109_preserves_a_preexisting_sequence_after_compatibility_downgrade(tmp_path):
    with disposing_engine(_legacy(tmp_path)) as engine:
        command.upgrade(_alembic(tmp_path), "head")
        with Session(engine) as session:
            row = Collection(id=2000, slug="retired-high-id", name="Retired")
            session.add(row)
            session.commit()
            session.delete(row)
            session.commit()
        command.downgrade(_alembic(tmp_path), "0108")

        command.upgrade(_alembic(tmp_path), "head")

        assert _insert_row(engine, "after-second-upgrade") > 2000


def test_0109_failed_foreign_key_check_rolls_back_original_schema_and_rows(tmp_path):
    with disposing_engine(_legacy(tmp_path)) as engine:
        _insert_row(engine, "must-survive-failed-upgrade")
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
            db.execute("INSERT INTO collection_audience(collection_id,user_id) VALUES(999,999)")
            db.commit()
            before_schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            before_rows = db.execute("SELECT * FROM collections ORDER BY id").fetchall()

        with pytest.raises(RuntimeError, match="inconsistent foreign keys"):
            command.upgrade(_alembic(tmp_path), "head")

        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
            assert (
                db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
                == before_schema
            )
            assert db.execute("SELECT * FROM collections ORDER BY id").fetchall() == before_rows
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0108",)
            assert db.execute("SELECT * FROM collection_audience").fetchall() == [(999, 999)]


def test_0109_refuses_foreign_keys_enabled_before_touching_schema(tmp_path, monkeypatch):
    with disposing_engine(_legacy(tmp_path)) as engine:
        migration = importlib.import_module("shortlist.server.db.alembic.versions.0109_non_reusable_row_ids")
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            before = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='collections'").scalar()
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))

            with pytest.raises(RuntimeError, match="foreign_keys=OFF"):
                migration.upgrade()

            assert (
                connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='collections'").scalar() == before
            )
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0108"


@pytest.mark.parametrize(
    ("table", "column"), [("events", "message"), ("jobs", "payload"), ("jobs", "result"), ("runs", "stats")]
)
def test_0109_skips_unreadable_history_json_and_still_reserves_every_readable_reference(tmp_path, table, column):
    with disposing_engine(_legacy(tmp_path)) as engine:
        retired_id = 809
        with Session(engine) as session:
            session.add(Job(kind="assistant.converge", status="done", payload={"row_ids": [retired_id]}))
            session.add(
                {
                    "events": Event(scope="run", message={}),
                    "jobs": Job(kind="x", status="done"),
                    "runs": Run(trigger="manual", status="ok", stats={}),
                }[table]
            )
            session.commit()
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
            db.execute(f"UPDATE {table} SET {column} = 'not json {{' WHERE id = (SELECT max(id) FROM {table})")
            db.commit()

        command.upgrade(_alembic(tmp_path), "0109")

        assert _insert_row(engine, "new-unrelated-row") > retired_id


def _assistant_record(table: str):
    now = datetime.now(UTC)
    if table == "assistant_grants":
        return AssistantGrant(
            id="g", owner_account_id=42, client_id="c", name="n", preset="manage_selected_rows", constraints={}
        )
    if table == "assistant_changes":
        return AssistantChange(
            id="c",
            grant_id="g",
            owner_account_id=42,
            client_id="c",
            grant_revision=1,
            kind="row",
            dependencies={},
            content_hash="h",
            expires_at=now + timedelta(hours=1),
            intent={},
            requirements={},
            summary={},
            effects=[],
        )
    return AssistantOperation(
        id="o",
        grant_id="g",
        owner_account_id=42,
        client_id="c",
        change_id="c",
        idempotency_key="k",
        request_hash="h",
        authorization_basis="standing_grant",
        result={},
    )


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("assistant_grants", "constraints"),
        ("assistant_changes", "intent"),
        ("assistant_changes", "requirements"),
        ("assistant_changes", "summary"),
        ("assistant_changes", "effects"),
        ("assistant_operations", "result"),
    ],
)
def test_0109_still_refuses_an_unreadable_assistant_record(tmp_path, table, column):
    with disposing_engine(_legacy(tmp_path)) as engine:
        with Session(engine) as session:
            session.add(_assistant_record(table))
            session.commit()
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
            db.execute(f"UPDATE {table} SET {column} = 'not json {{'")
            db.commit()

        with pytest.raises(RuntimeError, match=f"{table}.{column}"):
            command.upgrade(_alembic(tmp_path), "0109")
