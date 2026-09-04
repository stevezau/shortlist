from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients.plex_pms import CollectionRejectedItems, PlexClient
from shortlist.engine.delivery import DEFAULT_ROW_NAME, deliver_rows, render_row_name, row_marker, sweep_broken_rows
from shortlist.engine.models import LABEL_PREFIX, EngineConfig, MediaType, Pick
from tests.conftest import make_profile


def picks(n: int = 2, media_type: MediaType = MediaType.MOVIE, start: int = 1) -> list[Pick]:
    kind = "Movie" if media_type is MediaType.MOVIE else "Show"
    return [
        Pick(
            tmdb_id=i,
            rating_key=1000 + i,
            title=f"{kind} {i}",
            rank=i,
            reason="Because you watched Fargo",
            media_type=media_type,
            seed_title="Fargo",
            seed_tmdb_id=900,
        )
        for i in range(start, start + n)
    ]


def _section(title: str, kind: str, key: int) -> MagicMock:
    section = MagicMock()
    section.title = title
    section.type = kind
    section.key = key  # sections are matched by key, never by object identity
    return section


def test_target_sections_defaults_to_all_then_narrows_by_media_and_keys():
    from shortlist.engine.delivery import target_sections
    from shortlist.engine.models import RowSpec

    movies = _section("Movies", "movie", "1")
    movies4k = _section("4K Movies", "movie", "3")
    shows = _section("TV Shows", "show", "2")
    secs = [movies, shows, movies4k]

    def spec(**kw):
        return RowSpec(slug="p", name_template="", size=5, **kw)

    assert target_sections(secs, spec()) == [movies, shows, movies4k]  # empty -> every library
    assert target_sections(secs, spec(media="movie")) == [movies, movies4k]  # type filter
    assert target_sections(secs, spec(library_keys=["3"])) == [movies4k]  # a specific library
    assert target_sections(secs, spec(library_keys=["9"])) == []  # a key that no longer exists


@pytest.fixture
def movies() -> MagicMock:
    return _section("Movies", "movie", 1)


@pytest.fixture
def shows() -> MagicMock:
    return _section("TV Shows", "show", 2)


def _named_pick(seed_title: str | None) -> Pick:
    return Pick(
        tmdb_id=1, rating_key=1, title="Movie", rank=1, reason="r", media_type=MediaType.MOVIE, seed_title=seed_title
    )


class TestRenderRowName:
    def test_top_seed_substitution(self):
        assert render_row_name("Because you watched {top_seed}", make_profile(), picks()) == "Because you watched Fargo"

    def test_unfillable_template_yields_no_name_at_all(self):
        """ "" is the answer, and the caller must read it as "do not build this row for them".

        It used to answer DEFAULT_ROW_NAME — a hardcoded English string that ignored the operator's
        own row-name setting and claimed a watch that never happened. Issue #84: on a 22-user server
        with a French template, that put "✨ Picked for You" on 19 people's Plex.
        """
        cold = [Pick(1, 1, "X", 1, "r", MediaType.MOVIE)]
        assert render_row_name("{top_seed}", make_profile(), cold) == ""
        assert render_row_name("{top_seed}", make_profile(), cold) != DEFAULT_ROW_NAME

    def test_library_name_substitution_fills_the_delivering_library(self):
        tpl = "✨ {library_name} Picked for You"
        assert render_row_name(tpl, make_profile(), picks(), library_name="Movies") == "✨ Movies Picked for You"
        assert render_row_name(tpl, make_profile(), picks(), library_name="TV Shows") == "✨ TV Shows Picked for You"

    def test_library_name_with_no_library_collapses_to_the_generic_default(self):
        # A preview or a row-level summary has no single library, so the empty placeholder is collapsed
        # away rather than leaving a double space — and lands exactly on the generic default title.
        tpl = "✨ {library_name} Picked for You"
        assert render_row_name(tpl, make_profile(), picks(), library_name="") == DEFAULT_ROW_NAME
        assert render_row_name(tpl, make_profile(), picks()) == "✨ Picked for You"

    def test_a_template_without_the_placeholder_keeps_its_exact_spacing(self):
        # Non-{library_name} templates take the untouched .strip() path — spacing is preserved byte-for-byte.
        assert render_row_name("✨  Custom  Row", make_profile(), picks(), library_name="Movies") == "✨  Custom  Row"


class TestColdStartRowName:
    """A cold-start user has no seed — the row must not read 'Because you watched'."""

    def test_seeded_user_gets_the_dynamic_title(self):
        name = render_row_name("Because you watched {top_seed}", make_profile(), [_named_pick("Fargo")])
        assert name == "Because you watched Fargo"

    def test_cold_start_user_gets_no_name_rather_than_a_dangling_one_or_an_invented_one(self):
        # Neither "Because you watched" (a sentence that stops halfway) nor a substitute of our own.
        assert render_row_name("Because you watched {top_seed}", make_profile(), [_named_pick(None)]) == ""
        assert render_row_name("Because you watched {top_seed}", make_profile(), []) == ""

    def test_the_operators_own_fallback_is_used_when_they_have_given_one(self):
        # The whole matrix of the naming rule: own template -> operator's fallback -> nothing.
        cold = [_named_pick(None)]
        profile = make_profile()

        assert (
            render_row_name("Car vous avez regardé {top_seed}", profile, cold, fallback_name="Spécifiquement pour vous")
            == "Spécifiquement pour vous"
        )
        # Their fallback still gets its own placeholders filled.
        assert (
            render_row_name("{top_seed}", profile, cold, library_name="Films", fallback_name="{library_name} pour vous")
            == "Films pour vous"
        )
        # A fallback that ALSO needs a seed is no fallback at all — and must not dangle either.
        assert render_row_name("{top_seed}", profile, cold, fallback_name="Parce que {top_seed}") == ""
        # A seed exists: the row's own name wins and the fallback is never consulted.
        assert (
            render_row_name("Because you watched {top_seed}", profile, [_named_pick("Fargo")], fallback_name="Other")
            == "Because you watched Fargo"
        )

    def test_static_template_is_untouched(self):
        assert render_row_name("✨ Picked for You", make_profile(), [_named_pick(None)]) == "✨ Picked for You"

    def test_both_libraries_of_one_row_get_the_same_seeded_name(self, engine_config, movies, shows):
        """Issue #84's real mechanism, from the reporter's own screenshots.

        A `movies & shows` row named "Car vous avez regardé {top_seed}" produced TWO differently
        titled collections for one person: the seeded name in Movies, and the bare English default in
        TV — because the title was rendered from THAT LIBRARY's picks, and their seeds were all films.
        `{top_seed}` names something the person WATCHED; what they watched is not confined to the
        library a pick happens to live in.
        """
        from shortlist.engine.delivery import deliver_rows, strip_marker
        from shortlist.engine.models import RowSpec

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.return_value = []
        seeded_movie = Pick(1, 101, "Sicario", rank=1, reason="r", media_type=MediaType.MOVIE, seed_title="Conjuring")
        unseeded_show = Pick(2, 202, "The Bear", rank=2, reason="r", media_type=MediaType.SHOW, seed_title=None)

        deliver_rows(
            plex,
            make_profile(),
            [seeded_movie, unseeded_show],
            engine_config,
            RowSpec(slug="because", name_template="Car vous avez regardé {top_seed}", size=10, media="both"),
            sections=[movies, shows],
            section_picks={movies.key: [seeded_movie], shows.key: [unseeded_show]},
            dry_run=False,
        )

        created = [strip_marker(call.args[1]) for call in plex.create_collection.call_args_list]
        assert created == ["Car vous avez regardé Conjuring"] * 2, (
            f"one row must have ONE name in every library it lands in, got {created}"
        )
        assert DEFAULT_ROW_NAME not in created, "the TV library must not fall back while the row has a seed"

    def test_the_seed_source_rule_has_exactly_one_implementation(self):
        """`seed_source` is the whole cross-module contract, so cover its matrix here.

        `delivery._deliver_one` renders the title Plex is given and `rows._run_user` re-renders it to
        stamp `placement_titles`, which is how promote finds the collection delivery just wrote. Two
        copies of this rule that drift by one character mean promote looks up a title that was never
        written. Both now call THIS, so the only thing that can be wrong is the rule itself.
        """
        from shortlist.engine.delivery import seed_source

        seeded_here = [_named_pick("Fargo")]
        seeded_elsewhere = [_named_pick("Heat")]
        seedless = [_named_pick(None)]

        # Its own seed wins — a row over two libraries follows a different watch in each, on purpose.
        assert seed_source(seeded_here, seeded_elsewhere) is seeded_here
        # No seed here: borrow the row's, rather than give up and use the default name (#84).
        assert seed_source(seedless, seeded_elsewhere) is seeded_elsewhere
        # Nothing anywhere: hand back the row's list and let render_row_name fall back as before.
        assert seed_source(seedless, seedless) is seedless
        assert seed_source([], []) == []

    def test_an_unseeded_top_pick_does_not_hide_the_seeds_behind_it(self):
        """Issue #84, the half that made this happen to everyone.

        `{top_seed}` used to read the single best pick and use its seed "if it had one". Sources that
        seed nothing — trending, popular-on-this-server, a web-search suggestion — routinely rank
        first, and the row then fell back to the default title as though the person had no history at
        all. The reporter saw it on every account on their server, including ones with years of it.
        """
        picks_with_unseeded_leader = [
            Pick(1, 1, "Trending Thing", rank=1, reason="r", media_type=MediaType.MOVIE, seed_title=None),
            Pick(2, 2, "Sicario", rank=2, reason="r", media_type=MediaType.MOVIE, seed_title="Wind River"),
            Pick(3, 3, "Hell or High Water", rank=3, reason="r", media_type=MediaType.MOVIE, seed_title="Fargo"),
        ]

        name = render_row_name("Because you watched {top_seed}", make_profile(), picks_with_unseeded_leader)

        # The BEST SEEDED pick (rank 2), not the best pick overall and not the first in the list.
        assert name == "Because you watched Wind River"


class TestAnUnnameableRowTouchesNothing:
    """The row that cannot be named must leave no trace — least of all in the privacy machinery."""

    def test_it_does_not_blank_the_label_a_real_row_recorded(self, engine_config: EngineConfig, movies):
        """`stored_labels` is keyed by PERSON, shared by every one of their rows.

        So an unnameable row writing "" there erases the label a nameable row just recorded — and
        `desired_excludes` merges that empty string into every OTHER account's share filter as
        `label!=Shortlist_bob,,Shortlist_mike`. Malformed, and while it stands there is no exclude at
        all for that person's row. Fires on the default configuration: a `{top_seed}` row with no
        fallback and a user with picks but no seed.
        """
        from shortlist.engine.delivery import deliver_rows
        from shortlist.engine.models import RowSpec

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        plex.sections.return_value = [movies]
        plex.find_owned_collections.return_value = []
        seeded = Pick(1, 101, "A", rank=1, reason="r", media_type=MediaType.MOVIE, seed_title="Fargo")
        unseeded = Pick(2, 102, "B", rank=1, reason="r", media_type=MediaType.MOVIE, seed_title=None)
        stored_labels: dict[str, str] = {}

        deliver_rows(
            plex,
            make_profile(),
            [seeded],
            engine_config,
            RowSpec(slug="named", name_template="Because you watched {top_seed}", size=5, media="movie"),
            sections=[movies],
            section_picks={movies.key: [seeded]},
            stored_labels=stored_labels,
            dry_run=False,
        )
        recorded = dict(stored_labels)
        assert recorded, "the nameable row must record its label"

        deliver_rows(
            plex,
            make_profile(),
            [unseeded],
            engine_config,
            RowSpec(slug="nameless", name_template="Car vous avez regardé {top_seed}", size=5, media="movie"),
            sections=[movies],
            section_picks={movies.key: [unseeded]},
            stored_labels=stored_labels,
            dry_run=False,
        )

        assert stored_labels == recorded, (
            f"a row that wrote nothing must not touch the label accumulator, got {stored_labels}"
        )
        assert "" not in stored_labels.values()


