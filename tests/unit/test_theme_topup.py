"""Top-up (#138): one extra AI call per theme, once someone's row has run out of the titles the AI named."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shortlist.engine.models import MediaType, RowLimits
from shortlist.engine.themes import ThemePick, ThemeSpec
from shortlist.server.db.models import Base, Collection, Event, PickRow, Run, Theme, ThemeHistory, User
from shortlist.server.services.theme_author import ThemeAuthorError, ThemeDraft, ThemeStats
from shortlist.server.services.theme_rotation import TOP_UP_CHANGE, AuthoringTools, top_up_themes

NOW = datetime(2026, 10, 10, 1, 30, tzinfo=UTC)


class _Author:
    """Stands in for `author_theme`: records what it was asked, names the titles it was given."""

    def __init__(self, new: list[int] | None = None, error: Exception | None = None, tokens: int = 70) -> None:
        self.new = [2, 3, 4] if new is None else new
        self.error = error
        self.tokens = tokens
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> ThemeDraft:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        spec = ThemeSpec(
            slug="x",
            name="Renamed by the AI",
            emoji=None,
            media=(MediaType.MOVIE,),
            tags=(999,),
            genres=("Comedy",),
            excluded_genres=(),
            collections=(),
            picks=tuple(ThemePick(i, MediaType.MOVIE, "ai", f"why {i}") for i in self.new),
            rules=RowLimits(max_runtime=30),
            min_votes=None,
        )
        return ThemeDraft(
            spec=spec,
            brief=kwargs["brief"],
            stats=ThemeStats(named=1, resolved=1, in_library=1, after_rules=1, unwatched_median=None),
            tokens=self.tokens,
            ai_reasons={},
            titles={(MediaType.MOVIE, i): f"Film {i}" for i in self.new},
        )


class _Tmdb:
    def search_keywords(self, query, limit=10):
        return []


class _Plex:
    """Reads Plex collections: ``members`` is {title -> tmdb ids of movies}; ``error`` makes every read raise."""

    def __init__(self, members: dict[str, list[int]] | None = None, error: bool = False) -> None:
        self.members = members or {}
        self.error = error

    def collection_members(self, section_key, title):
        if self.error:
            raise ConnectionError("PMS down")
        if title not in self.members:
            return None
        return [SimpleNamespace(tmdb_id=i, media_type=MediaType.MOVIE) for i in self.members[title]]


def _tools(unavailable: str = "", plex=None) -> AuthoringTools:
    return AuthoringTools(curator="curator", tmdb=_Tmdb(), plex=plex or _Plex(), unavailable=unavailable)


@pytest.fixture(autouse=True)
def _library_index(monkeypatch):
    from shortlist.server.services import theme_rotation

    monkeypatch.setattr(theme_rotation, "library_index", lambda plex, sessions, **kw: {})


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


def _theme(slug: str = "twists", **fields) -> Theme:
    return Theme(
        slug=slug,
        name="Twists",
        brief="films with a twist",
        media=["movie"],
        tags=[{"id": 7, "name": "twist ending"}],
        genres=["Thriller"],
        rules={"max_runtime": 120},
        picks=[
            {"tmdb_id": 1, "media": "movie", "origin": "ai", "reason": "r", "title": "Film 1", "year": 2000},
            {"tmdb_id": 2, "media": "movie", "origin": "ai", "reason": "r", "title": "Film 2", "year": 2001},
        ],
        ai_tokens=10,
        **fields,
    )


def seed(sessions, *, people: int = 1, mode: str = "fixed", paused: bool = False, topped: bool = False):
    """One enabled AI row on a theme; ``people`` users. Returns (row id, theme id, user ids)."""
    with sessions() as s:
        theme = _theme(topped_up_at=NOW.replace(tzinfo=None) if topped else None)
        s.add(theme)
        s.flush()
        row = Collection(
            slug="ai-row",
            name="AI row",
            theme_id=theme.id,
            theme_mode=mode,
            media="both",
            library_keys=["1"],
            enabled=True,
            ai_paused=paused,
            ai_tokens=5,
        )
        users = [
            User(plex_account_id=i + 1, username=f"u{i}", slug=f"u{i}", enabled=True, user_type="shared")
            for i in range(people)
        ]
        s.add_all([row, *users])
        s.commit()
        return row.id, theme.id, [u.id for u in users]


def ran(sessions, user_id: int, tmdb_ids: list[int], *, dry_run: bool = False) -> None:
    """A finished run in which the person's row showed these movies."""
    with sessions() as s:
        run = Run(trigger="schedule", status="ok", dry_run=dry_run)
        s.add(run)
        s.flush()
        for rank, tmdb_id in enumerate(tmdb_ids):
            s.add(
                PickRow(
                    run_id=run.id,
                    user_id=user_id,
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    rating_key=tmdb_id,
                    rank=rank,
                    collection_slug="ai-row",
                )
            )
        s.commit()


