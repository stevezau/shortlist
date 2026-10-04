"""Explore rotation (#138): each person's theme changes on a schedule, and a failure never empties a row."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import MediaType, RowLimits, UserProfile
from shortlist.engine.themes import ThemeSpec
from shortlist.engine.web_guidance import AiInstructions
from shortlist.server.db.models import Base, Collection, CollectionAudience, Event, Theme, ThemeHistory, User
from shortlist.server.services import theme_rotation
from shortlist.server.services.theme_author import ThemeAuthorError, ThemeDraft, ThemeStats
from shortlist.server.services.theme_rotation import (
    NEXT_LEAD_DAYS,
    RotationOutcome,
    recent_theme_names,
    rotate_themes,
    theme_guidance,
)
from tests.conftest import make_profile

NOW = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)
NAIVE_NOW = NOW.replace(tzinfo=None)
LIBRARY_INDEX = {MediaType.MOVIE: {1: 11}}


class FakeAuthor:
    """Stands in for `author_theme`: records what it was asked, answers with a new theme each time."""

    def __init__(self, tokens: int = 50, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.tokens = tokens
        self.error = error
        self._n = itertools.count(1)

    def __call__(self, **kwargs) -> ThemeDraft:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        n = next(self._n)
        spec = ThemeSpec(
            slug=f"t{n}",
            name=f"Theme {n}",
            emoji=None,
            media=(MediaType.MOVIE,),
            tags=(),
            genres=("Thriller",),
            excluded_genres=(),
            collections=(),
            picks=(),
            rules=RowLimits(),
            min_votes=None,
        )
        return ThemeDraft(
            spec=spec,
            brief=kwargs["brief"],
            stats=ThemeStats(named=1, resolved=1, in_library=1, after_rules=1, unwatched_median=None),
            tokens=self.tokens,
            ai_reasons={},
        )


@pytest.fixture(autouse=True)
def _library_index(monkeypatch):
    monkeypatch.setattr(theme_rotation, "library_index", lambda plex, sessions, **kw: LIBRARY_INDEX)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


def seed(sessions, *, people: int = 1, **row) -> tuple[int, list[int]]:
    """An enabled explore row on a starter theme, and ``people`` enabled users. Returns (row id, user ids)."""
    with sessions() as s:
        starter = Theme(slug="starter", name="Starter", media=["movie"], genres=["Drama"])
        s.add(starter)
        s.flush()
        fields = {
            "slug": "ai-row",
            "name": "AI row",
            "theme_id": starter.id,
            "theme_mode": "explore",
            "explore_brief": "cosy nights",
            "media": "movie",
            "library_keys": ["1"],
            "enabled": True,
            **row,
        }
        collection = Collection(**fields)
        users = [
            User(plex_account_id=i + 1, username=f"u{i}", slug=f"u{i}", enabled=True, user_type="shared")
            for i in range(people)
        ]
        s.add_all([collection, *users])
        s.commit()
        return collection.id, [u.id for u in users]


def add_history(sessions, collection_id, user_id, state, *, started: datetime, name="Old", theme=True, due=None):
    with sessions() as s:
        theme_id = None
        if theme:
            t = Theme(slug=f"h-{name}-{state}-{started.timestamp()}", name=name, media=["movie"], genres=["Drama"])
            s.add(t)
            s.flush()
            theme_id = t.id
        s.add(
            ThemeHistory(
                collection_id=collection_id,
                user_id=user_id,
                theme_id=theme_id,
                theme_name=name,
                state=state,
                started_at=started,
                due_at=due,
            )
        )
        s.commit()


def profile_for(session, user_id: int) -> UserProfile:
    return make_profile(f"profile-{user_id}", account_id=user_id, slug=f"u{user_id}")


def rotate(sessions, author, now: datetime = NOW):
    return rotate_themes(
        sessions,
        now=now,
        secrets=object(),
        author=author,
        curator="curator",
        tmdb=_Tmdb(),
        plex="plex",
        profile_for=profile_for,
    )


class _Tmdb:
    def search_keywords(self, query, limit=10):
        return []


def history_of(sessions, collection_id, user_id) -> list[tuple[str, str]]:
    with sessions() as s:
        rows = s.scalars(
            select(ThemeHistory)
            .where(ThemeHistory.collection_id == collection_id, ThemeHistory.user_id == user_id)
            .order_by(ThemeHistory.id)
        )
        return [(r.state, r.theme_name) for r in rows]


def events(sessions, level: str | None = None) -> list[Event]:
    with sessions() as s:
        query = select(Event).where(Event.scope == "theme.rotate")
        if level:
            query = query.where(Event.level == level)
        return list(s.scalars(query))


class TestRotate:
    def test_a_person_with_no_current_gets_one(self, sessions):
        row_id, (uid,) = seed(sessions)
        author = FakeAuthor()

        outcomes = rotate(sessions, author)

        assert outcomes == [RotationOutcome(row_id, uid, "authored_current")]
        assert history_of(sessions, row_id, uid) == [("current", "Theme 1")]
        with sessions() as s:
            row = s.get(Collection, row_id)
            current = s.scalars(select(ThemeHistory)).one()
            assert row.theme_id != current.theme_id
            assert current.started_at == NAIVE_NOW
            assert current.due_at == NAIVE_NOW + timedelta(days=7)

    def test_a_current_theme_with_days_to_run_is_kept(self, sessions):
        row_id, (uid,) = seed(sessions, theme_days=7)
        add_history(sessions, row_id, uid, "current", started=NAIVE_NOW - timedelta(days=3))
        author = FakeAuthor()

        outcomes = rotate(sessions, author)

        assert [o.action for o in outcomes] == ["kept"]
        assert author.calls == []

    def test_the_next_theme_is_authored_in_the_last_day(self, sessions):
        row_id, (uid,) = seed(sessions, theme_days=7, prompt={"mode": "own", "text": "Be bold"})
        started = NAIVE_NOW - timedelta(days=7 - NEXT_LEAD_DAYS)
        add_history(sessions, row_id, uid, "current", started=started, name="Cosy")
        add_history(sessions, row_id, uid, "past", started=started - timedelta(days=7), name="Bleak")
        author = FakeAuthor()

        outcomes = rotate(sessions, author)

        assert [o.action for o in outcomes] == ["authored_next"]
        call = author.calls[0]
        assert "cosy nights" in call["brief"]
        assert "Avoid these recent theme names: Cosy, Bleak." in call["brief"]
        assert call["profile"].username == f"profile-{uid}"
        assert call["guidance"] == theme_guidance(AiInstructions(mode="own", text="Be bold")) == "Be bold"
        assert call["media"] == (MediaType.MOVIE,)
        assert call["library_index"] == LIBRARY_INDEX
        assert call["curator"] == "curator"
        assert history_of(sessions, row_id, uid)[-1] == ("next", "Theme 1")
        with sessions() as s:
            nxt = s.scalars(select(ThemeHistory).where(ThemeHistory.state == "next")).one()
            assert nxt.due_at == started + timedelta(days=7)

    def test_an_existing_next_is_not_authored_again(self, sessions):
        row_id, (uid,) = seed(sessions, theme_days=7)
        started = NAIVE_NOW - timedelta(days=6, hours=2)
        add_history(sessions, row_id, uid, "current", started=started)
        add_history(sessions, row_id, uid, "next", started=started, name="Queued")
        author = FakeAuthor()

        assert [o.action for o in rotate(sessions, author)] == ["kept"]
        assert author.calls == []

    def test_a_due_current_is_replaced_by_its_next(self, sessions):
        row_id, (uid,) = seed(sessions, theme_days=7)
        started = NAIVE_NOW - timedelta(days=7)
        add_history(sessions, row_id, uid, "current", started=started, name="Cosy")
        add_history(sessions, row_id, uid, "next", started=started, name="Queued")
        author = FakeAuthor()

        outcomes = rotate(sessions, author)

        assert [o.action for o in outcomes] == ["promoted"]
        assert author.calls == []
        assert history_of(sessions, row_id, uid) == [("past", "Cosy"), ("current", "Queued")]
        with sessions() as s:
            promoted = s.scalars(select(ThemeHistory).where(ThemeHistory.state == "current")).one()
            assert promoted.started_at == NAIVE_NOW

    def test_a_due_current_with_no_next_authors_a_current_not_a_gap(self, sessions):
        row_id, (uid,) = seed(sessions, theme_days=7)
        add_history(sessions, row_id, uid, "current", started=NAIVE_NOW - timedelta(days=9), name="Cosy")
        author = FakeAuthor()

        outcomes = rotate(sessions, author)

        assert [o.action for o in outcomes] == ["authored_current"]
        assert history_of(sessions, row_id, uid) == [("past", "Cosy"), ("current", "Theme 1")]

    def test_a_deleted_current_theme_is_authored_again(self, sessions):
        row_id, (uid,) = seed(sessions)
        add_history(sessions, row_id, uid, "current", started=NAIVE_NOW - timedelta(days=1), theme=False, name="Gone")
        author = FakeAuthor()

        outcomes = rotate(sessions, author)

        assert [o.action for o in outcomes] == ["authored_current"]
        assert history_of(sessions, row_id, uid) == [("past", "Gone"), ("current", "Theme 1")]

    def test_a_deleted_next_theme_is_replaced(self, sessions):
        row_id, (uid,) = seed(sessions, theme_days=7)
        started = NAIVE_NOW - timedelta(days=7)
        add_history(sessions, row_id, uid, "current", started=started, name="Cosy")
        add_history(sessions, row_id, uid, "next", started=started, theme=False, name="Gone")

        outcomes = rotate(sessions, FakeAuthor())

        assert [o.action for o in outcomes] == ["authored_current"]
        assert ("next", "Gone") not in history_of(sessions, row_id, uid)

    def test_failed_write_keeps_current_theme(self, sessions):
        row_id, (uid,) = seed(sessions, theme_days=7)
        add_history(sessions, row_id, uid, "current", started=NAIVE_NOW - timedelta(days=8), name="Cosy")
        author = FakeAuthor(error=ThemeAuthorError("The AI's answer was not a theme I could read."))

        outcomes = rotate(sessions, author)

        assert [o.action for o in outcomes] == ["failed"]
        assert history_of(sessions, row_id, uid) == [("current", "Cosy")]
        with sessions() as s:
            assert s.scalars(select(Theme.slug).where(Theme.slug.like("theme-%"))).all() == []
        assert len(events(sessions, "error")) == 1

    def test_an_unexpected_failure_logs_only_its_class(self, sessions):
        seed(sessions)

        rotate(sessions, FakeAuthor(error=RuntimeError("sk-secret-key leaked")))

        [event] = events(sessions, "error")
        assert event.message["detail"] == "RuntimeError"

    def test_a_paused_row_is_not_authored_for(self, sessions):
        row_id, (uid,) = seed(sessions, ai_paused=True)
        author = FakeAuthor()

        outcomes = rotate(sessions, author)

        assert [o.action for o in outcomes] == ["skipped_paused"]
        assert author.calls == []
        assert history_of(sessions, row_id, uid) == []
        assert len(events(sessions)) == 1

    def test_a_paused_row_still_promotes_a_theme_already_queued(self, sessions):
        row_id, (uid,) = seed(sessions, ai_paused=True, theme_days=7)
        started = NAIVE_NOW - timedelta(days=7)
        add_history(sessions, row_id, uid, "current", started=started, name="Cosy")
        add_history(sessions, row_id, uid, "next", started=started, name="Queued")

        assert [o.action for o in rotate(sessions, FakeAuthor())] == ["promoted"]

    def test_tokens_are_charged_to_the_row(self, sessions):
        row_id, _ = seed(sessions)

        rotate(sessions, FakeAuthor(tokens=75))

        with sessions() as s:
            assert s.get(Collection, row_id).ai_tokens == 75

    def test_two_people_rotate_independently(self, sessions):
        row_id, (a, b) = seed(sessions, people=2, theme_days=7)
        started = NAIVE_NOW - timedelta(days=7)
        for uid in (a, b):
            add_history(sessions, row_id, uid, "current", started=started, name=f"Cosy{uid}")
        add_history(sessions, row_id, a, "next", started=started, name="QueuedA")
        outcomes = rotate(sessions, FakeAuthor())

        by_user = {o.user_id: o.action for o in outcomes}
        assert by_user == {a: "promoted", b: "authored_current"}
        assert history_of(sessions, row_id, a) == [("past", f"Cosy{a}"), ("current", "QueuedA")]
        assert history_of(sessions, row_id, b) == [("past", f"Cosy{b}"), ("current", "Theme 1")]

    def test_theme_days_none_behaves_as_seven(self, sessions):
        row_id, (uid,) = seed(sessions, theme_days=None)
        add_history(sessions, row_id, uid, "current", started=NAIVE_NOW - timedelta(days=5))

        assert [o.action for o in rotate(sessions, FakeAuthor())] == ["kept"]

    @pytest.mark.parametrize(
        "row",
        [{"theme_mode": "fixed"}, {"theme_id": None}, {"enabled": False}, {"audience": "subset"}],
        ids=["fixed", "not-an-ai-row", "disabled", "empty-subset"],
    )
    def test_rows_that_do_not_rotate_are_never_touched(self, sessions, row):
        _, _ = seed(sessions, **row)
        author = FakeAuthor()

        assert rotate(sessions, author) == []
        assert author.calls == []

    def test_a_subset_row_rotates_only_its_audience(self, sessions):
        row_id, (_, b) = seed(sessions, people=2, audience="subset")
        with sessions() as s:
            s.add(CollectionAudience(collection_id=row_id, user_id=b))
            s.commit()

        outcomes = rotate(sessions, FakeAuthor())

        assert [o.user_id for o in outcomes] == [b]


class TestQueuedNext:
    def test_a_queued_next_with_no_current_becomes_current_without_the_ai(self, sessions):
        row_id, (uid,) = seed(sessions)
        add_history(sessions, row_id, uid, "next", started=NAIVE_NOW - timedelta(days=1), name="Queued")
        author = FakeAuthor()

        outcomes = rotate(sessions, author)

        assert [o.action for o in outcomes] == ["promoted"]
        assert author.calls == []
        assert history_of(sessions, row_id, uid) == [("current", "Queued")]

    def test_queueing_replaces_any_existing_next_and_starts_when_the_current_ends(self, sessions):
        row_id, (uid,) = seed(sessions, theme_days=7)
        started = NAIVE_NOW - timedelta(days=2)
        add_history(sessions, row_id, uid, "current", started=started, name="Cosy")
        add_history(sessions, row_id, uid, "next", started=started, name="Old queue")

        with sessions() as s:
            theme = Theme(slug="fresh", name="Fresh", media=["movie"], genres=["Drama"])
            s.add(theme)
            s.flush()
            theme_rotation.queue_next(s, s.get(Collection, row_id), uid, theme, NOW)
            s.commit()

        assert history_of(sessions, row_id, uid) == [("current", "Cosy"), ("next", "Fresh")]
        with sessions() as s:
            queued = s.scalars(select(ThemeHistory).where(ThemeHistory.state == "next")).one()
            assert queued.due_at == started + timedelta(days=7)


class TestUnavailable:
    def test_no_provider_fails_each_target_that_needed_a_theme_and_changes_nothing(self, sessions):
        row_id, (needy, kept) = seed(sessions, people=2, theme_days=7)
        add_history(sessions, row_id, kept, "current", started=NAIVE_NOW - timedelta(days=1), name="Cosy")
        author = FakeAuthor()

        outcomes = rotate_themes(
            sessions,
            now=NOW,
            secrets=object(),
            unavailable="Choosing new themes needs an AI provider. Add one in Settings.",
            author=author,
            curator=None,
            tmdb=None,
            plex=None,
            profile_for=profile_for,
        )

        assert {o.user_id: o.action for o in outcomes} == {needy: "failed", kept: "kept"}
        assert author.calls == []
        assert history_of(sessions, row_id, needy) == []
        [event] = events(sessions, "error")
        assert event.message["detail"].startswith("Choosing new themes needs an AI provider")


class TestPromoteNext:
    def test_promotes_one_person_and_leaves_the_other_alone(self, sessions):
        row_id, (a, b) = seed(sessions, people=2, theme_days=3)
        started = NAIVE_NOW - timedelta(days=3)
        for uid in (a, b):
            add_history(sessions, row_id, uid, "current", started=started, name=f"Cosy{uid}")
            add_history(sessions, row_id, uid, "next", started=started, name=f"Queued{uid}")
        before_b = history_of(sessions, row_id, b)

        with sessions() as s:
            theme_rotation.promote_next(s, row_id, a, NOW)
            s.commit()

        assert history_of(sessions, row_id, b) == before_b
        assert history_of(sessions, row_id, a) == [("past", f"Cosy{a}"), ("current", f"Queued{a}")]
        with sessions() as s:
            promoted = s.scalars(
                select(ThemeHistory).where(ThemeHistory.user_id == a, ThemeHistory.state == "current")
            ).one()
            assert (promoted.started_at, promoted.due_at) == (NAIVE_NOW, NAIVE_NOW + timedelta(days=3))


class TestHelpers:
    def test_recent_names_are_newest_first_and_capped_at_six(self, sessions):
        row_id, (uid,) = seed(sessions)
        for day in range(8):
            add_history(sessions, row_id, uid, "past", started=NAIVE_NOW - timedelta(days=20 - day), name=f"T{day}")

        with sessions() as s:
            assert recent_theme_names(s, row_id, uid) == ["T7", "T6", "T5", "T4", "T3", "T2"]

    @pytest.mark.parametrize(
        ("instructions", "expected_start"),
        [(None, ""), (AiInstructions(mode="default"), ""), (AiInstructions(mode="own", text="Mine"), "Mine")],
    )
    def test_guidance_for_default_and_own(self, instructions, expected_start):
        assert theme_guidance(instructions) == expected_start

    def test_add_guidance_follows_the_default_wording(self):
        from shortlist.server.services.theme_author import BUILD_SYSTEM_GUIDANCE

        assert theme_guidance(AiInstructions(mode="add", text="More")) == f"{BUILD_SYSTEM_GUIDANCE.strip()} More"