def _labelling_plex_mock(plex: MagicMock) -> MagicMock:
    """Make `stored_label` leave the label ON the collection, as the real one does, and give
    `fetch_items` the `(items, missing)` shape the real client returns.

    `fetch_items` reports what Plex still HAS and what has GONE, because a partial batch omits dead
    keys silently — a mock returning a bare list would let delivery claim it delivered titles the row
    does not contain. Default: nothing missing.

    Not decoration: `_apply_shortlist_label` refuses to write unless the owner label is already in
    `collection.labels`, because plexapi's addLabel PUTs an ABSOLUTE tag set built from that list —
    so writing against an empty one would DELETE the owner label and un-hide the row. A mock that
    returned a string without touching the object would report that guard as broken, and (worse) a
    mock that ignored the guard entirely would let a regression through. Testing rule: the fake must
    be no easier than the real server.
    """

    # `fetch_items` returns (items, missing): a partial batch omits dead keys silently, so delivery
    # has to be told what went. A bare MagicMock ITERATES EMPTY rather than raising, so unpacking it
    # would quietly yield nothing — the mock must carry the real shape.
    plex.fetch_items.return_value = ([], [])

    def stored_label(collection, label):
        stored = label.replace("shortlist", "Shortlist", 1)
        current = list(getattr(collection, "_labels", []))
        if not any(t.tag.lower() == label.lower() for t in current):
            current.append(SimpleNamespace(tag=stored))
        collection._labels = current
        collection.labels = current
        return stored

    plex.stored_label.side_effect = stored_label
    original_create = plex.create_collection.side_effect

    def create(section, title, items):
        # Default to the mock's own return_value, not a fresh MagicMock — tests assert identity
        # against `plex.create_collection.return_value`.
        collection = original_create(section, title, items) if original_create else plex.create_collection.return_value
        collection._labels = []
        collection.labels = []
        return collection

    plex.create_collection.side_effect = create
    return plex


