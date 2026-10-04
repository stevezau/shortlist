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
        assert draft.ai_reasons == {"1": "Told backwards.", "2": "That box."}

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
