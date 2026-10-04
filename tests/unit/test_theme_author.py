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

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        self.calls.append((system, user))
        self.max_tokens = max_tokens
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
            {"media": "movie", "title": "Memento", "year": 2000, "reason": "Told backwards."},
            {"media": "movie", "title": "Se7en", "year": 1995, "reason": "That box."},
        ],
    }
    return json.dumps({**body, **overrides})


def _author(answer: str, **kwargs):
    curator = kwargs.pop("curator", None) or _Curator(answer)
    index = {MediaType.MOVIE: {1: 11, 2: 12, 4: 14}, MediaType.SHOW: {}}
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

    def test_author_theme_counts_the_ais_own_titles_apart_from_tag_matches(self):
        titles = [
            {"media": "movie", "title": "Memento", "year": 2000},
            {"media": "movie", "title": "The Prestige", "year": 2006},
        ]

        class _TagTmdb(_Tmdb):
            def discover_all(self, media, params):
                # The tag read lists Short Cut and Se7en, both on the server, besides the AI's own titles.
                return [self.list_item(4, media), self.list_item(2, media)] if "with_keywords" in params else []

        index = {MediaType.MOVIE: {1: 11, 2: 12, 4: 14}, MediaType.SHOW: {}}
        draft = author_theme(
            brief=BRIEF,
            media=MediaType.MOVIE,
            curator=_Curator(_answer(titles=titles)),
            tmdb=_TagTmdb(),
            plex=_Plex(),
            library_index=index,
        )

        # The Prestige (3) is not on the server; Memento is. Two more titles on the server match the tag.
        assert (draft.stats.named, draft.stats.resolved) == (2, 2)
        assert draft.stats.in_library == 3
        assert draft.stats.after_rules == 3
        assert draft.stats.ai_kept == 1

    def test_author_theme_does_not_count_an_ai_title_the_rules_drop_as_kept(self):
        titles = [
            {"media": "movie", "title": "Memento", "year": 2000},
            {"media": "movie", "title": "Short Cut", "year": 2010},
        ]

        draft, _ = _author(_answer(titles=titles, rules={"max_runtime": 100}))

        assert (draft.stats.resolved, draft.stats.after_rules, draft.stats.ai_kept) == (2, 1, 1)

    def test_author_theme_tolerates_prose_around_the_json(self):
        draft, _ = _author("Sure! Here you go:\n" + _answer() + "\nEnjoy.")

        assert len(draft.spec.picks) == 2

    def test_author_theme_drops_unresolvable_titles_and_counts_them(self):
        titles = [
            {"media": "movie", "title": "Memento", "year": 2000},
            {"media": "movie", "title": "Invented Film", "year": 2001},
        ]

        draft, _ = _author(_answer(titles=titles))

        assert [p.tmdb_id for p in draft.spec.picks] == [1]
        assert (draft.stats.named, draft.stats.resolved) == (2, 1)

    def test_author_theme_enforces_runtime_rule_from_tmdb_not_from_ai(self):
        titles = [
            {"media": "movie", "title": "Memento", "year": 2000},
            {"media": "movie", "title": "Short Cut", "year": 2010},
        ]

        draft, _ = _author(_answer(titles=titles, rules={"max_runtime": 100}))

        assert draft.spec.rules == RowLimits(max_runtime=100)
        assert (draft.stats.resolved, draft.stats.after_rules) == (2, 1)
        assert draft.ai_reasons == {}

    def test_author_theme_sanitises_and_truncates_reason(self):
        reason = "**Bold** {brace}\nline [link](x) " + "word " * 80
        titles = [{"media": "movie", "title": "Memento", "year": 2000, "reason": reason}]

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
        titles = [
            {"media": "movie", "title": "Memento", "year": 2000},
            {"media": "movie", "title": "The Prestige", "year": 2006},
        ]

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
        titles = [{"media": "movie", "title": f"Film {n}", "year": 2000} for n in range(5000)]

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

        _run(
            _answer(
                titles=[{"media": "movie", "title": "T" * 900}, {"media": "movie", "title": "Memento"}],
                tags=["g" * 900],
            ),
            _Spy(),
        )

        assert len(seen) == 3 and all(len(s) <= 120 for s in seen)

    def test_brief_is_wrapped_and_capped(self):
        _, curator = _run(_answer(), brief="b" * 5000)

        user = curator.calls[0][1]
        assert "<brief>" + "b" * 1000 + "</brief>" in user
        assert "b" * 1001 not in user