class TestDeliverRows:
    """Delivery is split by media type because Plex collections belong to exactly one library.

    The matrix that matters is the pick mix: movies only, shows only, both, and neither — the
    "both" and "neither" cells are the ones that leaked on a live server.
    """

    def _plex(self, movies: MagicMock, shows: MagicMock) -> MagicMock:
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies, shows]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies, MediaType.SHOW: shows}
        plex.find_owned_collections.return_value = []
        plex.matches_section.return_value = True
        plex.fetch_items.return_value = ([], [])
        return _labelling_plex_mock(plex)

    def test_library_keys_target_one_library_and_remap_its_rating_keys(self, engine_config: EngineConfig):
        from shortlist.engine.models import RowSpec

        # Two movie libraries; the SAME titles have different ratingKeys in each. A row pinned to
        # "4K Movies" must build only there, with 4K's ratingKeys — not the "Movies" ones.
        movies = _section("Movies", "movie", "1")
        movies4k = _section("4K Movies", "movie", "3")
        shows = _section("TV Shows", "show", "2")
        plex = self._plex(movies, shows)
        section_index = {"1": {1: 1001, 2: 1002}, "3": {1: 4001, 2: 4002}, "2": {}}
        spec = RowSpec(slug="gems", name_template="Gems", size=5, library_keys=["3"])

        deliver_rows(
            plex,
            make_profile(),
            picks(),
            engine_config,
            spec,
            sections=[movies, shows, movies4k],
            section_index=section_index,
        )

        assert plex.create_collection.call_args.args[0] is movies4k  # only the 4K library
        plex.fetch_items.assert_called_once_with([4001, 4002])  # 4K ratingKeys, not [1001, 1002]

    def test_creates_collection_when_missing(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)

        diff, stored = deliver_rows(plex, make_profile(), picks(), engine_config)

        assert diff.created is True
        assert diff.added == ["Movie 1", "Movie 2"]
        assert stored == "Shortlist_sarah"
        plex.fetch_items.assert_called_once_with([1001, 1002])
        create = plex.create_collection.call_args
        assert create.args[0] is movies
        # The title Plex is given carries an INVISIBLE per-account marker. Without it every user's
        # row is the same collection tag in that library, holding everyone's picks. The default
        # template fills {library_name} from the delivering library ("Movies" here).
        assert create.args[1] == "✨ Movies Picked for You" + row_marker(make_profile().plex_account_id)
        assert create.args[1].startswith("✨ Movies Picked for You"), "what a human reads is a clean title"
        # The row-level report title renders library-less (no single library) -> the generic default.
        assert diff.collection_title == "✨ Picked for You"
        # Two labels per collection: the OWNER label everything keys off, and the constant one a
        # co-managing tool can be pointed at (ours are per person, so a 46-account server otherwise
        # needs 46 entries in agregarr's exclusion list, going stale on every roster change).
        assert [c.args[1] for c in plex.stored_label.call_args_list] == [
            "shortlist_sarah",
            "shortlist",
        ]
        # Promotion is the pipeline's job, AFTER filters are merged — never delivery's.
        plex.promote.assert_not_called()

    def test_show_picks_go_to_the_tv_library_not_the_movie_one(self, engine_config: EngineConfig, movies, shows):
        """A show delivered into a movie collection is matched by neither filterMovies nor
        filterTelevision, so its label exclude does nothing and the row leaks to every user."""
        plex = self._plex(movies, shows)

        deliver_rows(plex, make_profile(), picks(media_type=MediaType.SHOW), engine_config)

        assert plex.create_collection.call_args.args[0] is shows

    def test_mixed_picks_are_split_into_one_collection_per_library(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)
        mixed = picks(2, MediaType.MOVIE) + picks(3, MediaType.SHOW, start=5)

        diff, _ = deliver_rows(plex, make_profile(), mixed, engine_config)

        sections_written = [call.args[0] for call in plex.create_collection.call_args_list]
        assert sections_written == [movies, shows]
        # each collection gets ONLY its own type — never the whole pick list
        assert plex.fetch_items.call_args_list[0].args[0] == [1001, 1002]
        assert plex.fetch_items.call_args_list[1].args[0] == [1005, 1006, 1007]
        assert sorted(diff.added) == ["Movie 1", "Movie 2", "Show 5", "Show 6", "Show 7"]
        # Both collections carry the SAME owner label — that one label is what every other user's
        # share filter excludes, so a DIFFERENT one per row would leave one of the two visible. (The
        # constant `shortlist` label is on both too; it excludes nothing and is filtered out here.)
        owner_labels = [c.args[1] for c in plex.stored_label.call_args_list if c.args[1] != "shortlist"]
        assert owner_labels == ["shortlist_sarah", "shortlist_sarah"]

    def test_a_library_with_no_picks_keeps_its_existing_row(self, engine_config: EngineConfig, movies, shows):
        """A row nobody wrote to this run is stale, NOT leaking: it still carries its label, so
        every other user's `label!=` exclude still hides it.

        Deleting it would mean one bad night upstream — a TMDB 404 on a show id, a lopsided
        candidate pool — destroys an established row. The user simply gets no show picks tonight.
        """
        untouched = MagicMock()
        untouched.title = "✨ Picked for You"
        plex = self._plex(movies, shows)
        plex.find_owned_collections.side_effect = lambda section, label: [untouched] if section is shows else []

        diff, _ = deliver_rows(plex, make_profile(), picks(media_type=MediaType.MOVIE), engine_config)

        plex.delete_owned_collection.assert_not_called()
        assert diff.deleted == []

    def test_no_stale_row_means_nothing_is_deleted(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)

        diff, _ = deliver_rows(plex, make_profile(), picks(), engine_config)

        plex.delete_owned_collection.assert_not_called()
        assert diff.deleted == []

    def test_updates_existing_collection_found_by_label_not_title(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = MagicMock()
        # A row already in the current format: a dynamic template renamed it, but it still carries
        # this account's marker, so its membership is its own and it can be updated in place.
        existing.title = "Old Name" + row_marker(profile.plex_account_id)
        # Movie 1 (1001) is already present; Stale Movie (1003) will be removed. picks() = 1001, 1002.
        existing.items.return_value = [
            MagicMock(title="Movie 1", ratingKey=1001),
            MagicMock(title="Stale Movie", ratingKey=1003),
        ]
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        diff, _ = deliver_rows(plex, profile, picks(), engine_config)

        assert diff.created is False
        assert diff.added == ["Movie 2"]
        assert diff.removed == ["Stale Movie"]
        assert diff.kept == ["Movie 1"]
        plex.delete_owned_collection.assert_not_called()  # its tag is not shared: no rebuild needed
        existing.editTitle.assert_called_once_with("✨ Movies Picked for You" + row_marker(profile.plex_account_id))
        # Only the DELTA is fetched — 1001 is already in the collection, so just 1002 (Movie 2).
        plex.fetch_items.assert_called_once_with([1002])
        # set_items gets the pre-read membership, the add-delta, and the full ranked key order.
        assert plex.set_items.call_args.args == (
            existing,
            existing.items.return_value,
            plex.fetch_items.return_value[0],
            [1001, 1002],
        )
        existing.items.assert_called_once()  # membership read exactly once, not twice

    def test_a_vanished_pick_does_not_erase_a_live_pick_that_shares_its_title(
        self, engine_config: EngineConfig, movies, shows
    ):
        """Release review, 2026-08-18 (LOW). On the UPDATE path the vanished filter matched on TITLE
        while everything around it diffs by ratingKey. Two picks can legitimately share a title — a
        remake, or a film and its 4K edition surfacing under one name — so dropping 'Movie 1' because
        one of them vanished also erased the copy Plex still holds, and `titles_added` in the run
        stats inherited it. Same "the audit disagrees with the row" fault the block exists to
        prevent (plex-safety rule 10), inverted."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = MagicMock()
        existing.title = "Old Name" + row_marker(profile.plex_account_id)
        existing.items.return_value = []  # empty row: both picks are in the add-delta
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        twins = [
            Pick(1, 1001, "Dune", rank=1, reason="r", media_type=MediaType.MOVIE),
            Pick(2, 1002, "Dune", rank=2, reason="r", media_type=MediaType.MOVIE),
        ]
        survivor = MagicMock(title="Dune", ratingKey=1001)
        plex.fetch_items.return_value = ([survivor], [1002])  # 1002 deleted from Plex since the pick

        diff, _ = deliver_rows(plex, profile, twins, engine_config)

        assert diff.added == ["Dune"], (
            f"the surviving copy must still be reported as added, got {diff.added!r} — "
            "one vanished key erased both because the filter matched on title"
        )

    def test_unchanged_row_makes_no_membership_write(self, engine_config, movies, shows):
        """A row already holding exactly the wanted picks writes NOTHING — no add/remove/sortUpdate.
        It used to fire a sortUpdate every run (a real write on a slow library, for nothing)."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = MagicMock()
        existing.title = "✨ Movies Picked for You" + row_marker(profile.plex_account_id)
        # Membership already IS the wanted set (picks() = 1001, 1002).
        existing.items.return_value = [
            MagicMock(title="Movie 1", ratingKey=1001),
            MagicMock(title="Movie 2", ratingKey=1002),
        ]
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        order_work: list = []

        diff, stored = deliver_rows(plex, profile, picks(), engine_config, order_work=order_work)

        plex.set_items.assert_not_called()  # no add / remove / sortUpdate
        plex.fetch_items.assert_not_called()  # nothing new to fetch
        existing.editTitle.assert_not_called()  # title already matches
        plex.delete_owned_collection.assert_not_called()  # not a rebuild
        assert (existing, [1001, 1002]) in order_work  # still queued so a freshness re-rank applies
        assert diff.added == [] and diff.removed == []
        assert stored == "Shortlist_sarah"

    def test_diff_matches_by_rating_key_not_title(self, engine_config, movies, shows):
        """A show's Plex title can carry a year suffix ('Archer (2009)') the pick title ('Archer')
        lacks. The diff must match by ratingKey — not title — or the SAME title reports as removed +
        re-added every run: phantom churn that inflated the run stats (the write already diffed by
        key, so nothing actually changed). Regression for the live run-8 finding (2026-07-20)."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = MagicMock()
        existing.title = "✨ Movies Picked for You" + row_marker(profile.plex_account_id)
        # Same ratingKeys as picks() (1001, 1002), but Plex's titles carry a year suffix the picks lack:
        # NOT ONE pick title equals its collection item's title.
        existing.items.return_value = [
            MagicMock(title="Movie 1 (2009)", ratingKey=1001),
            MagicMock(title="Movie 2 (2015)", ratingKey=1002),
        ]
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        diff, _ = deliver_rows(plex, profile, picks(), engine_config)

        # Matched by key → everything kept, nothing churned, despite every title differing.
        assert diff.kept == ["Movie 1", "Movie 2"]
        assert diff.added == []
        assert diff.removed == []
        plex.set_items.assert_not_called()  # membership already correct by key — no write
        plex.fetch_items.assert_not_called()

    def _existing_with_stale(self, profile, n_stale: int) -> MagicMock:
        existing = MagicMock()
        existing.title = "✨ Movies Picked for You" + row_marker(profile.plex_account_id)
        # n_stale items, none of them wanted (wanted keys are 1001/1002 from picks()), so the update
        # would need n_stale per-item removes.
        existing.items.return_value = [MagicMock(title=f"Stale {k}", ratingKey=2000 + k) for k in range(n_stale)]
        return existing

    def test_large_turnover_rebuilds_instead_of_firing_per_item_removes(self, engine_config, movies, shows):
        """A big turnover (>= _REBUILD_MIN_REMOVES stale items) rebuilds the collection — one batched
        create — instead of N slow per-item removeItems DELETEs. set_items is never called."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 6)  # 6 removes >= threshold -> rebuild
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        diff, stored = deliver_rows(plex, profile, picks(), engine_config)

        plex.delete_owned_collection.assert_called_once()
        assert plex.delete_owned_collection.call_args.args[0] is existing
        # The ownership-guard prefix (rule 4) — LABEL_PREFIX is the one hardcoded source of truth now
        # that EngineConfig no longer carries a (never-set) label_prefix knob of its own.
        assert plex.delete_owned_collection.call_args.args[1] == LABEL_PREFIX
        plex.create_collection.assert_called_once()  # rebuilt via one batched create
        plex.set_items.assert_not_called()  # NOT the per-item update path
        existing.editTitle.assert_not_called()  # nothing to rename — it's being deleted
        plex.fetch_items.assert_called_once_with([1001, 1002])  # the fresh row holds the wanted picks
        assert stored == "Shortlist_sarah"
        assert diff.removed == [f"Stale {k}" for k in range(6)]

    def test_a_collection_that_refuses_every_item_is_rebuilt(self, engine_config, movies, shows):
        """Observed on a real server: an EMPTY collection of ours 400'd on a batch of 30 valid shows
        and on a single one, while a sibling accepted the same item a second later. Same library,
        same subtype, every ratingKey resolving — the Plex object itself was broken, and it stayed
        broken run after run, so that person's row was empty and would never have refilled."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 0)  # empty: nothing to lose by rebuilding
        existing.items.return_value = []
        existing.childCount = 0  # and Plex AGREES it is empty — an empty read alone is not enough
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.set_items.side_effect = CollectionRejectedItems("(400) bad_request; .../collections/9/items")

        diff, stored = deliver_rows(plex, profile, picks(), engine_config)

        plex.delete_owned_collection.assert_called_once()
        assert plex.delete_owned_collection.call_args.args[0] is existing
        assert plex.delete_owned_collection.call_args.args[1] == LABEL_PREFIX
        # The ARGUMENTS, not just the call. Mutating the repair to `title=display` — dropping the
        # per-account invisible marker, which is exactly the shared-tag leak `sweep_broken_rows`
        # exists to clean up — left an assert-called-once test green.
        create = plex.create_collection.call_args
        assert create.args[0] is movies
        assert create.args[1] == "✨ Movies Picked for You" + row_marker(profile.plex_account_id)
        assert plex.fetch_items.call_args.args[0] == [1001, 1002]
        assert stored == "Shortlist_sarah"
        # The ledger's handle must follow the NEW collection, or the next run addresses the deleted one.
        assert diff.rating_key is not None

    def test_a_row_plex_says_has_items_is_never_deleted_on_an_empty_read(self, engine_config, movies, shows):
        """plex-safety rule 4: an empty read never authorises a delete.

        plexapi returns [] for a 200 carrying no children, which is indistinguishable from a failed
        read — the exact successful-but-empty answer rule 4 records for `<Label>`. A PMS mid
        library-index rebuild could hand back an empty membership for a row that really has 30 items,
        and without this guard one bad-read night would delete and recreate every row on the server.
        """
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 0)
        existing.items.return_value = []  # the read says empty...
        existing.childCount = 30  # ...but Plex says it has 30 items
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.set_items.side_effect = CollectionRejectedItems("(400) bad_request; .../collections/9/items")

        with pytest.raises(CollectionRejectedItems):
            deliver_rows(plex, profile, picks(), engine_config)
        plex.delete_owned_collection.assert_not_called()

    def test_a_400_on_a_POPULATED_collection_still_fails(self, engine_config, movies, shows):
        """The repair is deliberately narrow. A row that HAS items has something to lose from being
        deleted, and a 400 there is a different fault — so it must surface, not silently rebuild."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 2)
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.set_items.side_effect = CollectionRejectedItems("(400) bad_request; .../collections/9/items")

        with pytest.raises(CollectionRejectedItems):
            deliver_rows(plex, profile, picks(), engine_config)
        plex.delete_owned_collection.assert_not_called()

    def test_a_non_400_failure_is_never_swallowed(self, engine_config, movies, shows):
        """Anchored on the leading token, not `"400" in`: plexapi puts the collection's own ratingKey
        in the message, so a substring test matches keys like 1400 or 40053. That exact mistake
        swallowed 500s and 401s in `_rename_or_keep`."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 0)
        existing.items.return_value = []
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.set_items.side_effect = RuntimeError("(500) internal; http://pms/library/collections/1400/items")

        with pytest.raises(RuntimeError, match="500"):
            deliver_rows(plex, profile, picks(), engine_config)
        plex.delete_owned_collection.assert_not_called()

    def test_exactly_the_threshold_rebuilds_boundary(self, engine_config, movies, shows):
        """Boundary: removing exactly _REBUILD_MIN_REMOVES items rebuilds (the branch is `>=`)."""
        from shortlist.engine.delivery import _REBUILD_MIN_REMOVES

        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, _REBUILD_MIN_REMOVES)
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        deliver_rows(plex, profile, picks(), engine_config)

        plex.delete_owned_collection.assert_called_once()
        plex.set_items.assert_not_called()

    def test_rebuild_deletes_the_old_row_before_creating_the_new_one(self, engine_config, movies, shows):
        """Leak-safe order: delete-first, then create+label. Nothing exists between the two steps
        (nothing to leak), and it avoids a duplicate-title 409 from two live collections."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 6)
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        deliver_rows(plex, profile, picks(), engine_config)

        names = [c[0] for c in plex.mock_calls]
        assert names.index("delete_owned_collection") < names.index("create_collection")

    def test_a_small_delta_still_updates_in_place_no_rebuild(self, engine_config, movies, shows):
        """Just under the threshold stays on the cheap in-place update — no needless delete+recreate."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 4)  # 4 removes < threshold -> update
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        deliver_rows(plex, profile, picks(), engine_config)

        plex.delete_owned_collection.assert_not_called()
        plex.set_items.assert_called_once()

    def test_dry_run_never_rebuilds(self, engine_config, movies, shows):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 6)
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        deliver_rows(plex, profile, picks(), engine_config, dry_run=True)

        plex.delete_owned_collection.assert_not_called()
        plex.create_collection.assert_not_called()

    def test_records_order_work_on_create_for_the_deferred_ordering_pass(
        self, engine_config: EngineConfig, movies, shows
    ):
        # Ordering is deferred to a post-promote pass; delivery must queue each created collection with
        # its ranked rating keys, or that row silently never gets ordered.
        plex = self._plex(movies, shows)
        order_work: list = []

        deliver_rows(plex, make_profile(), picks(), engine_config, order_work=order_work)

        assert len(order_work) == 1
        coll, keys = order_work[0]
        assert coll is plex.create_collection.return_value
        assert keys == [1001, 1002]  # the ranked rating keys, in order

    def test_records_order_work_on_update(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = MagicMock()
        existing.title = "Old Name" + row_marker(profile.plex_account_id)
        existing.items.return_value = []
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        order_work: list = []

        deliver_rows(plex, profile, picks(), engine_config, order_work=order_work)

        assert (existing, [1001, 1002]) in order_work  # the updated collection is queued too

    def test_dry_run_records_no_order_work(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)
        order_work: list = []
        deliver_rows(plex, make_profile(), picks(), engine_config, dry_run=True, order_work=order_work)
        assert order_work == []

    def test_dry_run_makes_zero_writes(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)

        diff, stored = deliver_rows(plex, make_profile(), picks(), engine_config, dry_run=True)

        assert diff.created is True
        assert stored == "shortlist_sarah"  # requested form; nothing was written to read back
        plex.create_collection.assert_not_called()
        plex.set_items.assert_not_called()
        plex.stored_label.assert_not_called()
        plex.promote.assert_not_called()

    def test_picks_for_a_library_the_server_lacks_are_dropped(self, engine_config: EngineConfig, movies):
        """A movies-only server must not crash on a show pick — it just can't deliver it."""
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        plex.find_owned_collections.return_value = []
        plex.matches_section.return_value = True
        plex.stored_label.return_value = "Shortlist_sarah"

        diff, _ = deliver_rows(plex, make_profile(), picks(media_type=MediaType.SHOW), engine_config)

        plex.create_collection.assert_not_called()
        assert diff.added == []

    def test_a_row_of_the_wrong_type_is_rebuilt_not_patched(self, engine_config: EngineConfig, movies, shows):
        """The sweep has already removed it, so delivery must build a NEW row rather than edit
        the old one. Plex fixes a collection's subtype at creation and never revises it: swapping
        the items would leave the row unhidable and still visible to everyone."""
        mistyped = MagicMock()
        mistyped.title = "✨ Picked for You"
        plex = self._plex(movies, shows)
        plex.find_owned_collections.side_effect = lambda section, label: [mistyped] if section is movies else []
        plex.matches_section.side_effect = lambda collection, section: collection is not mistyped

        diff, stored = deliver_rows(plex, make_profile(), picks(), engine_config)

        plex.set_items.assert_not_called()  # never patched in place
        plex.create_collection.assert_called_once()
        assert plex.create_collection.call_args.args[0] is movies
        assert diff.created is True
        assert stored == "Shortlist_sarah"
        # The deletion is the SWEEP's to report — counting it here too would tell an owner
        # approving a dry run that twice as many rows would be destroyed as actually would.
        assert diff.deleted == []

    def test_a_single_pick_still_gets_a_row_rather_than_deleting_it(self, engine_config: EngineConfig, movies, shows):
        """Deleting an existing row because a library earned only one pick tonight would be a
        destructive answer to a cosmetic problem."""
        plex = self._plex(movies, shows)

        diff, _ = deliver_rows(plex, make_profile(), picks(1), engine_config)

        plex.create_collection.assert_called_once()
        plex.delete_owned_collection.assert_not_called()
        assert diff.added == ["Movie 1"]

    def test_nothing_delivered_reports_no_stored_label(self, engine_config: EngineConfig, movies, shows):
        """The requested label is NOT the stored one — Plex title-cases it. Handing the raw form
        back would write `label!=shortlist_sarah` onto every other user's share, and since excludes
        are compared case-insensitively that wrong casing would look present forever."""
        plex = self._plex(movies, shows)

        diff, stored = deliver_rows(plex, make_profile(), [], engine_config)

        assert stored is None
        assert diff.added == []
        plex.stored_label.assert_not_called()

    def test_per_user_template_override(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)
        profile = make_profile(row_name_template="Sarah's Picks")

        deliver_rows(plex, profile, picks(), engine_config)

        assert plex.create_collection.call_args.args[1] == "Sarah's Picks" + row_marker(profile.plex_account_id)


class TestServerWithTwoLibrariesOfTheSameType:
    """ "Movies" + "4K Movies" is a very common Plex layout, and an UNPINNED row builds in EVERY
    library of its type — one collection per library, each holding that library's own ratingKeys.

    That is what production does: the pipeline always passes `sections=ctx.delivery_sections` (every
    library), and only a row's `library_keys` narrows it. These tests used to assert the opposite —
    "never both" — because they called `deliver_rows` WITHOUT `sections=`, exercising a fallback no
    caller takes. Two live bugs hid behind that fiction: a row delivered to a non-lowest-keyed
    library was never promoted (so it stayed visible in library browse to everyone), and a row
    pinned to one library was curated against the union of all of them.
    """

    def _plex(self, *sections: MagicMock) -> MagicMock:
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = list(sections)
        plex.find_owned_collections.return_value = []
        plex.matches_section.return_value = True
        plex.stored_label.return_value = "Shortlist_sarah"
        return plex

    def test_an_unpinned_row_builds_in_every_library_of_its_type(self, engine_config: EngineConfig):
        movies, movies_4k = _section("Movies", "movie", "1"), _section("4K Movies", "movie", "3")
        plex = self._plex(movies_4k, movies)  # PMS lists 4K first — order must not decide anything
        # The same two films, under each library's own ratingKeys.
        section_index = {"1": {1: 1001, 2: 1002}, "3": {1: 4001, 2: 4002}}

        deliver_rows(
            plex,
            make_profile(),
            picks(),
            engine_config,
            sections=[movies_4k, movies],
            section_index=section_index,
        )

        assert [call.args[0] for call in plex.create_collection.call_args_list] == [movies_4k, movies]
        # Each collection is built from ITS library's ratingKeys. A Plex collection can only hold
        # items of the library it lives in, so the other library's keys name items that are not there.
        assert [call.args[0] for call in plex.fetch_items.call_args_list] == [[4001, 4002], [1001, 1002]]
        # One label across both, because one `label!=` exclude on everyone else has to hide the pair.
        owner_labels = [call.args[1] for call in plex.stored_label.call_args_list if call.args[1] != "shortlist"]
        assert owner_labels == ["shortlist_sarah", "shortlist_sarah"]

    def test_a_pinned_row_builds_only_in_the_library_it_names(self, engine_config: EngineConfig):
        """`library_keys` is the ONLY thing that narrows a row to one library of its type."""
        from shortlist.engine.models import RowSpec

        movies, movies_4k = _section("Movies", "movie", "1"), _section("4K Movies", "movie", "3")
        plex = self._plex(movies, movies_4k)
        section_index = {"1": {1: 1001, 2: 1002}, "3": {1: 4001, 2: 4002}}
        spec = RowSpec(slug="gems", name_template="Gems", size=5, library_keys=["3"])

        deliver_rows(
            plex,
            make_profile(),
            picks(),
            engine_config,
            spec,
            sections=[movies, movies_4k],
            section_index=section_index,
        )

        plex.create_collection.assert_called_once()
        assert plex.create_collection.call_args.args[0] is movies_4k
        plex.fetch_items.assert_called_once_with([4001, 4002])

    def test_the_legacy_no_sections_fallback_uses_one_library_per_type(self, engine_config: EngineConfig):
        """LEGACY PATH — no production caller reaches it.

        `rows.py` always passes `sections=ctx.delivery_sections`. Omitting it falls back to
        `sections_by_type()` (one library per type, lowest key wins), which is kept only so an
        older/simpler caller cannot crash. It is pinned here so the fallback stays deterministic —
        NOT as a statement of what a real run does. Believing this was the real contract is what
        let a row leak in the library nobody promoted it in.
        """
        movies, movies_4k = _section("Movies", "movie", "1"), _section("4K Movies", "movie", "3")
        plex = self._plex(movies_4k, movies)
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies}  # lowest key of the type

        deliver_rows(plex, make_profile(), picks(), engine_config)

        plex.create_collection.assert_called_once()
        assert plex.create_collection.call_args.args[0] is movies

    def test_a_well_typed_row_in_the_other_library_is_left_alone(self, engine_config: EngineConfig):
        """A foreign row that already carries our label still gets its own fresh row built beside
        it, and the old one is NOT deleted: it still carries the label, so it is still hidden from
        everyone else, and destroying a collection we are not going to replace is not our call."""
        movies, movies_4k = _section("Movies", "movie", "1"), _section("4K Movies", "movie", "3")
        stray = MagicMock()
        stray.title = "✨ Picked for You"  # no marker: a pre-marker row, whose tag is shared
        plex = self._plex(movies, movies_4k)
        plex.find_owned_collections.side_effect = lambda section, label: [stray] if section is movies_4k else []

        deliver_rows(plex, make_profile(), picks(), engine_config, sections=[movies, movies_4k])

        plex.delete_owned_collection.assert_not_called()
        stray.editTitle.assert_not_called()  # never renamed into ours either


