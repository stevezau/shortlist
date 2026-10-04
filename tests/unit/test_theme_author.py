"""Authoring a theme from a brief (#138): one AI call, every claim checked against TMDB."""

from __future__ import annotations

import json

import pytest

from shortlist.engine.models import MediaType, RowLimits, UserProfile, UserType, WatchedItem
from shortlist.engine.themes import ThemePick, ThemeSpec
from shortlist.server.services.theme_author import (
    BUILD_SYSTEM_MECHANICS,
    ThemeAuthorError,
    author_theme,
    diff_themes,
)

BRIEF = "mind-bending films with a twist ending"
CATALOGUE = {
    "Memento": (1, 2000, 113),
    "Se7en": (2, 1995, 127),
    "The Prestige": (3, 2006, 130),
    "Short Cut": (4, 2010, 80),
}


class _Curator:
    name = "anthropic"
    can_complete = True
    last_tokens = 321

    def __init__(self, answer: str):
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.answer


class _Tmdb:
    def __init__(self):
        self.by_id = {i: (t, y, r) for t, (i, y, r) in CATALOGUE.items()}

    def search(self, title, media, *, year=None):
        found = CATALOGUE.get(title)
        return {"id": found[0], "title": title} if found else None

    def search_keywords(self, query, limit=10):
        return [{"id": 900, "name": "other"}, {"id": 901, "name": "twist ending"}] if query == "twist ending" else []

    def discover_all(self, media, params):
        return []

    def list_item(self, tmdb_id, media):
        title, year, _ = self.by_id[tmdb_id]
        return {"id": tmdb_id, "title": title, "release_date": f"{year}-01-01", "vote_average": 8.0, "vote_count": 999}

    def details(self, tmdb_id, media):
        return {"runtime": self.by_id[tmdb_id][2]}


class _Plex:
    def collection_members(self, section_key, title):
        return []


def _answer(**overrides) -> str:
    body = {
        "name": "Twist Endings",
        "emoji": "🌀",
        "rules": {"max_runtime": None},
        "tags": ["twist ending", "nothing matches this"],
        "genres": ["Thriller", "Not A Genre"],
        "titles": [
            {"title": "Memento", "year": 2000, "reason": "Told backwards."},
            {"title": "Se7en", "year": 1995, "reason": "That box."},
        ],
    }
    return json.dumps({**body, **overrides})


def _author(answer: str, **kwargs):
    curator = kwargs.pop("curator", None) or _Curator(answer)
    index = {MediaType.MOVIE: {1: 11, 2: 12}, MediaType.SHOW: {}}
    draft = author_theme(
        brief=kwargs.pop("brief", BRIEF),
        media=MediaType.MOVIE,
        curator=curator,
        tmdb=_Tmdb(),
        plex=_Plex(),
        library_index=index,
        **kwargs,
    )
    return draft, curator


def _profile() -> UserProfile:
    return UserProfile(
        username="sarah_plex",
        plex_account_id=7,
        user_type=UserType.SHARED,
        nickname="Sarah Smith",
        history=[
            WatchedItem(title="Heat", media_type=MediaType.MOVIE, watched_at=__import__("datetime").datetime.now())
        ],
    )