def _cut_off_reply(complete_titles: int = 30, cut: str = ' {"media": "movie", "title": "The Pre') -> str:
    """The shape of the live failure: a ```json fence that is never closed, ending mid-object."""
    done = ",".join(
        json.dumps({"media": "movie", "title": "Memento", "year": 2000, "reason": "Told {backwards}."})
        for _ in range(complete_titles)
    )
    return (
        '```json\n{"name": "Twist Endings", "emoji": "🌀", "rules": {"max_runtime": null}, '
        '"tags": [], "genres": ["Thriller"], "drop_tags": [], "drop_genres": [], "titles": [' + done + "," + cut
    )


class TestCutOffReply:
    def test_asks_for_a_long_reply(self):
        _, curator = _author(_answer())

        assert curator.max_tokens == 8000

    def test_a_reply_cut_inside_the_titles_keeps_the_complete_ones(self):
        draft, _ = _author(_cut_off_reply())

        assert draft.stats.truncated is True
        assert draft.stats.named == 30
        assert draft.spec.name == "Twist Endings"

    def test_a_whole_answer_is_not_flagged_truncated(self):
        draft, _ = _author(_answer())

        assert draft.stats.truncated is False

    def test_a_reply_cut_inside_the_name_gives_the_plain_error(self):
        with pytest.raises(ThemeAuthorError) as error:
            _author('```json\n{"name": "Twist End')

        assert "cut off or unreadable" in str(error.value)

    def test_a_reply_cut_inside_the_rules_gives_the_plain_error(self):
        with pytest.raises(ThemeAuthorError) as error:
            _author('```json\n{"name": "Twist Endings", "rules": {"max_runtime": 1')

        assert "cut off or unreadable" in str(error.value)

    def test_a_cut_before_any_title_completes_gives_the_plain_error(self):
        with pytest.raises(ThemeAuthorError) as error:
            _author(_cut_off_reply(complete_titles=0).replace('"titles": [,', '"titles": ['))

        assert "cut off or unreadable" in str(error.value)
        assert "titles" not in str(error.value)


class TestFailures:
    def test_provider_exception_gives_plain_error_without_its_message(self):
        from loguru import logger

        class _Boom(_Curator):
            def complete(self, system, user, **_):
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
            _run(_answer(titles=[{"media": "movie", "title": "Invented"}], tags=[], genres=[]))

    def test_wrong_shape_json_is_tolerated(self):
        draft, _ = _run(_answer(titles="Memento", rules=[1, 2], genres=["Thriller"]))

        assert draft.spec.picks == () and draft.spec.rules == RowLimits()

    def test_non_string_title_and_year_are_skipped(self):
        titles = [{"title": 5, "year": "x"}, {"media": "movie", "title": "Memento", "year": "nineteen"}, "Se7en", None]

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
    def test_refine_keeps_owner_picks_and_collections(self):
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

        draft, curator = _run(_answer(titles=[{"media": "movie", "title": "Memento"}]), current=current)

        assert draft.spec.collections == (collection,)
        assert {(p.tmdb_id, p.origin) for p in draft.spec.picks} == {(1, "ai"), (4, "owner")}
        assert "777" not in curator.calls[0][1]
        diff = diff_themes(current, draft.spec)
        assert "4" not in diff.added and "4" not in diff.removed