class TestSweepBrokenRows:
    """The sweep is the one thing standing between a stranded row and every user on the server.

    It runs server-wide, before any per-user work, on every run. Its whole branch matrix is here
    because nothing else in the suite can catch a regression in it: an earlier version's dry-run
    guard could be deleted — making `--dry-run` destroy real collections — with the entire suite
    still green.
    """

    def _plex(self, movies: MagicMock, shows: MagicMock, *collections: MagicMock) -> MagicMock:
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies, shows]
        movies.collections.return_value = [c for c in collections if c.section is movies]
        shows.collections.return_value = [c for c in collections if c.section is shows]
        return plex

    def _collection(self, section: MagicMock, *labels: str, title: str = "✨ Picked for You") -> MagicMock:
        collection = MagicMock()
        collection.title = title
        collection.section = section
        collection.labels = [SimpleNamespace(tag=label) for label in labels]
        return collection

    def test_deletes_a_row_that_cannot_be_hidden_and_names_its_owner(self, engine_config: EngineConfig, movies, shows):
        stranded = self._collection(movies, "Shortlist_mike")  # a show-subtype row in the movie library
        plex = self._plex(movies, shows, stranded)
        plex.matches_section.return_value = False

        deleted = sweep_broken_rows(plex, engine_config)

        assert deleted == {"mike": ["✨ Picked for You"]}
        plex.delete_owned_collection.assert_called_once_with(stranded, "shortlist")

    def test_leaves_a_well_typed_row_alone(self, engine_config: EngineConfig, movies, shows):
        healthy = self._collection(movies, "Shortlist_mike")
        plex = self._plex(movies, shows, healthy)
        plex.matches_section.return_value = True

        assert sweep_broken_rows(plex, engine_config) == {}
        plex.delete_owned_collection.assert_not_called()

    def test_never_touches_a_collection_it_does_not_own(self, engine_config: EngineConfig, movies, shows):
        """Kometa coexistence (rule 4). A foreign collection may well be "mistyped" by our
        definition — that is not our business, and deleting it would be unforgivable."""
        kometa = self._collection(movies, "Overlay", title="Kometa: Best of the 90s")
        plex = self._plex(movies, shows, kometa)
        plex.matches_section.return_value = False  # even so

        assert sweep_broken_rows(plex, engine_config) == {}
        plex.delete_owned_collection.assert_not_called()

    def test_dry_run_reports_the_deletion_without_making_it(self, engine_config: EngineConfig, movies, shows):
        """`--dry-run` exists so an owner can see what a run would do to a live server. If this
        guard ever breaks, dry-run silently destroys real collections."""
        stranded = self._collection(movies, "Shortlist_mike")
        plex = self._plex(movies, shows, stranded)
        plex.matches_section.return_value = False

        deleted = sweep_broken_rows(plex, engine_config, dry_run=True)

        assert deleted == {"mike": ["✨ Picked for You"]}
        plex.delete_owned_collection.assert_not_called()

    def test_sweeps_every_library_and_every_user(self, engine_config: EngineConfig, movies, shows):
        """It is not scoped to tonight's users: a paused user's leaking row is still a leak."""
        stranded_movie = self._collection(movies, "Shortlist_mike")
        stranded_show = self._collection(shows, "Shortlist_sarah", title="Because you watched Fargo")
        plex = self._plex(movies, shows, stranded_movie, stranded_show)
        plex.matches_section.return_value = False

        deleted = sweep_broken_rows(plex, engine_config)

        assert deleted == {"mike": ["✨ Picked for You"], "sarah": ["Because you watched Fargo"]}
        assert plex.delete_owned_collection.call_count == 2

    def test_deletes_an_unlabelled_orphan_carrying_our_marker(self, engine_config: EngineConfig, movies, shows):
        # A per-user row whose label write never landed: marker present, NO shortlist label. No
        # `label!=` can hide a label-less collection, so it leaks to EVERY user — the SFLIX incident.
        # It's correctly typed, so the ONLY defect is the missing label; the marker proves it's ours.
        orphan = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, orphan)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = True  # the server agrees it has no label

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

        assert deleted == {"mike": [orphan.title]}
        plex.delete_owned_collection.assert_called_once_with(orphan, "shortlist")

    def test_attributes_an_orphan_by_decoded_account_when_the_owner_is_unknown(
        self, engine_config: EngineConfig, movies, shows
    ):
        # A departed user's orphan isn't in `markers`; the account id decoded from the marker still
        # names it in the audit trail so "whose row did you delete" stays answerable (rule 10).
        orphan = self._collection(shows, title="✨ TV Shows Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, orphan)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = True  # the server agrees it has no label

        deleted = sweep_broken_rows(plex, engine_config)

        assert deleted == {"orphan:202": [orphan.title]}
        plex.delete_owned_collection.assert_called_once()

    def test_an_orphan_is_confirmed_against_the_server_before_it_is_deleted(
        self, engine_config: EngineConfig, movies, shows
    ):
        """The collection LIST does not carry labels on a real PMS — verified on 1.43.3.10861: 103
        collections, zero `<Label>` children. `collection.labels` is populated only because plexapi
        silently re-reads each collection behind the attribute, so "no label" here can equally mean
        "the label did not come back". Deleting on that reading is unrecoverable, so it is checked."""
        orphan = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, orphan)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = True

        sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

        plex.confirm_unlabelled.assert_called_once_with(orphan, "shortlist")

    def test_a_label_read_that_comes_back_empty_does_not_wipe_the_server(
        self, engine_config: EngineConfig, movies, shows
    ):
        """The catastrophic case, and the reason the guard exists.

        Every Shortlist row carries the invisible marker, and `delete_owned_collection` accepts that
        marker ALONE as proof of ownership. So one empty label read turns every row on the server
        into an unlabelled orphan and this loop deletes all of them — while the run reports success,
        because nothing raised. The server saying "no, these are labelled" has to stop it dead.
        """
        rows = [
            self._collection(movies, title=f"✨ Movies Picked for You{row_marker(account)}")
            for account in (201, 202, 203)
        ]
        plex = self._plex(movies, shows, *rows)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = False  # the re-read finds labels after all

        deleted = sweep_broken_rows(plex, engine_config)

        assert deleted == {}
        plex.delete_owned_collection.assert_not_called()

    def test_no_labels_at_all_on_a_server_full_of_our_rows_is_a_read_failure(
        self, engine_config: EngineConfig, movies, shows
    ):
        """The SYSTEMIC case the per-collection re-read cannot catch: if the PMS answers both reads
        the same way — mid library-index rebuild, or a version that stops serving `<Label>` — then
        confirming each row individually just agrees with itself, and the sweep deletes everything.

        So the aggregate is the second guard, the same reasoning the privacy sync already applies:
        an EMPTY enumeration is not evidence of absence. Rows of ours exist and NOT ONE reads as
        labelled — that is a failed read, not a server full of orphans.
        """
        rows = [
            self._collection(movies, title=f"✨ Movies Picked for You{row_marker(account)}")
            for account in (201, 202, 203)
        ]
        plex = self._plex(movies, shows, *rows)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = True  # even a second read agrees — it is systemic

        deleted = sweep_broken_rows(plex, engine_config)

        assert deleted == {}
        plex.delete_owned_collection.assert_not_called()
        plex.confirm_unlabelled.assert_not_called(), "the aggregate check must short-circuit first"

    def test_one_orphan_beside_healthy_labelled_rows_is_still_deleted(self, engine_config: EngineConfig, movies, shows):
        """The guard must not become a blanket refusal. Labels ARE readable here — other rows came
        back labelled — so the single unlabelled one is a genuine orphan and still gets removed."""
        # Sarah's row, and no marker is passed for her — so the shared-tag rule leaves it alone and
        # the only thing under test is whether the aggregate guard lets the real orphan through.
        healthy = self._collection(movies, "Shortlist_sarah")
        orphan = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, healthy, orphan)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = True

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

        assert deleted == {"mike": [orphan.title]}
        plex.delete_owned_collection.assert_called_once_with(orphan, "shortlist")

    def test_a_lone_orphan_on_an_otherwise_empty_server_is_still_deleted(
        self, engine_config: EngineConfig, movies, shows
    ):
        """A fresh install whose first run died between creating a collection and labelling it: no
        labelled rows exist to prove the read works, but ONE orphan is not a mass-deletion signature,
        and leaving it would leave a row nothing can hide."""
        orphan = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, orphan)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = True

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

        assert deleted == {"mike": [orphan.title]}

    def test_a_failed_re_read_is_treated_as_do_not_delete(self, engine_config: EngineConfig, movies, shows):
        """`confirm_unlabelled` returns False when it cannot read at all. "I don't know" must never
        authorise a delete — the same fail-closed direction the privacy sync takes."""
        orphan = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, orphan)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = False

        assert sweep_broken_rows(plex, engine_config) == {}
        plex.delete_owned_collection.assert_not_called()

    def test_leaves_an_unlabelled_collection_without_our_marker_alone(self, engine_config: EngineConfig, movies, shows):
        # No label AND no marker → genuinely foreign (Kometa etc.). Never touched (rule 4).
        foreign = self._collection(movies, title="Kometa: Best of the 90s")
        plex = self._plex(movies, shows, foreign)
        plex.matches_section.return_value = True

        assert sweep_broken_rows(plex, engine_config) == {}
        plex.delete_owned_collection.assert_not_called()

    def test_dry_run_reports_an_orphan_without_deleting_it(self, engine_config: EngineConfig, movies, shows):
        orphan = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, orphan)
        plex.matches_section.return_value = True

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)}, dry_run=True)

        assert deleted == {"mike": [orphan.title]}
        plex.delete_owned_collection.assert_not_called()

    def test_an_empty_server_is_not_an_error(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)
        assert sweep_broken_rows(plex, engine_config) == {}