class TestAuthorTheme:
    def test_author_theme_resolves_titles_tags_genres(self):
        draft, _ = _author("```json\n" + _answer() + "\n```")

        spec = draft.spec
        assert [(p.tmdb_id, p.origin, p.reason) for p in spec.picks] == [
            (1, "ai", "Told backwards."),
            (2, "ai", "That box."),
        ]
        assert spec.tags == (901,)
        assert spec.genres == ("Thriller",)
        assert (spec.name, spec.emoji, spec.collections) == ("Twist Endings", "🌀", ())
        assert (draft.stats.named, draft.stats.resolved, draft.stats.in_library, draft.stats.after_rules) == (
            2,
            2,
            2,
            2,
        )
        assert draft.tokens == 321
        assert draft.ai_reasons == {(MediaType.MOVIE, 1): "Told backwards.", (MediaType.MOVIE, 2): "That box."}

    def test_author_theme_tolerates_prose_around_the_json(self):
        draft, _ = _author("Sure! Here you go:\n" + _answer() + "\nEnjoy.")

        assert len(draft.spec.picks) == 2

    def test_author_theme_drops_unresolvable_titles_and_counts_them(self):
        titles = [{"title": "Memento", "year": 2000}, {"title": "Invented Film", "year": 2001}]

        draft, _ = _author(_answer(titles=titles))

        assert [p.tmdb_id for p in draft.spec.picks] == [1]
        assert (draft.stats.named, draft.stats.resolved) == (2, 1)

    def test_author_theme_enforces_runtime_rule_from_tmdb_not_from_ai(self):
        titles = [{"title": "Memento", "year": 2000}, {"title": "Short Cut", "year": 2010}]

        draft, _ = _author(_answer(titles=titles, rules={"max_runtime": 100}))

        assert draft.spec.rules == RowLimits(max_runtime=100)
        assert (draft.stats.resolved, draft.stats.after_rules) == (2, 1)
        assert draft.ai_reasons == {}

    def test_author_theme_sanitises_and_truncates_reason(self):
        reason = "**Bold** {brace}\nline [link](x) " + "word " * 80
        titles = [{"title": "Memento", "year": 2000, "reason": reason}]

        draft, _ = _author(_answer(titles=titles))

        shown = draft.spec.picks[0].reason
        assert shown is not None and len(shown) <= 160
        assert not any(ch in shown for ch in "*{}[]\n")

    def test_author_theme_sends_mechanics_and_brief(self):
        _, curator = _author(_answer())

        system, user = curator.calls[0]
        assert BUILD_SYSTEM_MECHANICS in system
        assert BRIEF in user

    def test_author_theme_owner_guidance_replaces_default_but_not_mechanics(self):
        _, curator = _author(_answer(), guidance="Only pick films with plot twists.")

        system, _ = curator.calls[0]
        assert system.startswith("Only pick films with plot twists.")
        assert BUILD_SYSTEM_MECHANICS in system

    def test_author_theme_never_sends_account_names(self):
        profile = _profile()

        _, curator = _author(_answer(), profile=profile)

        user = curator.calls[0][1]
        assert "Heat" in user
        assert profile.display_name not in user and profile.username not in user

    def test_shared_theme_sends_no_watch_history(self):
        _, curator = _author(_answer(), profile=None)

        assert "Recently watched" not in curator.calls[0][1]

    @pytest.mark.parametrize("answer", ["this is not json", "{broken", "[1, 2]"])
    def test_author_theme_raises_plain_error_on_invalid_json(self, answer):
        with pytest.raises(ThemeAuthorError) as error:
            _author(answer)

        assert answer not in str(error.value)

    def test_author_theme_raises_plain_error_on_empty(self):
        with pytest.raises(ThemeAuthorError, match="did not answer"):
            _author("  ")

    def test_author_theme_raises_plain_error_when_provider_none(self):
        class _None(_Curator):
            name = "none"
            can_complete = False

        none = _None("")
        with pytest.raises(ThemeAuthorError, match="AI provider"):
            _author("", curator=none)
        assert none.calls == []


class TestRefine:
    def test_refine_sends_current_theme_and_diff_lists_added_removed(self):
        current = ThemeSpec(
            slug="twists",
            name="Twist Endings",
            emoji=None,
            media=(MediaType.MOVIE,),
            tags=(),
            genres=(),
            excluded_genres=(),
            collections=(),
            picks=(ThemePick(1, MediaType.MOVIE, "ai", None), ThemePick(2, MediaType.MOVIE, "ai", None)),
            rules=RowLimits(),
            min_votes=None,
        )
        titles = [{"title": "Memento", "year": 2000}, {"title": "The Prestige", "year": 2006}]

        draft, curator = _author(_answer(titles=titles, rules={"min_year": 1999}), current=current, brief="newer")

        user = curator.calls[0][1]
        assert "newer" in user and "Se7en" in user and "Memento" in user
        assert draft.spec.slug == "twists"
        diff = diff_themes(current, draft.spec, draft.titles | {(MediaType.MOVIE, 2): "Se7en"})
        assert (diff.added, diff.removed, diff.rules_changed) == (["The Prestige"], ["Se7en"], True)
        assert (diff.added_count, diff.removed_count) == (1, 1)


class _CountingTmdb(_Tmdb):
    def __init__(self):
        super().__init__()
        self.searches = 0
        self.keyword_searches = 0

    def search(self, title, media, *, year=None):
        self.searches += 1
        return super().search(title, media, year=year)

    def search_keywords(self, query, limit=10):
        self.keyword_searches += 1
        return super().search_keywords(query, limit)


def _run(answer: str, tmdb=None, **kwargs):
    curator = kwargs.pop("curator", None) or _Curator(answer)
    draft = author_theme(
        brief=kwargs.pop("brief", BRIEF),
        media=MediaType.MOVIE,
        curator=curator,
        tmdb=tmdb or _Tmdb(),
        plex=_Plex(),
        library_index={MediaType.MOVIE: {}, MediaType.SHOW: {}},
        **kwargs,
    )
    return draft, curator