class TestRound2:
    @staticmethod
    def _current(**overrides) -> ThemeSpec:
        fields = {
            "slug": "t",
            "name": "T",
            "emoji": None,
            "media": (MediaType.MOVIE,),
            "tags": (777,),
            "genres": (),
            "excluded_genres": (),
            "collections": (),
            "picks": (ThemePick(4, MediaType.MOVIE, "owner", None), ThemePick(2, MediaType.MOVIE, "ai", None)),
            "rules": RowLimits(),
            "min_votes": None,
        }
        return ThemeSpec(**{**fields, **overrides})

    def test_current_tag_names_are_shown_and_ids_never(self):
        _, curator = _run(
            _answer(tags=[]), current=self._current(tags=(777, 778)), current_tag_names={777: "horror", 778: "slasher"}
        )

        user = curator.calls[0][1]
        assert '"tags": ["horror", "slasher"]' in user
        assert "777" not in user

    def test_refine_without_tag_names_omits_the_tags_line(self):
        _, curator = _run(_answer(), current=self._current())

        assert '"tags"' not in curator.calls[0][1]

    def test_refine_keeps_a_tag_the_ai_omits(self):
        draft, _ = _run(_answer(tags=["twist ending"]), current=self._current(), current_tag_names={777: "horror"})

        assert draft.spec.tags == (777, 901)

    def test_search_cap_holds_across_both_media(self):
        tmdb = _CountingTmdb()
        titles = [{"media": "movie", "title": f"Film {n}", "year": 2000} for n in range(5000)]

        author_theme(
            brief=BRIEF,
            media=(MediaType.MOVIE, MediaType.SHOW),
            curator=_Curator(_answer(titles=titles, tags=["twist ending"])),
            tmdb=tmdb,
            plex=_Plex(),
            library_index={MediaType.MOVIE: {}, MediaType.SHOW: {}},
        )

        assert tmdb.searches == 60

    def test_refine_resolving_nothing_new_keeps_owner_picks_without_raising(self):
        draft, _ = _run(
            _answer(titles=[{"media": "movie", "title": "Invented"}], tags=[], genres=[]), current=self._current()
        )

        assert [(p.tmdb_id, p.origin) for p in draft.spec.picks] == [(4, "owner")]

    def test_refine_resolving_nothing_new_keeps_a_collection_without_raising(self):
        from shortlist.engine.themes import ThemeCollection

        current = self._current(picks=(), collections=(ThemeCollection("1", "Mine"),))

        draft, _ = _run(_answer(titles=[], tags=[], genres=[]), current=current)

        assert draft.spec.collections == current.collections

    def test_diff_lists_owner_pick_title_as_unchanged(self):
        current = self._current()
        titles = {(MediaType.MOVIE, 4): "Short Cut", (MediaType.MOVIE, 2): "Se7en"}

        draft, _ = _run(_answer(titles=[{"media": "movie", "title": "Memento"}]), current=current)
        diff = diff_themes(current, draft.spec, draft.titles | titles)

        assert diff.unchanged == ["Short Cut"]
        assert diff.added == ["Memento"]
        assert diff.removed == ["Se7en"]


class _KindTmdb(_Tmdb):
    """Both kinds exist under one title, as "Severance" does: a 2006 film and a 2022 show."""

    def __init__(self):
        super().__init__()
        self.searched: list[tuple[str, MediaType]] = []

    def search(self, title, media, *, year=None):
        self.searched.append((title, media))
        if title == "Severance":
            return {"id": 50 if media is MediaType.MOVIE else 51, "title": title}
        return super().search(title, media, year=year)

    def list_item(self, tmdb_id, media):
        if tmdb_id in (50, 51):
            return {"id": tmdb_id, "title": "Severance", "release_date": "2022-02-18", "vote_count": 999}
        return super().list_item(tmdb_id, media)


class TestEachTitleSaysWhatKindItIs:
    @staticmethod
    def _both(titles: list, tmdb: _Tmdb):
        return author_theme(
            brief=BRIEF,
            media=(MediaType.MOVIE, MediaType.SHOW),
            curator=_Curator(_answer(titles=titles, tags=[])),
            tmdb=tmdb,
            plex=_Plex(),
            library_index={MediaType.MOVIE: {}, MediaType.SHOW: {}},
        )

    def test_a_show_is_looked_up_only_among_shows(self):
        tmdb = _KindTmdb()

        draft = self._both([{"title": "Severance", "year": 2022, "media": "show"}], tmdb)

        assert [(p.tmdb_id, p.media) for p in draft.spec.picks] == [(51, MediaType.SHOW)]
        assert tmdb.searched == [("Severance", MediaType.SHOW)]

    def test_a_title_with_no_kind_or_a_bad_one_is_skipped_and_counted_as_unresolved(self):
        titles = [
            {"title": "Severance", "year": 2022},
            {"title": "Memento", "year": 2000, "media": "film"},
            {"title": "Se7en", "year": 1995, "media": "movie"},
        ]
        tmdb = _KindTmdb()

        draft = self._both(titles, tmdb)

        assert [p.tmdb_id for p in draft.spec.picks] == [2]
        assert (draft.stats.named, draft.stats.resolved) == (3, 1)
        assert tmdb.searched == [("Se7en", MediaType.MOVIE)]

    def test_a_kind_the_row_does_not_cover_is_skipped(self):
        tmdb = _KindTmdb()

        draft, _ = _run(
            _answer(titles=[{"title": "Severance", "media": "show"}, {"title": "Memento", "media": "movie"}]), tmdb
        )

        assert [p.tmdb_id for p in draft.spec.picks] == [1]
        assert tmdb.searched == [("Memento", MediaType.MOVIE)]