class TestAnUnlabelledRowIsNeverLeftBehind:
    """A collection without a `shortlist_*` label is invisible to Shortlist forever.

    `find_owned_collection`, `owned_collections`, `sweep_unhidable_rows` and uninstall ALL match
    on that label prefix. So a row created but not labelled can never be found, never be hidden
    by a share filter, and never be cleaned up — it just sits there, visible to everyone. Create
    and label must therefore succeed together or not at all.
    """

    def test_a_failure_to_label_deletes_the_row_it_just_created(self, engine_config: EngineConfig):
        movies = _section("Movies", "movie", 1)
        created = MagicMock()
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        plex.find_owned_collections.return_value = []
        plex.matches_section.return_value = True
        plex.create_collection.return_value = created
        plex.stored_label.side_effect = RuntimeError("PMS timed out")

        with pytest.raises(RuntimeError, match="PMS timed out"):
            deliver_rows(plex, make_profile(), picks(), engine_config)

        created.delete.assert_called_once()

    def test_the_original_failure_is_raised_even_if_the_cleanup_also_fails(self, engine_config: EngineConfig):
        """The owner needs to know the LABEL write failed — that is the actionable fault. The
        orphan is logged with its ratingKey for a human to remove by hand."""
        movies = _section("Movies", "movie", 1)
        created = MagicMock()
        created.delete.side_effect = RuntimeError("PMS still down")
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        plex.find_owned_collections.return_value = []
        plex.matches_section.return_value = True
        plex.create_collection.return_value = created
        plex.stored_label.side_effect = RuntimeError("label write failed")

        with pytest.raises(RuntimeError, match="label write failed"):
            deliver_rows(plex, make_profile(), picks(), engine_config)


class TestARowSharingItsTagWithOthers:
    """A row created before the invisible marker existed shares its collection TAG — and therefore
    its contents — with every other user's row in that library. It holds their picks as well as its
    owner's. Renaming cannot undo that (the items keep the old tag): it has to be rebuilt.

    The SWEEP removes it (server-wide, before any user work, so it also reaches the rows of paused
    users and of users who get no picks tonight). Delivery then simply finds nothing and builds a
    fresh one — it must not delete or report the row a second time, or a dry run would tell the
    owner twice as many of their rows would be destroyed as actually would be.
    """

    def _plex(self, movies: MagicMock, shows: MagicMock) -> MagicMock:
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies, shows]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies, MediaType.SHOW: shows}
        plex.matches_section.return_value = True
        plex.stored_label.return_value = "Shortlist_sarah"
        return plex

    def test_a_row_without_the_marker_is_rebuilt_not_renamed(self, engine_config: EngineConfig, movies, shows):
        legacy = MagicMock()
        legacy.title = "✨ Picked for You"  # no marker: shared with everyone else's row
        plex = self._plex(movies, shows)
        plex.find_owned_collections.side_effect = lambda section, label: [legacy] if section is movies else []

        diff, _ = deliver_rows(plex, make_profile(), picks(), engine_config)

        legacy.editTitle.assert_not_called()
        plex.set_items.assert_not_called()
        plex.create_collection.assert_called_once()
        assert diff.created is True
        # The sweep already deleted it and recorded that. Delivery must not double-count.
        plex.delete_owned_collection.assert_not_called()
        assert diff.deleted == []


class TestFindThisRowsCollection:
    """`_find_this_rows_collection` is the identity match `_deliver_one` was extracted around: which
    (if any) of this user's OWNED collections is this row. Rule 4 ("touch only what we own") lives
    here — every candidate it can return comes from the `owned` list the caller already filtered by
    this user's label, never anything else on the server.
    """

    def _plex(self, matches_section: bool = True) -> MagicMock:
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.matches_section.return_value = matches_section
        return plex

    def test_matches_by_title_when_it_already_carries_the_current_marker(self):
        from shortlist.engine.delivery import _find_this_rows_collection

        section = _section("Movies", "movie", 1)
        marker = row_marker(1)
        wanted = MagicMock(title="✨ Picked for You" + marker, ratingKey=111)
        other = MagicMock(title="Some Other Row" + marker, ratingKey=222)
        plex = self._plex()

        found = _find_this_rows_collection(
            plex, section, [other, wanted], wanted.title, marker, delivered_key=None, sole_row=False, who="alex"
        )

        assert found is wanted
        plex.matches_section.assert_called_once_with(wanted, section)

    def test_matches_by_ledger_ratingkey_when_the_title_no_longer_renders(self):
        """A renamed template/library/nickname means the title we'd render today no longer matches
        what's on the server — the ledger's ratingKey is this row's identity, independent of title."""
        from shortlist.engine.delivery import _find_this_rows_collection

        section = _section("Movies", "movie", 1)
        marker = row_marker(1)
        renamed = MagicMock(title="Old Template Name" + marker, ratingKey=555)
        plex = self._plex()

        found = _find_this_rows_collection(
            plex,
            section,
            [renamed],
            "New Template Name" + marker,
            marker,
            delivered_key=555,
            sole_row=False,
            who="alex",
        )

        assert found is renamed
        plex.matches_section.assert_called_once_with(renamed, section)

    def test_a_collection_outside_owned_is_never_claimed(self):
        """Foreign (e.g. Kometa) and other-user collections never carry our label, so the caller's
        `find_owned_collections` never puts them in `owned` — this function only ever considers what
        it is handed. Nothing else on the server, however similarly titled, is reachable from here."""
        from shortlist.engine.delivery import _find_this_rows_collection

        section = _section("Movies", "movie", 1)
        marker = row_marker(1)
        plex = self._plex()

        found = _find_this_rows_collection(
            plex, section, [], "✨ Picked for You" + marker, marker, delivered_key=None, sole_row=True, who="alex"
        )

        assert found is None
        # Nothing was even a candidate, so there was nothing to type-check.
        plex.matches_section.assert_not_called()

    def test_sole_row_fallback_requires_both_the_marker_and_being_the_only_owned_row(self):
        """The legacy fallback (rows delivered before the ledger existed) may rename a row in place
        only when it's unambiguous: exactly one owned collection, and it already carries THIS
        account's marker — otherwise it could be a shared-tag row holding other people's picks too,
        and must be rebuilt rather than adopted."""
        from shortlist.engine.delivery import _find_this_rows_collection

        section = _section("Movies", "movie", 1)
        marker = row_marker(1)
        sole = MagicMock(title="Renamed By Template Change" + marker, ratingKey=999)
        plex = self._plex()

        matched = _find_this_rows_collection(
            plex, section, [sole], "✨ Picked for You" + marker, marker, delivered_key=None, sole_row=True, who="alex"
        )
        assert matched is sole

        not_matched = _find_this_rows_collection(
            plex, section, [sole], "✨ Picked for You" + marker, marker, delivered_key=None, sole_row=False, who="alex"
        )
        assert not_matched is None