class TestCaps:
    def test_titles_are_capped_so_tmdb_is_searched_at_most_sixty_times(self):
        tmdb = _CountingTmdb()
        titles = [{"title": f"Film {n}", "year": 2000} for n in range(5000)]

        _run(_answer(titles=titles, tags=["twist ending"]), tmdb)

        assert tmdb.searches == 60

    def test_tags_are_capped_at_ten_searches(self):
        tmdb = _CountingTmdb()

        _run(_answer(tags=[f"tag {n}" for n in range(500)]), tmdb)

        assert tmdb.keyword_searches == 10

    def test_genres_are_capped_at_ten(self):
        names = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family"]
        names += ["Fantasy", "History", "Horror", "Music", "Mystery", "Romance"]

        draft, _ = _run(_answer(genres=names))

        assert draft.spec.genres == tuple(names[:10])

    def test_name_is_capped_at_sixty_characters(self):
        draft, _ = _run(_answer(name="N" * 500))

        assert len(draft.spec.name) == 60

    def test_title_and_tag_strings_are_capped_at_120_characters(self):
        seen = []

        class _Spy(_Tmdb):
            def search(self, title, media, *, year=None):
                seen.append(title)
                return super().search(title, media, year=year)

            def search_keywords(self, query, limit=10):
                seen.append(query)
                return []

        _run(_answer(titles=[{"title": "T" * 900}, {"title": "Memento"}], tags=["g" * 900]), _Spy())

        assert len(seen) == 3 and all(len(s) <= 120 for s in seen)

    def test_brief_is_wrapped_and_capped(self):
        _, curator = _run(_answer(), brief="b" * 5000)

        user = curator.calls[0][1]
        assert "<brief>" + "b" * 1000 + "</brief>" in user
        assert "b" * 1001 not in user


class TestFailures:
    def test_provider_exception_gives_plain_error_without_its_message(self):
        from loguru import logger

        class _Boom(_Curator):
            def complete(self, system, user):
                raise RuntimeError("bad key sk-secret")

        logs: list[str] = []
        sink = logger.add(logs.append)
        try:
            with pytest.raises(ThemeAuthorError) as error:
                _run("", curator=_Boom(""))
        finally:
            logger.remove(sink)

        assert "sk-secret" not in str(error.value) and "sk-secret" not in "".join(logs)
        assert "RuntimeError" in "".join(logs)
        assert str(error.value).startswith("The AI provider didn't respond")

    def test_nothing_resolved_raises_plain_error(self):
        with pytest.raises(ThemeAuthorError, match="didn't suggest anything"):
            _run(_answer(titles=[{"title": "Invented"}], tags=[], genres=[]))

    def test_wrong_shape_json_is_tolerated(self):
        draft, _ = _run(_answer(titles="Memento", rules=[1, 2], genres=["Thriller"]))

        assert draft.spec.picks == () and draft.spec.rules == RowLimits()

    def test_non_string_title_and_year_are_skipped(self):
        titles = [{"title": 5, "year": "x"}, {"title": "Memento", "year": "nineteen"}, "Se7en", None]

        draft, _ = _run(_answer(titles=titles))

        assert [p.tmdb_id for p in draft.spec.picks] == [1]

    def test_one_search_raising_drops_only_that_title(self):
        class _Flaky(_Tmdb):
            def search(self, title, media, *, year=None):
                if title == "Se7en":
                    raise RuntimeError("boom")
                return super().search(title, media, year=year)

        draft, _ = _run(_answer(), _Flaky())

        assert [p.tmdb_id for p in draft.spec.picks] == [1]

    def test_malformed_hit_drops_that_title(self):
        class _Bad(_Tmdb):
            def search(self, title, media, *, year=None):
                return {"title": title} if title == "Se7en" else super().search(title, media, year=year)

        draft, _ = _run(_answer(), _Bad())

        assert [p.tmdb_id for p in draft.spec.picks] == [1]


class TestRefineKeeps:
    def test_refine_keeps_owner_picks_and_collections_and_shows_tags(self):
        from shortlist.engine.themes import ThemeCollection

        collection = ThemeCollection("1", "My Favourites")
        current = ThemeSpec(
            slug="t",
            name="T",
            emoji=None,
            media=(MediaType.MOVIE,),
            tags=(777,),
            genres=(),
            excluded_genres=(),
            collections=(collection,),
            picks=(ThemePick(4, MediaType.MOVIE, "owner", None), ThemePick(2, MediaType.MOVIE, "ai", None)),
            rules=RowLimits(),
            min_votes=None,
        )

        draft, curator = _run(_answer(titles=[{"title": "Memento"}]), current=current)

        assert draft.spec.collections == (collection,)
        assert {(p.tmdb_id, p.origin) for p in draft.spec.picks} == {(1, "ai"), (4, "owner")}
        assert "777" in curator.calls[0][1]
        diff = diff_themes(current, draft.spec)
        assert "4" not in diff.added and "4" not in diff.removed