class TestAChangeKeepsTheDescription:
    def test_the_brief_is_context_and_the_change_is_the_instruction(self):
        current = TestRound2._current()

        draft, curator = _run(_answer(), current=current, brief="films with a twist", change="less gore")

        user = curator.calls[0][1]
        assert "<brief>films with a twist</brief>" in user and "<change>less gore</change>" in user
        assert draft.brief == "films with a twist"

    def test_a_change_is_ignored_for_a_new_theme(self):
        _, curator = _run(_answer(), change="less gore")

        assert "less gore" not in curator.calls[0][1]


class TestARefinementKeepsWhatItHas:
    @staticmethod
    def _current(**overrides) -> ThemeSpec:
        return TestRound2._current(tags=(777, 778), genres=("Horror", "Comedy"), **overrides)

    def test_tags_and_genres_the_ai_does_not_repeat_carry_over_including_hand_added_ones(self):
        draft, _ = _run(
            _answer(tags=["twist ending"], genres=["Thriller"]),
            current=self._current(),
            current_tag_names={777: "horror", 778: "slasher"},
        )

        assert draft.spec.tags == (777, 778, 901)
        assert draft.spec.genres == ("Horror", "Comedy", "Thriller")

    def test_only_what_the_ai_names_in_drop_lists_leaves(self):
        draft, _ = _run(
            _answer(tags=[], genres=[], drop_tags=["Slasher", "never had this"], drop_genres=["comedy"]),
            current=self._current(),
            current_tag_names={777: "horror", 778: "slasher"},
        )

        assert draft.spec.tags == (777,)
        assert draft.spec.genres == ("Horror",)

    def test_drop_lists_are_capped_and_ignored_outside_a_refinement(self):
        draft, _ = _run(_answer(tags=["twist ending"], drop_tags=["twist ending"], drop_genres=["thriller"]))

        assert draft.spec.tags == (901,) and draft.spec.genres == ("Thriller",)

    def test_the_diff_reports_tag_and_genre_changes_and_title_counts(self):
        current = self._current()
        names = {777: "horror", 778: "slasher", 901: "twist ending"}
        draft, _ = _run(
            _answer(tags=["twist ending"], genres=["Thriller"], drop_tags=["slasher"], drop_genres=["comedy"]),
            current=current,
            current_tag_names=names,
        )

        diff = diff_themes(current, draft.spec, draft.titles, names)

        assert diff.tags_added == ["twist ending"] and diff.tags_removed == ["slasher"]
        assert diff.genres_added == ["Thriller"] and diff.genres_removed == ["Comedy"]
        assert (diff.before_count, diff.after_count) == (2, 3)


class TestTheAisLimitsAreBounded:
    @pytest.mark.parametrize(
        ("rules", "expected"),
        [
            ({"min_rating": 80}, RowLimits()),
            ({"min_rating": -1}, RowLimits()),
            ({"min_rating": 7.5}, RowLimits(min_rating=7.5)),
            ({"min_year": 2020, "max_year": 1990}, RowLimits()),
            ({"min_year": 1990, "max_year": 2020}, RowLimits(min_year=1990, max_year=2020)),
            ({"min_year": 3, "max_year": 99999}, RowLimits()),
            ({"max_runtime": 0}, RowLimits()),
            ({"max_runtime": -90}, RowLimits()),
            ({"max_runtime": 100000}, RowLimits(max_runtime=600)),
        ],
    )
    def test_out_of_range_values_are_dropped_or_capped(self, rules, expected):
        draft, _ = _run(_answer(rules=rules))

        assert draft.spec.rules == expected

    def test_the_preview_view_of_such_an_answer_is_a_valid_response(self):
        """A rating of 80 once failed the response model AFTER the tokens were spent: a 500 on a paid call."""
        from shortlist.server.api import themes as themes_api

        draft, _ = _run(_answer(rules={"min_rating": 80, "min_year": 2020, "max_year": 1990, "max_runtime": 90}))

        view = themes_api._spec_view(draft.spec, brief="x", origin="ai", titles=draft.titles, tag_names={})
        themes_api.ThemeOut(**view)

    def test_the_prompt_states_the_rating_scale(self):
        assert "0 to 10" in BUILD_SYSTEM_MECHANICS