class TestRowMarker:
    def test_distinct_accounts_get_distinct_markers(self):
        """The marker IS the row's identity within a library. Two accounts sharing one would share
        a collection tag — and with it, each other's picks."""
        assert row_marker(1) != row_marker(2)
        assert row_marker(555000001) != row_marker(555000002)

    def test_the_encoding_is_not_truncated(self):
        """Encoding only the low N bits makes any two ids congruent modulo 2**N collide — a
        silent return of the bug, in a cell no test could reach."""
        assert row_marker(1) != row_marker(1 + 2**32)
        assert row_marker(7) != row_marker(7 + 2**48)

    def test_it_renders_as_nothing(self):
        marker = row_marker(555000001)
        assert marker.strip("\u200b\u200c") == ""
        assert len(marker) == 64


class TestTheSweepRemovesSharedTagRows:
    """A row whose title lacks its owner's marker shares a collection TAG with every other row in
    that library — so it shows its owner other people's recommendations.

    The sweep is where this is fixed, not delivery, because delivery only ever visits users who
    are being processed AND have picks for that library. A paused user's row, or the stale movie
    row of someone who only watches TV, would otherwise sit there forever showing them everyone
    else's picks.
    """

    def _plex(self, movies: MagicMock, shows: MagicMock, *collections: MagicMock) -> MagicMock:
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies, shows]
        plex.matches_section.return_value = True  # correctly typed: only the TAG is wrong
        movies.collections.return_value = [c for c in collections if c.section is movies]
        shows.collections.return_value = [c for c in collections if c.section is shows]
        return plex

    def _row(self, section: MagicMock, slug: str, title: str) -> MagicMock:
        collection = MagicMock()
        collection.title = title
        collection.section = section
        collection.labels = [SimpleNamespace(tag=f"Shortlist_{slug}")]
        return collection

    def test_a_row_without_its_owners_marker_is_removed(self, engine_config: EngineConfig, movies, shows):
        legacy = self._row(movies, "mike", "✨ Picked for You")
        plex = self._plex(movies, shows, legacy)

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

        assert deleted == {"mike": ["✨ Picked for You"]}
        plex.delete_owned_collection.assert_called_once_with(legacy, "shortlist")

    def test_a_row_with_its_owners_marker_is_left_alone(self, engine_config: EngineConfig, movies, shows):
        healthy = self._row(movies, "mike", "✨ Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, healthy)

        assert sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)}) == {}
        plex.delete_owned_collection.assert_not_called()

    def test_a_row_whose_owner_shortlist_cannot_identify_is_left_alone(
        self, engine_config: EngineConfig, movies, shows
    ):
        """Without the account id there is no marker to check and no way to rebuild the row —
        destroying something we cannot replace would be worse than leaving it."""
        unknown = self._row(movies, "stranger", "✨ Picked for You")
        plex = self._plex(movies, shows, unknown)

        assert sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)}) == {}
        plex.delete_owned_collection.assert_not_called()

    def test_dry_run_reports_without_removing(self, engine_config: EngineConfig, movies, shows):
        legacy = self._row(movies, "mike", "✨ Picked for You")
        plex = self._plex(movies, shows, legacy)

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)}, dry_run=True)

        assert deleted == {"mike": ["✨ Picked for You"]}
        plex.delete_owned_collection.assert_not_called()


