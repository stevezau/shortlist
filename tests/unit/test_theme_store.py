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


class TestPersonThemeTitles:
    """An explore row is titled from each person's own theme (#121 class): no row follows that theme as its base."""

    @staticmethod
    def seed(session):
        from datetime import datetime

        from shortlist.server.db.models import ThemeHistory, User

        base = Theme(slug="base", name="Base", media=["movie"], genres=["Drama"])
        person_theme = Theme(slug="mine", name="Cosy Nights", media=["movie"], genres=["Drama"])
        user = User(plex_account_id=7, username="ann", slug="ann", enabled=True, user_type="shared")
        session.add_all([base, person_theme, user])
        session.flush()
        explore = Collection(
            slug="explore",
            name="Explore",
            name_template="{theme}",
            theme_id=base.id,
            theme_mode="explore",
            build="per_person",
            enabled=True,
            media="movie",
        )
        session.add(explore)
        session.flush()
        session.add(
            ThemeHistory(
                collection_id=explore.id,
                user_id=user.id,
                theme_id=person_theme.id,
                theme_name="Cosy Nights",
                state="current",
                started_at=datetime(2026, 10, 1),
            )
        )
        session.flush()
        return explore, person_theme, user

    def test_a_new_row_cannot_take_the_title_a_person_theme_renders(self, session):
        from shortlist.server.services import collection_reconcile

        self.seed(session)

        clashes = collection_reconcile.rows_titled_from(session, "Cosy Nights", build="per_person", media="movie")

        assert [row.slug for row in clashes] == ["explore"]

    def test_renaming_a_theme_a_person_holds_is_refused_when_a_sibling_has_that_name(self, session):
        _, person_theme, _ = self.seed(session)
        session.add(Collection(slug="sibling", name="Renamed", build="per_person", enabled=True, media="movie"))
        person_theme.name = "Renamed"
        session.flush()

        with pytest.raises(theme_store.TitleClash):
            theme_store.reject_title_clashes(session, SECRETS, person_theme)

    def test_a_sibling_the_person_is_not_in_the_audience_of_does_not_clash(self, session):
        explore, person_theme, user = self.seed(session)
        session.add(
            Collection(
                slug="sibling", name="Cosy Nights", build="per_person", enabled=True, media="movie", audience="subset"
            )
        )
        session.flush()

        theme_store.reject_person_title_clash(session, SECRETS, explore, user.id, person_theme)