def top_up(sessions, author, *, tools=None, dry_run=False) -> int:
    return top_up_themes(
        sessions, now=NOW, secrets=object(), author=author, tools=tools or (lambda: _tools()), dry_run=dry_run
    )


def stored(sessions, theme_id: int) -> Theme:
    with sessions() as s:
        theme = s.get(Theme, theme_id)
        s.expunge(theme)
        return theme


class TestAThemeIsToppedUpOnce:
    def test_filler_in_the_latest_run_triggers_one_call_that_merges_new_titles(self, sessions):
        row_id, theme_id, (uid,) = seed(sessions)
        ran(sessions, uid, [1, 500])
        author = _Author(new=[2, 3, 4])

        assert top_up(sessions, author) == 1

        assert len(author.calls) == 1
        call = author.calls[0]
        assert call["change"] == TOP_UP_CHANGE
        assert call["media"] == (MediaType.MOVIE, MediaType.SHOW)
        assert call["current"].slug == "twists"
        assert call["brief"] == "films with a twist"
        theme = stored(sessions, theme_id)
        assert [p["tmdb_id"] for p in theme.picks] == [1, 2, 3, 4]
        assert theme.picks[0]["year"] == 2000 and theme.picks[2]["title"] == "Film 3"
        assert (theme.name, theme.genres, theme.rules) == ("Twists", ["Thriller"], {"max_runtime": 120})
        assert theme.tags == [{"id": 7, "name": "twist ending"}]
        assert theme.ai_tokens == 80
        assert theme.stats["topped_up"] == 2
        assert theme.topped_up_at is not None
        with sessions() as s:
            assert s.get(Collection, row_id).ai_tokens == 75
            event = s.scalars(select(Event).where(Event.scope == "theme.topped_up")).one()
            assert event.message["titles_before"] == 2 and event.message["titles_after"] == 4
            assert event.message["ok"] is True and event.message["tokens"] == 70

    def test_every_title_the_ai_named_means_no_call(self, sessions):
        _, theme_id, (uid,) = seed(sessions)
        ran(sessions, uid, [1, 2])
        author = _Author()

        assert top_up(sessions, author) == 0

        assert author.calls == []
        assert stored(sessions, theme_id).topped_up_at is None

    def test_filler_only_in_a_dry_run_run_means_no_call(self, sessions):
        _, _, (uid,) = seed(sessions)
        ran(sessions, uid, [1, 2])
        ran(sessions, uid, [1, 500], dry_run=True)
        author = _Author()

        assert top_up(sessions, author) == 0
        assert author.calls == []

    def test_a_theme_already_topped_up_is_never_topped_up_again(self, sessions):
        _, _, (uid,) = seed(sessions, topped=True)
        ran(sessions, uid, [1, 500])
        author = _Author()

        assert top_up(sessions, author) == 0
        assert author.calls == []

    def test_a_paused_row_makes_no_call_and_is_not_marked(self, sessions):
        _, theme_id, (uid,) = seed(sessions, paused=True)
        ran(sessions, uid, [1, 500])
        author = _Author()

        assert top_up(sessions, author) == 0
        assert author.calls == []
        assert stored(sessions, theme_id).topped_up_at is None

    def test_no_provider_makes_no_call_and_is_not_marked(self, sessions):
        _, theme_id, (uid,) = seed(sessions)
        ran(sessions, uid, [1, 500])
        author = _Author()

        assert top_up(sessions, author, tools=lambda: _tools("Writing a theme needs an AI provider.")) == 0
        assert author.calls == []
        assert stored(sessions, theme_id).topped_up_at is None

    def test_an_unreadable_answer_uses_the_one_spend_and_keeps_the_list(self, sessions):
        _, theme_id, (uid,) = seed(sessions)
        ran(sessions, uid, [1, 500])
        author = _Author(error=ThemeAuthorError("The AI did not answer."))

        assert top_up(sessions, author) == 1
        assert top_up(sessions, author) == 0

        assert len(author.calls) == 1
        theme = stored(sessions, theme_id)
        assert [p["tmdb_id"] for p in theme.picks] == [1, 2]
        assert theme.topped_up_at is not None
        with sessions() as s:
            event = s.scalars(select(Event).where(Event.scope == "theme.topped_up")).one()
            assert event.message["ok"] is False

    def test_a_dry_run_calls_nothing_and_changes_nothing(self, sessions):
        _, theme_id, (uid,) = seed(sessions)
        ran(sessions, uid, [1, 500])
        author = _Author()

        assert top_up(sessions, author, dry_run=True) == 0

        assert author.calls == []
        assert stored(sessions, theme_id).topped_up_at is None
        with sessions() as s:
            assert s.scalars(select(Event)).all() == []

    def test_a_title_already_on_the_list_is_not_added_twice(self, sessions):
        _, theme_id, (uid,) = seed(sessions)
        ran(sessions, uid, [1, 500])

        top_up(sessions, _Author(new=[1, 2, 3]))

        assert [p["tmdb_id"] for p in stored(sessions, theme_id).picks] == [1, 2, 3]