class TestRemoveRowCollections:
    """The on-demand reconcile primitive: remove a row's collections outside a run (removal only)."""

    def test_strip_marker_is_the_inverse_of_the_marker_suffix(self):
        from shortlist.engine.delivery import strip_marker

        assert strip_marker("Picked for You" + row_marker(218833834)) == "Picked for You"
        assert strip_marker("No marker here") == "No marker here"

    def test_removes_only_the_titles_asked_for(self, engine_config: EngineConfig, movies):
        from shortlist.engine.delivery import remove_row_collections

        keep = MagicMock(title="💎 Hidden Gems" + row_marker(100))
        drop = MagicMock(title="✨ Picked for You" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.find_owned_collections.side_effect = lambda section, label: [keep, drop]

        removed = remove_row_collections(
            plex, engine_config, label="shortlist_sarah", displays={"✨ Picked for You"}, dry_run=False
        )

        assert removed == ["✨ Picked for You"]  # the other row is left alone
        plex.delete_owned_collection.assert_called_once_with(drop, "shortlist")

    def test_displays_none_removes_every_collection_under_the_label(self, engine_config: EngineConfig, movies, shows):
        from shortlist.engine.delivery import remove_row_collections

        m = MagicMock(title="🔥 Popular" + row_marker(0))
        s = MagicMock(title="🔥 Popular" + row_marker(0))
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.side_effect = lambda section, label: [m] if section is movies else [s]

        removed = remove_row_collections(
            plex, engine_config, label="shortlist__shared_popular", displays=None, dry_run=False
        )

        assert removed == ["🔥 Popular", "🔥 Popular"]  # every library
        # The exact objects the SUT selected were the ones deleted — not just "two deletes happened".
        from unittest.mock import call

        assert plex.delete_owned_collection.call_args_list == [call(m, "shortlist"), call(s, "shortlist")]

    def test_dry_run_reports_but_deletes_nothing(self, engine_config: EngineConfig, movies):
        from shortlist.engine.delivery import remove_row_collections

        c = MagicMock(title="✨ Picked for You" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.find_owned_collections.side_effect = lambda section, label: [c]

        removed = remove_row_collections(
            plex, engine_config, label="shortlist_sarah", displays={"✨ Picked for You"}, dry_run=True
        )

        assert removed == ["✨ Picked for You"]
        plex.delete_owned_collection.assert_not_called()


class TestRenameRowCollections:
    """The on-demand rename reconcile: retitle a row's collections in place (privacy-neutral)."""

    def test_renames_only_the_matching_row_in_place(self, engine_config: EngineConfig, movies):
        from shortlist.engine.delivery import rename_row_collections

        marker = row_marker(100)
        target = MagicMock(title="Old Gems" + marker)
        other = MagicMock(title="Popular" + marker)  # a different row of the same user — must be untouched
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.find_owned_collections.side_effect = lambda section, label: [target, other]

        renamed = rename_row_collections(
            plex,
            engine_config,
            label="shortlist_sarah",
            marker=marker,
            old_display="Old Gems",
            new_display="Buried Treasure",
            dry_run=False,
        )

        assert renamed == ["Movies"]
        # SUT-controlled contract: the NEW human title + the SAME account marker, only on the matched row.
        target.editTitle.assert_called_once_with("Buried Treasure" + marker)
        other.editTitle.assert_not_called()

    def test_scans_every_library(self, engine_config: EngineConfig, movies, shows):
        from shortlist.engine.delivery import rename_row_collections

        marker = row_marker(0)
        m = MagicMock(title="Old Gems" + marker)
        s = MagicMock(title="Old Gems" + marker)
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.side_effect = lambda section, label: [m] if section is movies else [s]

        renamed = rename_row_collections(
            plex,
            engine_config,
            label="shortlist_sarah",
            marker=marker,
            old_display="Old Gems",
            new_display="New Gems",
            dry_run=False,
        )

        assert renamed == ["Movies", "TV Shows"]
        m.editTitle.assert_called_once_with("New Gems" + marker)
        s.editTitle.assert_called_once_with("New Gems" + marker)

    def test_already_renamed_is_skipped(self, engine_config: EngineConfig, movies):
        from shortlist.engine.delivery import rename_row_collections

        marker = row_marker(100)
        already = MagicMock(title="New Gems" + marker)  # its stripped title != old_display → not matched
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.find_owned_collections.side_effect = lambda section, label: [already]

        renamed = rename_row_collections(
            plex,
            engine_config,
            label="shortlist_sarah",
            marker=marker,
            old_display="Old Gems",
            new_display="New Gems",
            dry_run=False,
        )

        assert renamed == []
        already.editTitle.assert_not_called()

    def test_dry_run_reports_but_renames_nothing(self, engine_config: EngineConfig, movies):
        from shortlist.engine.delivery import rename_row_collections

        marker = row_marker(100)
        c = MagicMock(title="Old Gems" + marker)
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.find_owned_collections.side_effect = lambda section, label: [c]

        renamed = rename_row_collections(
            plex,
            engine_config,
            label="shortlist_sarah",
            marker=marker,
            old_display="Old Gems",
            new_display="New Gems",
            dry_run=True,
        )

        assert renamed == ["Movies"]
        c.editTitle.assert_not_called()


class TestMutingNeverDeletesADifferentRow:
    """`remove_row` matches a muted row's collection by its rendered title — and per-person rows share
    ONE label, told apart by title alone.

    A `{top_seed}` (or blank) template renders to the bare default with no picks, so matching on that
    finds whatever else is titled that: the user's live default row, or a cold-start row. Muting one
    row deleted a different row's collection, in every library, every run.

    `_retired_rows` guards the identical collision for DISABLED rows and its docstring claimed the mute
    path already did the same. It did not.
    """

    def _plex(self, titles: list[str]):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        deleted: list[str] = []
        section = SimpleNamespace(title="Movies", key="1", type="movie")
        plex = MagicMock()
        plex.sections_by_type.return_value = {"movie": section}
        plex.find_owned_collections.return_value = [SimpleNamespace(title=t) for t in titles]
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append(c.title)
        return plex, [section], deleted

    def _remove(self, plex, sections, template, delivered_keys=None):
        from shortlist.engine.delivery import remove_row
        from shortlist.engine.models import CollectionDiff, EngineConfig, RowSpec, UserProfile, UserType

        diff = CollectionDiff()
        remove_row(
            plex,
            UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah"),
            EngineConfig(),
            RowSpec(slug="because", name_template=template, size=5),
            dry_run=False,
            diff=diff,
            sections=sections,
            delivered_keys=delivered_keys,
        )
        return diff

    def test_a_top_seed_row_leaves_the_live_default_row_alone(self):
        from shortlist.engine.delivery import DEFAULT_ROW_NAME, row_marker

        plex, sections, deleted = self._plex([DEFAULT_ROW_NAME + row_marker(100)])

        diff = self._remove(plex, sections, "Because you watched {top_seed}")

        assert deleted == [], "muting a {top_seed} row must not touch the row that happens to hold that title"
        assert diff.deleted == []

    def test_a_blank_template_is_equally_refused(self):
        """A whitespace-only template renders to the default too — test the RESULT, not a substring,
        or '   ' slips through and re-opens the collision."""
        from shortlist.engine.delivery import DEFAULT_ROW_NAME, row_marker

        plex, sections, deleted = self._plex([DEFAULT_ROW_NAME + row_marker(100)])

        self._remove(plex, sections, "   ")

        assert deleted == []

    def test_a_library_name_template_is_still_removed(self):
        """The guard is per LIBRARY, not once up front: `{library_name}` collapses to the bare default
        only when there is no library name, and here there always is. Guarding globally would stop the
        default row — the one nearly every server has — from ever being un-muted correctly."""
        from shortlist.engine.delivery import row_marker

        plex, sections, deleted = self._plex(["✨ Movies Picked for You" + row_marker(100)])

        diff = self._remove(plex, sections, "✨ {library_name} Picked for You")

        assert deleted == ["✨ Movies Picked for You" + row_marker(100)]
        assert diff.deleted == ["✨ Movies Picked for You"]

    def test_a_static_titled_row_is_still_removed(self):
        from shortlist.engine.delivery import row_marker

        plex, sections, deleted = self._plex(["Hidden Gems" + row_marker(100)])

        self._remove(plex, sections, "Hidden Gems")

        assert deleted == ["Hidden Gems" + row_marker(100)]


class TestTheLedgerRemovesAnUnrenderableRow:
    """The delivery ledger's ratingKey is the ONLY handle on a `{top_seed}` row — its title was
    different every run, so nothing computed from config can find it.

    Without this a row set to skip a cold start (issue #66), or a muted `{top_seed}` row, could never
    actually be removed: the title guard above correctly refuses to match, and there was nothing else
    to match ON. Identity is still scoped to the user's own label, so it narrows the search rather
    than widening ownership.
    """

    def _plex(self, collections):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        deleted: list[str] = []
        section = SimpleNamespace(title="Movies", key="1", type="movie")
        plex = MagicMock()
        plex.sections_by_type.return_value = {"movie": section}
        plex.find_owned_collections.return_value = collections
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append(c.title)
        return plex, [section], deleted

    def _collection(self, title: str, rating_key: int):
        from types import SimpleNamespace

        return SimpleNamespace(title=title, ratingKey=rating_key)

    def _remove(self, plex, sections, template, delivered_keys):
        from shortlist.engine.delivery import remove_row
        from shortlist.engine.models import CollectionDiff, EngineConfig, RowSpec, UserProfile, UserType

        diff = CollectionDiff()
        remove_row(
            plex,
            UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah"),
            EngineConfig(),
            RowSpec(slug="because", name_template=template, size=5),
            dry_run=False,
            diff=diff,
            sections=sections,
            delivered_keys=delivered_keys,
        )
        return diff

    def test_a_top_seed_row_is_removed_by_its_ledger_key(self):
        from shortlist.engine.delivery import row_marker

        target = self._collection("Because you watched The Bear" + row_marker(100), 4242)
        plex, sections, deleted = self._plex([target])

        diff = self._remove(plex, sections, "Because you watched {top_seed}", {"1": 4242})

        assert deleted == ["Because you watched The Bear" + row_marker(100)]
        # The COLLECTION's own title, not the computed one — the computed name is the bare default
        # here, and reporting that would name a row the owner never had.
        assert diff.deleted == ["Because you watched The Bear"]

    def test_a_ledger_key_never_reaches_a_different_row(self):
        """Identity must select ONE object. The user's live default row shares this label and is the
        exact collection the title guard exists to protect."""
        from shortlist.engine.delivery import DEFAULT_ROW_NAME, row_marker

        other = self._collection(DEFAULT_ROW_NAME + row_marker(100), 999)
        target = self._collection("Because you watched The Bear" + row_marker(100), 4242)
        plex, sections, deleted = self._plex([other, target])

        self._remove(plex, sections, "Because you watched {top_seed}", {"1": 4242})

        assert deleted == ["Because you watched The Bear" + row_marker(100)]

    def test_a_zero_ledger_key_matches_nothing(self):
        """0 means "the PMS never gave us one" — a dry run records it, and `_rating_key` also returns 0
        for a collection carrying no key. Treating it as a match would delete every keyless collection
        under this label."""
        from shortlist.engine.delivery import DEFAULT_ROW_NAME, row_marker

        keyless = self._collection(DEFAULT_ROW_NAME + row_marker(100), 0)
        plex, sections, deleted = self._plex([keyless])

        self._remove(plex, sections, "Because you watched {top_seed}", {"1": 0})

        assert deleted == []

    def test_a_stale_key_never_reaches_a_row_whose_title_renders(self):
        """The ledger is the fallback for an uncomputable title, NOT a second matcher.

        ratingKeys are rowids Plex reuses and no delete path prunes the ledger, so a stale key can
        name a live object — and scoped to this label, that object is one of this user's OTHER rows.
        Matching a static-titled row by key as well as by title deleted the user's live default row
        and logged it as an ordinary removal.
        """
        from shortlist.engine.delivery import DEFAULT_ROW_NAME, row_marker

        live_default = self._collection(DEFAULT_ROW_NAME + row_marker(100), 4242)
        plex, sections, deleted = self._plex([live_default])

        # A static-titled row being removed, carrying a stale key that now names the default row.
        self._remove(plex, sections, "Hidden Gems", {"1": 4242})

        assert deleted == [], "a stale ledger key deleted a different row that titles perfectly well"

    def test_a_key_for_another_library_does_not_reach_this_one(self):
        """Keys are per section. A row's copy in Movies must not be matched by the key of its copy in
        TV, or narrowing a row to one library would delete the wrong side of it."""
        target = self._collection("Because you watched The Bear", 4242)
        plex, sections, deleted = self._plex([target])

        self._remove(plex, sections, "Because you watched {top_seed}", {"77": 4242})

        assert deleted == []


class TestTheConstantLabel:
    """Every row carries a constant `shortlist` label beside its `shortlist_<user>` one.

    A co-managing tool (agregarr, Kometa) is pointed at a list of labels to leave alone, and ours are
    per PERSON — a 46-account server has 46 of them, plus one per shared row, and the list goes stale
    the moment somebody joins or leaves. This one never changes, so it is a single entry forever.
    """

    def _plex(self, movies):
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        plex.find_owned_collections.return_value = []
        return _labelling_plex_mock(plex)

    def test_the_owner_label_is_still_applied_and_is_what_delivery_reports(self, engine_config: EngineConfig, movies):
        """The constant label is ADDITIVE. If it ever replaced the per-user one, every other
        account's `label!=shortlist_<user>` exclude would stop matching and the row would be visible
        to the whole server — the leak this app exists to prevent."""
        plex = self._plex(movies)

        _diff, stored = deliver_rows(plex, make_profile(), picks(), engine_config)

        applied = [c.args[1] for c in plex.stored_label.call_args_list]
        assert "shortlist_sarah" in applied, "the per-user label is what every share filter excludes"
        assert "shortlist" in applied
        assert stored == "Shortlist_sarah", "the reported label is the OWNER one, not the constant"

    def test_a_row_survives_a_constant_label_that_will_not_stick(self, engine_config: EngineConfig, movies):
        """Unlike the per-user label, this one is never worth a row.

        The create path DELETES a collection whose labelling fails — correctly, because a row with no
        `shortlist_<user>` label can never be found, hidden or removed again. The constant label
        carries none of that: without it a co-managing tool merely keeps reordering this one row. So
        its failure must not reach that delete, or a cosmetic label would start destroying rows.
        """
        plex = self._plex(movies)
        # Keep the honest labelling for the OWNER label and fail only the constant one. Replacing
        # `stored_label` wholesale left the owner label off the collection, so `_apply_shortlist_label`
        # tripped its own guard and returned before ever writing — the swallow this test exists for
        # was never reached, and deleting it would not have failed anything.
        labelling = plex.stored_label.side_effect

        def boom(collection, label):
            if label == LABEL_PREFIX:
                raise RuntimeError("PMS said no")
            return labelling(collection, label)

        plex.stored_label.side_effect = boom

        diff, stored = deliver_rows(plex, make_profile(), picks(), engine_config)

        assert stored == "Shortlist_sarah"
        assert diff.created is True
        collection = plex.create_collection.return_value
        collection.delete.assert_not_called()  # the row must outlive a cosmetic label

    def test_it_refuses_to_write_when_the_owner_label_is_not_in_the_returned_labels(
        self, engine_config: EngineConfig, movies
    ):
        """The leak this guard exists to stop.

        plexapi's addLabel is NOT additive on the wire: it builds the new tag list as
        `collection.labels + [new]` and PUTs it as an ABSOLUTE set (mixins/edit.py:294). So writing
        against an EMPTY label list — rule 4's read that succeeds carrying no <Label> — would PUT
        just `shortlist` and DELETE `shortlist_sarah`. No `label!=shortlist_sarah` exclude would match
        the row afterwards, so it would be visible to every shared account, and nothing verifies
        hiding after the fact. Skipping a cosmetic label is the only acceptable answer.
        """
        from shortlist.engine.delivery import _apply_shortlist_label

        plex = MagicMock(spec=PlexClient)

        plex.fetch_items.return_value = ([], [])
        blind = MagicMock()
        blind.title = "✨ Movies Picked for You"
        blind.labels = []  # Plex answered, and said this row has no labels at all

        _apply_shortlist_label(plex, blind, "sarah")

        # Writing here would replace the label set rather than add to it.
        plex.stored_label.assert_not_called()

    def test_it_writes_when_the_owner_label_is_present(self):
        from shortlist.engine.delivery import _apply_shortlist_label

        plex = MagicMock(spec=PlexClient)

        plex.fetch_items.return_value = ([], [])
        collection = MagicMock()
        collection.title = "✨ Movies Picked for You"
        collection.labels = [SimpleNamespace(tag="Shortlist_sarah")]

        _apply_shortlist_label(plex, collection, "sarah")

        assert plex.stored_label.call_args.args[1] == "shortlist"

    def test_it_does_not_write_again_once_the_label_is_there(self):
        """Steady state must cost nothing. On a PMS answering writes in ~17s, a needless write per
        row per night is the difference between a quiet night and a long one."""
        from shortlist.engine.delivery import _apply_shortlist_label

        plex = MagicMock(spec=PlexClient)

        plex.fetch_items.return_value = ([], [])
        collection = MagicMock()
        collection.title = "✨ Movies Picked for You"
        collection.labels = [SimpleNamespace(tag="Shortlist_sarah"), SimpleNamespace(tag="Shortlist")]

        _apply_shortlist_label(plex, collection, "sarah")

        plex.stored_label.assert_not_called()


class TestTheOwnerPrefixIsLoadBearing:
    """Every lookup that derives an OWNER from a label matches `shortlist_` with the underscore.

    Loosen any of them to bare `shortlist` and the constant label — which is on every row — matches
    first and yields an EMPTY slug. What each site then does with that is severe and different, so
    they are pinned against the real functions rather than against the string.
    """

    def test_owned_collections_does_not_treat_the_constant_label_as_a_users_row(self):
        """`owned_collections` feeds `stored_labels`, which becomes every account's `label!=` excludes.

        Match on bare `shortlist` and `Shortlist` enters that map, so `label!=Shortlist` is merged
        into every share filter — hiding EVERY Shortlist row from EVERY shared user. It over-hides
        rather than leaking, but it is permanent: the prune path also matches on `shortlist_`, so
        Shortlist can write that exclude and then neither see nor remove it. Only a snapshot restore
        would clear it.
        """
        client = PlexClient.__new__(PlexClient)
        # The constant label FIRST, so a loosened prefix would match it before the owner's.
        ours = SimpleNamespace(
            title="✨ Movies Picked for You",
            ratingKey=9001,
            labels=[SimpleNamespace(tag="Shortlist"), SimpleNamespace(tag="Shortlist_sarah")],
        )
        client._section_collections = lambda _section: [ours]
        client.sections = lambda: [SimpleNamespace(title="Movies")]

        owned = client.owned_collections("shortlist")

        assert set(owned) == {"sarah"}, "the constant label names nobody and must not become a slug"
        assert "" not in owned, "an empty slug here becomes `label!=Shortlist` on every share filter"
        assert owned["sarah"].label == "Shortlist_sarah"

    def test_sweep_reads_the_owner_from_the_real_label_not_the_constant_one(self, engine_config, movies):
        """`sweep_broken_rows` is the OTHER path that turns a label into an owner and then DELETES.

        Driven through the real function: a row carrying the constant label FIRST must still be
        attributed to sarah. An empty slug here belongs to nobody, which is what that path removes.
        """
        from shortlist.engine.delivery import sweep_broken_rows

        collection = MagicMock()
        collection.title = "✨ Movies Picked for You" + row_marker(100)
        collection.ratingKey = 9001
        collection.labels = [SimpleNamespace(tag="Shortlist"), SimpleNamespace(tag="Shortlist_sarah")]
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.owned_collections.return_value = {}
        plex._section_collections = lambda _s: [collection]
        plex.matches_section.return_value = True

        swept = sweep_broken_rows(plex, engine_config, dry_run=True)

        assert "" not in swept, "an empty owner slug is a deletion candidate and must never appear"
        assert set(swept) <= {"sarah"}, "a row is attributed to its OWNER, never to the constant label"


class TestTheConstantLabelCannotSelectEveryRow:
    """`find_owned_collections` matches a tag EXACTLY, and every row now carries the bare
    `shortlist` label — so a caller passing it would select every Shortlist collection on the
    server. No caller builds that label today; these are the guards that keep it harmless."""

    def test_removal_refuses_the_bare_constant_label(self, engine_config: EngineConfig, movies):
        """The unrecoverable one: this function DELETES."""
        from shortlist.engine.delivery import remove_row_collections

        plex = MagicMock(spec=PlexClient)

        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.find_owned_collections.return_value = [MagicMock(title="✨ Movies Picked for You")]

        removed = remove_row_collections(plex, engine_config, label="shortlist", displays=None, dry_run=False)

        assert removed == []
        plex.find_owned_collections.assert_not_called()
        plex.delete_owned_collection.assert_not_called()

        # But the TITLE-CASED form a caller reading `User.label` would pass must still work — a real
        # removal silently doing nothing leaves the collections on Plex for ever.
        plex.find_owned_collections.return_value = []
        remove_row_collections(plex, engine_config, label="Shortlist_sarah", displays=None, dry_run=True)
        plex.find_owned_collections.assert_called()

    def test_rename_and_poster_reset_refuse_it_too(self, engine_config: EngineConfig, movies):
        from shortlist.engine.delivery import rename_row_collections, reset_row_posters

        plex = MagicMock(spec=PlexClient)

        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]

        assert (
            rename_row_collections(
                plex,
                engine_config,
                label="shortlist",
                marker=row_marker(100),
                old_display="✨ Picked for You",
                new_display="✨ New Name",
                dry_run=False,
            )
            == []
        )
        assert reset_row_posters(plex, engine_config, label="shortlist", displays=None, dry_run=False) == []
        plex.find_owned_collections.assert_not_called()

    def test_they_still_accept_the_title_cased_label_plex_actually_stores(self, engine_config: EngineConfig, movies):
        """The other half of the guard, and the half that was wrong.

        `User.label` holds Plex's title-cased `Shortlist_sarah`, so a case-SENSITIVE
        `startswith("shortlist_")` rejects a legitimate caller — and both of these return `[]` on
        rejection, which is indistinguishable from "nothing matched". A rename would leave the row
        under its old title and a poster reset would leave the old artwork, with a warning in the log
        and no error anywhere the operator looks. Removal already had the `.lower()`; these two were
        edited in the same commit and did not.
        """
        from shortlist.engine.delivery import rename_row_collections, reset_row_posters

        plex = MagicMock(spec=PlexClient)

        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.find_owned_collections.return_value = []

        rename_row_collections(
            plex,
            engine_config,
            label="Shortlist_sarah",
            marker=row_marker(100),
            old_display="✨ Picked for You",
            new_display="✨ New Name",
            dry_run=True,
        )
        reset_row_posters(plex, engine_config, label="Shortlist_sarah", displays=None, dry_run=True)

        # Reached the search rather than being turned away at the door — asserted per function, since
        # one of the two passing would otherwise hide the other failing.
        assert plex.find_owned_collections.call_count == 2
        assert {c.args[1] for c in plex.find_owned_collections.call_args_list} == {"Shortlist_sarah"}


class TestAConflictingRenameDoesNotTakeThePersonDown:
    """A Plex collection is keyed by TITLE within a library, so renaming onto a title that already
    exists there answers 409 Conflict.

    The rebuild path deletes first precisely to avoid this. The in-place rename did not, and an
    unguarded `editTitle` propagated — recorded on a real 46-user server (run 4, 2026-08-15):
    `users_ok: 45, users_error: 1`, the one error being

        BadRequest: (409) conflict; …title.value=🎯 Because you watched Ted Lasso…&type=18

    That person got no rows at all that night, over a name. A `{top_seed}` row renames itself every
    time the seed it is named after changes, so it is the row whose title moves onto ground another
    of the same person's rows may already occupy.
    """

    # The REAL message shape. plexapi formats it as `f'({status}) {codename}; {url} {errtext}'`
    # (`plexapi/server.py:752`) with no class-name prefix, and `editTitle`'s url always carries
    # `id=<ratingKey>` — which is exactly why a substring test for "409" is unsafe and the guard
    # anchors to the leading token instead.
    CONFLICT = "(409) conflict; http://pms:32400/library/sections/2/all?id=771&title.value=X&type=18"
    # A NON-409 whose ratingKey happens to contain the digits. Five- and six-digit keys are normal
    # on a real server, so roughly one collection in three hundred can produce this.
    FIVE_HUNDRED_ON_A_409_KEY = "(500) internal_server_error; http://pms:32400/library/sections/2/all?id=40953&type=18"

    def _plex(self, movies, shows):
        plex = MagicMock(spec=PlexClient)
        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies, shows]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies, MediaType.SHOW: shows}
        plex.find_owned_collections.return_value = []
        plex.matches_section.return_value = True
        plex.fetch_items.return_value = ([], [])
        return _labelling_plex_mock(plex)

    def _existing(self, profile, raiser):
        existing = MagicMock()
        existing.title = "Old Name" + row_marker(profile.plex_account_id)
        existing.items.return_value = [MagicMock(title="Movie 1", ratingKey=1001)]
        existing.editTitle.side_effect = raiser
        return existing

    def test_the_row_still_gets_its_titles_when_plex_refuses_the_rename(
        self, engine_config: EngineConfig, movies, shows
    ):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing(profile, Exception(self.CONFLICT))
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        diff, _ = deliver_rows(plex, profile, picks(), engine_config)

        # Membership is the part that matters, and it is written either way.
        existing.editTitle.assert_called_once()
        assert diff.added == ["Movie 2"]
        assert diff.kept == ["Movie 1"]
        plex.set_items.assert_called_once()

    def test_the_old_title_is_kept_so_nothing_becomes_visible_to_anyone_new(
        self, engine_config: EngineConfig, movies, shows
    ):
        """The safe failure. The retained title still carries THIS account's marker, so the row's
        membership stays its own — a rename that silently dropped the marker would merge two
        people's rows into one shared tag, which is the leak the marker exists to prevent."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing(profile, Exception(self.CONFLICT))
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        deliver_rows(plex, profile, picks(), engine_config)

        # Asserted on the title the SUT COMPUTED and tried to write, not on `existing.title` — that
        # is set by this test's own fixture and never touched by the code under test, so asserting
        # it could not fail. A marker dropped from the computed title fails here.
        assert existing.editTitle.call_args.args[0].endswith(row_marker(profile.plex_account_id))

    def test_a_non_409_whose_rating_key_contains_409_still_propagates(self, engine_config: EngineConfig, movies, shows):
        """The cell a substring match gets wrong. `"409" in str(exc)` matched the collection's own
        ratingKey, so a 500 on key 40953 was swallowed AND logged as a title collision — a real
        failure reported as a benign one, in the log line an operator would go on to trust."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing(profile, Exception(self.FIVE_HUNDRED_ON_A_409_KEY))
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        with pytest.raises(Exception, match="500"):
            deliver_rows(plex, profile, picks(), engine_config)

    def test_any_other_plex_error_still_propagates(self, engine_config: EngineConfig, movies, shows):
        """Only the title collision is survivable. Swallowing everything would hide a dead server,
        an expired token or a refused write behind a row that merely looks slightly stale."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing(profile, Exception("(401) unauthorized; http://pms:32400/x"))
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        with pytest.raises(Exception, match="401"):
            deliver_rows(plex, profile, picks(), engine_config)


class TestTheDiffReportsWhatLandedNotWhatWasAsked:
    """Architecture review, 2026-08-18. A partial batch omits dead keys silently, so `diff.added` was
    computed from what we ASKED for: a 25-pick row that lost 2 reported "25 added" while Plex held
    23, and `titles_added` in the run stats inherited the same lie. "Why isn't X in my row when the
    run says it delivered it" is exactly what plex-safety rule 10 exists to make answerable."""

    def test_a_vanished_pick_is_not_reported_as_added(self, engine_config: EngineConfig):
        from shortlist.engine.delivery import deliver_rows
        from shortlist.engine.models import RowSpec

        movies = _section("Movies", "movie", "1")
        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        plex.sections.return_value = [movies]
        plex.find_owned_collections.return_value = []
        kept = MagicMock()
        kept.ratingKey = 101
        # Plex still holds 101; 202 was deleted between the pick being made and delivery.
        plex.fetch_items.return_value = ([kept], [202])

        alive = Pick(1, 101, "Still Here", rank=1, reason="r", media_type=MediaType.MOVIE)
        gone = Pick(2, 202, "Deleted Since", rank=2, reason="r", media_type=MediaType.MOVIE)

        reports = deliver_rows(
            plex,
            make_profile(),
            [alive, gone],
            engine_config,
            RowSpec(slug="picked", name_template="Picked", size=10, media="movie"),
            sections=[movies],
            section_picks={movies.key: [alive, gone]},
            dry_run=False,
        )

        # `deliver_rows` returns (diff, label) — the diff is what the run report and the stats read.
        diff = reports[0] if isinstance(reports, tuple) else reports
        added = diff.added if hasattr(diff, "added") else diff[0].added
        assert "Still Here" in added
        assert "Deleted Since" not in added, "the run must not claim it delivered a title Plex dropped"
