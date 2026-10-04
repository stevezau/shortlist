"""The theme store (#138): one save path for the themes API and the rotation job."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.themes import theme_content_hash
from shortlist.server.api.themes import ThemeIn, ThemeSaveIn
from shortlist.server.db.models import Base, Collection, Theme
from shortlist.server.services import theme_store


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as s:
        yield s


def body(name: str = "Twist endings", *, tokens: int = 0, collection_id: int | None = None) -> ThemeSaveIn:
    draft = ThemeIn(name=name, media=["movie"], genres=["Thriller"], origin="ai")
    return ThemeSaveIn(draft=draft, tokens=tokens, collection_id=collection_id)


def add_row(session, **overrides) -> Collection:
    row = Collection(slug="ai-row", name="AI row", **overrides)
    session.add(row)
    session.flush()
    return row


SECRETS = SimpleNamespace()


class TestSaveTheme:
    def test_paused_row_refuses_a_save_that_spent_tokens(self, session):
        row = add_row(session, ai_paused=True)

        with pytest.raises(theme_store.RowPaused):
            theme_store.save_theme(session, SECRETS, body(tokens=40, collection_id=row.id))

        assert session.query(Theme).count() == 0

    def test_paused_row_accepts_a_save_that_spent_nothing(self, session):
        row = add_row(session, ai_paused=True)

        saved = theme_store.save_theme(session, SECRETS, body(tokens=0, collection_id=row.id))

        assert saved.slug == "twist-endings"
        assert row.ai_tokens == 0

    def test_tokens_are_added_to_the_collection(self, session):
        row = add_row(session, ai_tokens=10)

        saved = theme_store.save_theme(session, SECRETS, body(tokens=40, collection_id=row.id))

        assert row.ai_tokens == 50
        assert saved.ai_tokens == 40

    def test_slug_collision_gets_a_unique_slug(self, session):
        first = theme_store.save_theme(session, SECRETS, body())
        second = theme_store.save_theme(session, SECRETS, body())

        assert (first.slug, second.slug) == ("twist-endings", "twist-endings-2")

    def test_content_hash_comes_from_the_columns(self, session):
        saved = theme_store.save_theme(session, SECRETS, body())

        assert saved.content_hash == theme_content_hash(theme_store.spec_from_row(saved))

    def test_rewriting_an_existing_theme_keeps_its_slug_and_adds_tokens(self, session):
        saved = theme_store.save_theme(session, SECRETS, body(tokens=5))

        again = theme_store.save_theme(session, SECRETS, body(name="Renamed", tokens=7), existing=saved)

        assert (again.slug, again.name, again.ai_tokens) == ("twist-endings", "Renamed", 12)

    def test_unknown_collection_is_a_lookup_error(self, session):
        with pytest.raises(LookupError):
            theme_store.save_theme(session, SECRETS, body(collection_id=99))