class TestOnlyTagAndGenreMatchesCountAsRunningOut:
    @staticmethod
    def _theme_with(sessions, theme_id: int, **fields) -> None:
        with sessions() as s:
            theme = s.get(Theme, theme_id)
            for key, value in fields.items():
                setattr(theme, key, value)
            s.commit()

    def test_an_owner_added_pick_in_the_row_is_not_filler(self, sessions):
        _, theme_id, (uid,) = seed(sessions)
        self._theme_with(
            sessions,
            theme_id,
            picks=[{"tmdb_id": 9, "media": "movie", "origin": "owner", "reason": None, "title": "Mine", "year": 1}],
        )
        ran(sessions, uid, [9])
        author = _Author()

        assert top_up(sessions, author) == 0
        assert author.calls == []

    def test_a_collection_member_in_the_row_is_not_filler(self, sessions):
        _, theme_id, (uid,) = seed(sessions)
        collection = {"section_key": "1", "section_title": "Movies", "title": "Favourites"}
        self._theme_with(sessions, theme_id, collections=[collection])
        ran(sessions, uid, [1, 77])
        author = _Author()

        assert top_up(sessions, author, tools=lambda: _tools(plex=_Plex({"Favourites": [77]}))) == 0
        assert author.calls == []

    def test_a_title_outside_picks_and_collections_is_filler(self, sessions):
        _, theme_id, (uid,) = seed(sessions)
        collection = {"section_key": "1", "section_title": "Movies", "title": "Favourites"}
        self._theme_with(sessions, theme_id, collections=[collection])
        ran(sessions, uid, [1, 77, 500])
        author = _Author()

        assert top_up(sessions, author, tools=lambda: _tools(plex=_Plex({"Favourites": [77]}))) == 1
        assert len(author.calls) == 1

    @pytest.mark.parametrize("plex", [_Plex(error=True), _Plex({})], ids=["raises", "collection-missing"])
    def test_an_unreadable_collection_is_not_low_and_spends_nothing(self, sessions, plex):
        _, theme_id, (uid,) = seed(sessions)
        collection = {"section_key": "1", "section_title": "Movies", "title": "Favourites"}
        self._theme_with(sessions, theme_id, collections=[collection])
        ran(sessions, uid, [1, 500])
        author = _Author()

        assert top_up(sessions, author, tools=lambda: _tools(plex=plex)) == 0
        assert author.calls == []
        assert stored(sessions, theme_id).topped_up_at is None


class TestExploreTopsUpOnlyTheThemeThatRanLow:
    def test_only_the_person_whose_theme_ran_low_gets_their_theme_topped_up(self, sessions):
        row_id, _, (ann, bob) = seed(sessions, people=2, mode="explore")
        with sessions() as s:
            ann_theme, bob_theme = _theme("ann-theme"), _theme("bob-theme")
            s.add_all([ann_theme, bob_theme])
            s.flush()
            for user_id, theme in ((ann, ann_theme), (bob, bob_theme)):
                s.add(
                    ThemeHistory(
                        collection_id=row_id,
                        user_id=user_id,
                        theme_id=theme.id,
                        theme_name=theme.name,
                        state="current",
                        started_at=NOW.replace(tzinfo=None),
                    )
                )
            s.commit()
            ann_id, bob_id = ann_theme.id, bob_theme.id
        ran(sessions, ann, [1, 500])
        ran(sessions, bob, [1, 2])
        author = _Author()

        assert top_up(sessions, author) == 1

        assert len(author.calls) == 1
        assert author.calls[0]["current"].slug == "ann-theme"
        assert stored(sessions, ann_id).topped_up_at is not None
        assert stored(sessions, bob_id).topped_up_at is None
