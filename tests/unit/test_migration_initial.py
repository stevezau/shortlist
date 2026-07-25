"""The squashed initial migration must stay in lockstep with the models.

The 28 pre-release migrations were collapsed into a single `0001_initial`. This guards that the schema
it builds still matches `Base.metadata` exactly (same tables, same columns) — so the day someone adds a
column to a model and forgets the migration, this fails instead of a fresh install silently drifting
from the ORM. Compares column SETS, not raw DDL (column order and backfill-era server_defaults differ
harmlessly and aren't part of the model contract).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from shortlist.server.db.models import Base
from shortlist.server.db.session import make_engine, run_migrations


def _head_revision() -> str:
    """The newest migration's revision id, read from the scripts themselves — so these tests assert
    "migrated all the way" rather than pinning a literal that every new migration invalidates."""
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    from shortlist.server.db.session import ALEMBIC_DIR

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _columns(db_path: str) -> dict[str, set[str]]:
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%' and name != 'alembic_version'"
            )
        ]
        return {t: {r[1] for r in conn.execute(f"pragma table_info('{t}')")} for t in tables}
    finally:
        conn.close()


def test_initial_migration_schema_matches_the_models(tmp_path: Path):
    migrated_dir = tmp_path / "migrated"
    migrated_dir.mkdir()
    run_migrations(migrated_dir)
    from_migration = _columns(str(migrated_dir / "shortlist.db"))

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    engine = make_engine(model_dir)
    Base.metadata.create_all(engine)
    engine.dispose()
    from_models = _columns(str(model_dir / "shortlist.db"))

    assert from_migration == from_models, (
        "The initial migration drifted from the models. A model changed without updating "
        "shortlist/server/db/alembic/versions/0001_initial.py — regenerate or amend it."
    )


def test_a_db_stamped_at_a_squashed_revision_is_healed_not_crashed(tmp_path: Path):
    # The maintainer's pre-release DB is stamped at a now-removed revision (e.g. 0028). Booting must
    # re-stamp it to the baseline, not crash on "Can't locate revision".
    run_migrations(tmp_path)  # full schema, stamped at head
    db = tmp_path / "shortlist.db"
    conn = sqlite3.connect(db)
    conn.execute("update alembic_version set version_num = '0028'")  # a squashed-away revision
    conn.commit()
    conn.close()

    run_migrations(tmp_path)  # must heal, not raise

    conn = sqlite3.connect(db)
    version = conn.execute("select version_num from alembic_version").fetchone()[0]
    conn.close()
    # Healed to the baseline, then carried on to HEAD like any other DB. Asserted against the real
    # head rather than a literal, so adding a migration never breaks this test's meaning — plus the
    # observable consequence, because "stamped at head" is also true of a heal that stamped straight
    # there and ran no DDL at all.
    assert version == _head_revision()
    assert "reason" in _columns(str(db))["run_users"], "healed to head without applying the migrations"


def test_an_incomplete_db_at_a_squashed_revision_is_not_silently_healed(tmp_path: Path):
    # The safety branch: a DB stamped at a squashed revision but MISSING a table must NOT be marked
    # complete — better to fail loudly than silently stamp an incomplete schema as up-to-date.
    import pytest
    from alembic.util.exc import CommandError

    run_migrations(tmp_path)
    db = tmp_path / "shortlist.db"
    conn = sqlite3.connect(db)
    conn.execute("update alembic_version set version_num = '0028'")
    conn.execute("drop table events")  # schema now incomplete
    conn.commit()
    conn.close()

    with pytest.raises(CommandError):  # heal skips (table missing); upgrade then can't resolve '0028'
        run_migrations(tmp_path)

    conn = sqlite3.connect(db)
    version = conn.execute("select version_num from alembic_version").fetchone()[0]
    conn.close()
    assert version == "0028"  # left as-is, NOT rewritten to 0001


class TestOllamaProviderMerge:
    """0032 was a no-op on every real database; 0034 is the one that does the work.

    The bug it fixes was invisible to a test that seeds raw SQL, because the wrong shape is exactly
    what the broken migration assumed. So these seed through `SettingsStore` itself — whatever the
    app actually writes is what the migration has to read.
    """

    @staticmethod
    def _seed_at_0031(config_dir: Path, *, provider: str = "ollama") -> None:
        from alembic import command
        from alembic.config import Config as AlembicConfig

        from shortlist.server.db.session import ALEMBIC_DIR, db_url
        from shortlist.server.settings_store import SettingsStore

        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(ALEMBIC_DIR))
        cfg.set_main_option("sqlalchemy.url", db_url(config_dir))
        command.upgrade(cfg, "0031")  # the revision an instance running `dev` sat at

        engine = make_engine(config_dir)
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            store = SettingsStore(session)
            store.set("curator.provider", provider)
            if provider == "ollama":
                store.set("curator.ollama_url", "http://nas:11434")
            session.commit()

    @staticmethod
    def _read(config_dir: Path, key: str):
        from sqlalchemy.orm import Session

        from shortlist.server.settings_store import SettingsStore

        with Session(make_engine(config_dir)) as session:
            return SettingsStore(session).get(key)

    def test_an_ollama_instance_is_carried_onto_the_merged_provider(self, tmp_path: Path):
        self._seed_at_0031(tmp_path)

        run_migrations(tmp_path)

        assert self._read(tmp_path, "curator.provider") == "openai_compatible"
        assert self._read(tmp_path, "curator.openai_base_url") == "http://nas:11434/v1"

    def test_settings_stay_readable_afterwards(self, tmp_path: Path):
        """The half-fix that only corrected the READ would store an unwrapped string here, and every
        later `SettingsStore.get` — for any key — would raise TypeError."""
        self._seed_at_0031(tmp_path)

        run_migrations(tmp_path)

        from sqlalchemy.orm import Session

        from shortlist.server.settings_store import SettingsStore

        with Session(make_engine(tmp_path)) as session:
            assert SettingsStore(session).all_public()["curator.provider"] == "openai_compatible"

    def test_an_instance_on_another_provider_is_left_alone(self, tmp_path: Path):
        """Seeded at 0031 like the others, NOT by migrating to head first: a DB already stamped at
        head replays nothing, so 0034 would never run and the test would pass with its guard
        deleted."""
        self._seed_at_0031(tmp_path, provider="anthropic")

        run_migrations(tmp_path)

        assert self._read(tmp_path, "curator.provider") == "anthropic"
        assert not self._read(tmp_path, "curator.openai_base_url"), "nothing else may be written either"


class TestCurateSettingsCleared:
    """0036 clears the dead curation-recipe settings and the cut `llm_library` source.

    Seeded through the real stores/models — whatever the app writes is what the migration must read
    — then the DB is stamped back to 0035 so `run_migrations` genuinely replays 0036. A DB already at
    head replays nothing, so stamping back is what actually exercises the migration.
    """

    @staticmethod
    def _seed_and_replay(tmp_path: Path) -> tuple[int, int]:
        import sqlite3

        from sqlalchemy.orm import Session

        from shortlist.server.db.models import Collection, CollectionUserOverride, User
        from shortlist.server.settings_store import SettingsStore

        run_migrations(tmp_path)  # full schema at head
        engine = make_engine(tmp_path)
        with Session(engine) as session:
            store = SettingsStore(session)
            store.set("curator.prompt_tone", "adventurous")
            store.set("curator.prompt_guidance", "be bold")
            store.set("curator.prompt_template", "tpl")
            store.set("candidates.sources", ["tmdb_similar", "llm_library", "llm_web"])
            store.set("curator.provider", "anthropic")  # a real, untouched setting
            user = User(
                plex_account_id=1,
                username="bob",
                slug="bob",
                prefs={"prompt_tone": "x", "prompt_guidance": "y", "excluded_genres": ["Horror"]},
            )
            session.add(user)
            row = Collection(
                slug="scifi",
                name="SciFi",
                media="movie",
                build="per_person",
                candidate_sources=["llm_library", "tmdb_discover"],
                prompt={"tone": "dark"},
            )
            session.add(row)
            session.flush()
            session.add(CollectionUserOverride(collection_id=row.id, user_id=user.id, prompt={"tone": "z"}))
            session.commit()
            ids = (row.id, user.id)
        engine.dispose()

        # Stamp back to 0035 and re-migrate — twice, to prove idempotency (a second replay is a no-op).
        db = tmp_path / "shortlist.db"
        for _ in range(2):
            conn = sqlite3.connect(db)
            conn.execute("update alembic_version set version_num = '0035'")
            conn.commit()
            conn.close()
            run_migrations(tmp_path)
        return ids

    def test_dead_recipe_settings_and_cut_source_are_gone(self, tmp_path: Path):
        from sqlalchemy.orm import Session

        from shortlist.server.db.models import Collection, CollectionUserOverride, User
        from shortlist.server.settings_store import SettingsStore

        collection_id, user_id = self._seed_and_replay(tmp_path)

        with Session(make_engine(tmp_path)) as session:
            store = SettingsStore(session)
            assert store.get("curator.prompt_tone") is None
            assert store.get("curator.prompt_guidance") is None
            assert store.get("curator.prompt_template") is None
            assert store.get("candidates.sources") == ["tmdb_similar", "llm_web"]  # llm_library stripped
            # A real setting and a non-prompt pref are untouched.
            assert store.get("curator.provider") == "anthropic"
            assert session.get(User, user_id).prefs == {"excluded_genres": ["Horror"]}
            row = session.get(Collection, collection_id)
            assert row.candidate_sources == ["tmdb_discover"]  # llm_library stripped, order kept
            assert row.prompt == {}  # dead recipe cleared
            assert session.get(CollectionUserOverride, (collection_id, user_id)).prompt == {}