class TestNonFiniteNumbersFromTheAi:
    def test_infinity_nan_and_overflow_are_ignored_and_the_draft_builds(self):
        raw = (
            _answer()
            .replace(
                '"rules": {"max_runtime": null}',
                '"rules": {"max_runtime": Infinity, "min_rating": NaN, "min_year": 1e999, "max_year": -Infinity}',
            )
            .replace('"year": 2000', '"year": Infinity')
        )

        draft, _ = _run(raw)

        assert draft.spec.rules == RowLimits()
        assert draft.spec.picks


class TestCarryOverTrimsCarriedItemsFirst:
    def test_ai_additions_survive_when_the_union_exceeds_the_cap(self):
        current = TestRound2._current(tags=tuple(range(1000, 1020)), genres=("Comedy",))
        draft, _ = _run(_answer(tags=["twist ending"], genres=["Thriller"]), current=current)

        assert len(draft.spec.tags) == 20
        assert draft.spec.tags[-1] == 901
        assert draft.spec.tags[:19] == tuple(range(1000, 1019))

    def test_genre_additions_survive_the_genre_cap(self):
        carried = ("Horror", "Comedy", "Drama", "Western", "Romance", "Mystery", "Family", "Music", "War", "Crime")
        current = TestRound2._current(genres=carried)
        draft, _ = _run(_answer(tags=[], genres=["Thriller"]), current=current)

        assert len(draft.spec.genres) == 10
        assert draft.spec.genres[-1] == "Thriller"


class TestRuntimeCheckCounts:
    def test_a_bounded_preview_reports_how_many_running_times_it_checked(self):
        draft, _ = _author(_answer(rules={"max_runtime": 600}), max_details=1)

        assert (draft.stats.runtime_total, draft.stats.runtime_checked) == (2, 1)

    def test_an_unbounded_run_checks_them_all(self):
        draft, _ = _author(_answer(rules={"max_runtime": 600}))

        assert (draft.stats.runtime_total, draft.stats.runtime_checked) == (2, 2)


class TestAThemeCoversOnlyTheKindsItNamed:
    """A both-media row must not fill a library with tag filler when the AI named nothing for it."""

    BOTH = (MediaType.MOVIE, MediaType.SHOW)
    MOVIE = (("title", "Memento"), ("media", "movie"))
    SHOW = (("title", "Severance"), ("media", "show"))

    @staticmethod
    def _both(titles: list, *, tags: list | None = None, current: ThemeSpec | None = None):
        return author_theme(
            brief=BRIEF,
            media=TestAThemeCoversOnlyTheKindsItNamed.BOTH,
            curator=_Curator(_answer(titles=titles, tags=[] if tags is None else tags)),
            tmdb=_KindTmdb(),
            plex=_Plex(),
            library_index={MediaType.MOVIE: {1: 11}, MediaType.SHOW: {51: 61}},
            current=current,
        )

    def test_only_movies_named_covers_movies_only(self):
        draft = self._both([dict(self.MOVIE)], tags=["twist ending"])

        assert draft.spec.media == (MediaType.MOVIE,)

    def test_only_shows_named_covers_shows_only(self):
        draft = self._both([dict(self.SHOW)], tags=["twist ending"])

        assert draft.spec.media == (MediaType.SHOW,)

    def test_both_kinds_named_covers_both(self):
        draft = self._both([dict(self.MOVIE), dict(self.SHOW)])

        assert draft.spec.media == self.BOTH

    def test_no_titles_named_keeps_the_requested_media(self):
        draft = self._both([], tags=["twist ending"])

        assert draft.spec.media == self.BOTH

    def test_a_kept_owner_show_keeps_shows_covered_when_the_ai_names_only_movies(self):
        current = ThemeSpec(
            slug="t",
            name="T",
            emoji=None,
            media=self.BOTH,
            tags=(),
            genres=(),
            excluded_genres=(),
            collections=(),
            picks=(ThemePick(51, MediaType.SHOW, "owner", None),),
            rules=RowLimits(),
            min_votes=None,
        )

        draft = self._both([dict(self.MOVIE)], current=current)

        assert draft.spec.media == self.BOTH

    def test_the_preview_counts_only_the_covered_kind(self):
        draft = self._both([dict(self.MOVIE)], tags=["twist ending"])

        assert draft.stats.ai_kept == 1
        assert draft.stats.in_library == 1
