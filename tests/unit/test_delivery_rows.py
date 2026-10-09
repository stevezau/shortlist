"""Delivering rows: naming, creation, reuse and duplicate handling."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from plexapi.exceptions import BadRequest

from shortlist.engine import delivery
from shortlist.engine.clients.plex_pms import CollectionRejectedItems, PlexClient
from shortlist.engine.delivery import DEFAULT_ROW_NAME, deliver_rows, render_row_name, row_marker
from shortlist.engine.models import LABEL_PREFIX, SHARED_LABEL_PREFIX, CollectionDiff, EngineConfig, MediaType, Pick
from tests.conftest import make_profile
from tests.unit.delivery_support import _labelling_plex_mock, _section, movies, picks, shows  # noqa: F401


def _named_pick(seed_title: str | None) -> Pick:
    return Pick(
        tmdb_id=1, rating_key=1, title="Movie", rank=1, reason="r", media_type=MediaType.MOVIE, seed_title=seed_title
    )


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

    def test_a_row_with_no_seeded_pick_is_named_after_the_watch_it_was_built_from(self):
        """Issue #133: discover and web-search picks carry no seed, so a row they filled rendered no name and
        its old collection stayed on Plex. It names the watch the row was built from instead — and a pick
        that does carry a seed still wins, so nothing changes for a row that has one."""
        unseeded = replace(_named_pick(None), lead_seed_title="Obsession")
        seeded = replace(_named_pick("Fargo"), rank=2, lead_seed_title="Obsession")

        assert render_row_name("Because you watched {top_seed}", make_profile(), [unseeded]) == (
            "Because you watched Obsession"
        )
        assert render_row_name("Because you watched {top_seed}", make_profile(), [unseeded, seeded]) == (
            "Because you watched Fargo"
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

    def test_a_library_names_its_own_watch_before_borrowing_the_other_librarys(self, engine_config, movies, shows):
        """Issue #133. The TV picks carry no seed — the show they watched has no look-alikes in the library,
        so discover and web search filled the row — but the row WAS built from that show. It is named after
        it, not after the film the Movies half follows."""
        from shortlist.engine.delivery import deliver_rows, strip_marker
        from shortlist.engine.models import RowSpec

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.return_value = []
        seeded_movie = Pick(1, 101, "Sicario", rank=1, reason="r", media_type=MediaType.MOVIE, seed_title="Conjuring")
        unseeded_show = Pick(
            2, 202, "The Bear", rank=1, reason="r", media_type=MediaType.SHOW, lead_seed_title="The Wire"
        )

        deliver_rows(
            plex,
            make_profile(),
            [seeded_movie, unseeded_show],
            engine_config,
            RowSpec(slug="because", name_template="Because you watched {top_seed}", size=10, media="both"),
            sections=[movies, shows],
            section_picks={movies.key: [seeded_movie], shows.key: [unseeded_show]},
            dry_run=False,
        )

        created = [strip_marker(call.args[1]) for call in plex.create_collection.call_args_list]
        assert created == ["Because you watched Conjuring", "Because you watched The Wire"]

    def test_a_library_with_no_watch_of_its_own_borrows_the_other_librarys(self):
        """#84's case, kept by #133: the row's seeds were all films, so the TV picks carry neither a seed nor
        a lead of their own, and the TV row borrows the film the Movies half was built from."""
        from shortlist.engine.delivery import seed_source

        film_row = [replace(_named_pick(None), lead_seed_title="Obsession")]
        show_row = [replace(_named_pick(None), tmdb_id=2, media_type=MediaType.SHOW)]

        seed_picks = seed_source(show_row, film_row + show_row)

        assert render_row_name("Because you watched {top_seed}", make_profile(), seed_picks) == (
            "Because you watched Obsession"
        )

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
        # ONE labelling call carrying BOTH labels, not two. A label PUT costs ~9.3s on a large
        # library whatever it carries, so the second write was 10% of a 46-user run; the row is new,
        # so there are no existing labels a combined write could drop.
        assert [(c.args[1], c.kwargs.get("extra")) for c in plex.stored_label.call_args_list] == [
            ("shortlist_sarah", "shortlist"),
        ]
        # Promotion is the pipeline's job, AFTER filters are merged — never delivery's.
        plex.promote.assert_not_called()

    def test_a_new_row_is_hidden_from_library_browse_as_soon_as_it_is_labelled(self, engine_config, movies, shows):
        """A person's FIRST row has no `label!=` exclude in anyone's share filter until the merge phase,
        which waits for every later person's delivery. The browse-hiding
        mode promote() sets is applied as soon as the label lands instead; promote() sets it again."""
        plex = self._plex(movies, shows)

        deliver_rows(plex, make_profile(), picks(), engine_config)

        created = plex.create_collection.return_value
        plex.hide_from_browse.assert_called_once_with(created)
        names = [c[0] for c in plex.mock_calls]
        assert names.index("stored_label") < names.index("hide_from_browse")

    def test_a_row_is_created_even_when_hiding_it_from_browse_fails(self, engine_config, movies, shows):
        """Best-effort: promote() hides it again after the filters merge, so a failed early hide is no
        worse than before — and must not cost the person their row."""
        plex = self._plex(movies, shows)
        plex.hide_from_browse.side_effect = RuntimeError("(500) internal_server_error")

        diff, stored = deliver_rows(plex, make_profile(), picks(), engine_config)

        assert diff.created is True
        assert stored == "Shortlist_sarah"
        plex.create_collection.return_value.delete.assert_not_called()

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

    def test_every_pick_vanishing_before_a_create_names_the_row_instead_of_plexapis_message(
        self, engine_config: EngineConfig, movies, shows
    ):
        """If every pick for a library is deleted from Plex between curation and `fetch_items`,
        `create_collection(section, title, [])` reaches plexapi's `Collection._create`, which raises
        `BadRequest('Must include items to add when creating new collection')` before any request —
        naming neither the person, the row nor the library. The delivery still fails for them tonight
        and heals tomorrow; this only makes the reason legible (found auditing #119, pre-existing)."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        plex.find_owned_collections.return_value = []
        picks = [Pick(1, 1001, "Dune", rank=1, reason="r", media_type=MediaType.MOVIE)]
        plex.fetch_items.return_value = ([], [1001])  # every pick deleted from Plex since curation

        with pytest.raises(RuntimeError) as raised:
            deliver_rows(plex, profile, picks, engine_config)

        message = str(raised.value)
        assert profile.username in message
        assert "Movies" in message
        assert "vanished" in message
        plex.create_collection.assert_not_called()

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

    def test_a_full_turnover_updates_the_row_in_place_and_keeps_its_plex_identity(self, engine_config, movies, shows):
        """Issue #119: a row losing many titles used to be deleted and recreated to save per-item
        removes. The new collection had a new ratingKey, so every tool that keys on it (agregarr's
        custom summary and sort title) lost its settings each time. The row must stay the SAME
        Plex object however much of it changes."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 20)  # every current item is unwanted
        existing.ratingKey = 4242
        stale = existing.items.return_value
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        fetched = [MagicMock(ratingKey=1001), MagicMock(ratingKey=1002)]
        plex.fetch_items.return_value = (fetched, [])
        breakdown: list[dict] = []

        diff, stored = deliver_rows(plex, profile, picks(), engine_config, breakdown=breakdown)

        plex.delete_owned_collection.assert_not_called()
        plex.create_collection.assert_not_called()
        plex.fetch_items.assert_called_once_with([1001, 1002])  # only the delta is fetched
        plex.set_items.assert_called_once_with(existing, stale, fetched, [1001, 1002])
        assert breakdown[0]["rating_key"] == 4242  # the ledger keeps pointing at the same collection
        assert diff.removed == [f"Stale {k}" for k in range(20)]
        assert diff.created is False
        assert stored == "Shortlist_sarah"

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

    def test_an_in_place_update_does_not_report_a_title_plex_dropped(self, engine_config, movies, shows):
        """plex-safety rule 10, update path. Movie 2 was deleted from Plex after it was picked, so the batch
        read omits it: the row must neither ask Plex to hold it nor report it added, because
        `titles_added` is the sum of `diff.added`."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 1)
        stale = existing.items.return_value
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        alive = MagicMock(ratingKey=1001)
        plex.fetch_items.return_value = ([alive], [1002])

        diff, _ = deliver_rows(plex, profile, picks(), engine_config)

        plex.create_collection.assert_not_called()
        plex.set_items.assert_called_once_with(existing, stale, [alive], [1001])
        assert diff.added == ["Movie 1"]
        assert diff.removed == ["Stale 0"]

    def test_a_rebuilt_row_does_not_report_a_title_plex_dropped(self, engine_config, movies, shows):
        """plex-safety rule 10, rebuild path: a row Plex refuses every item for is recreated, and the
        recreated row holds only what Plex still has."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 0)
        existing.items.return_value = []
        existing.childCount = 0
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.set_items.side_effect = CollectionRejectedItems("(400) bad_request; .../collections/9/items")
        plex.fetch_items.return_value = ([MagicMock(ratingKey=1001)], [1002])

        diff, _ = deliver_rows(plex, profile, picks(), engine_config)

        plex.delete_owned_collection.assert_called_once()
        assert plex.create_collection.call_args.args[2] == [plex.fetch_items.return_value[0][0]]
        assert diff.added == ["Movie 1"]

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
        swallowed 500s and 401s in `rename_or_keep`."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 0)
        existing.items.return_value = []
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.set_items.side_effect = RuntimeError("(500) internal; http://pms/library/collections/1400/items")

        with pytest.raises(RuntimeError, match="500"):
            deliver_rows(plex, profile, picks(), engine_config)
        plex.delete_owned_collection.assert_not_called()

    def test_a_dry_run_update_writes_and_announces_nothing(self, engine_config, movies, shows):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 6)
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        events: list = []

        deliver_rows(plex, profile, picks(), engine_config, dry_run=True, on_write=events.append)

        plex.set_items.assert_not_called()
        plex.delete_owned_collection.assert_not_called()
        plex.create_collection.assert_not_called()
        assert events == []

    def test_says_what_it_will_add_and_remove_before_updating_a_row(self, engine_config, movies, shows):
        """Plex removes titles one DELETE at a time, so an in-place update on a big library runs for
        minutes — the run page is told what is about to change BEFORE the writes start, not after."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 3)
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.fetch_items.return_value = ([MagicMock(ratingKey=1001), MagicMock(ratingKey=1002)], [])
        events: list = []
        plex.set_items.side_effect = lambda *args: events.append("set_items")

        deliver_rows(plex, profile, picks(), engine_config, on_write=events.append)

        assert events == [
            {"row": "✨ Movies Picked for You", "library": "Movies", "adding": 2, "removing": 3},
            "set_items",
        ]

    def test_the_announced_add_count_leaves_out_picks_that_have_vanished(self, engine_config, movies, shows):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 1)
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.fetch_items.return_value = ([MagicMock(ratingKey=1001)], [1002])  # Movie 2 deleted from Plex
        events: list = []

        deliver_rows(plex, profile, picks(), engine_config, on_write=events.append)

        assert events == [{"row": "✨ Movies Picked for You", "library": "Movies", "adding": 1, "removing": 1}]

    def test_announces_nothing_when_every_title_to_add_has_vanished_and_nothing_is_removed(
        self, engine_config, movies, shows
    ):
        """An announcement with nothing to add and nothing to remove would render as a line with no verb."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = MagicMock()
        existing.title = "✨ Movies Picked for You" + row_marker(profile.plex_account_id)
        existing.items.return_value = [MagicMock(title="Movie 1", ratingKey=1001)]
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.fetch_items.return_value = ([], [1002])  # the one title to add was deleted from Plex
        events: list = []

        deliver_rows(plex, profile, picks(), engine_config, on_write=events.append)

        assert events == []

    def test_the_announced_new_row_size_leaves_out_picks_that_have_vanished(self, engine_config, movies, shows):
        plex = self._plex(movies, shows)
        plex.fetch_items.return_value = ([MagicMock(ratingKey=1001)], [1002])
        events: list = []

        deliver_rows(plex, make_profile(), picks(), engine_config, on_write=events.append)

        assert events == [{"row": "✨ Movies Picked for You", "library": "Movies", "creating": 1}]

    def test_says_it_is_creating_the_row_when_a_broken_one_has_to_be_recreated(self, engine_config, movies, shows):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 0)
        existing.items.return_value = []
        existing.childCount = 0
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.set_items.side_effect = CollectionRejectedItems("(400) bad_request; .../collections/9/items")
        plex.fetch_items.return_value = ([MagicMock(ratingKey=1001), MagicMock(ratingKey=1002)], [])
        events: list = []

        deliver_rows(plex, profile, picks(), engine_config, on_write=events.append)

        assert events[-1] == {"row": "✨ Movies Picked for You", "library": "Movies", "creating": 2}

    def test_says_it_is_creating_a_row_before_creating_it(self, engine_config, movies, shows):
        plex = self._plex(movies, shows)
        plex.fetch_items.return_value = ([MagicMock(ratingKey=1001), MagicMock(ratingKey=1002)], [])
        profile = make_profile()
        events: list = []
        create = plex.create_collection.side_effect

        def create_and_record(*args):
            events.append("create")
            return create(*args)

        plex.create_collection.side_effect = create_and_record

        deliver_rows(plex, profile, picks(), engine_config, on_write=events.append)

        assert events == [{"row": "✨ Movies Picked for You", "library": "Movies", "creating": 2}, "create"]

    def test_reports_a_stored_label_before_the_next_library_is_written(self, engine_config, movies, shows):
        """The caller hides a person's first row the moment its label exists. Waiting for the row's other
        libraries left the first collection listed in everyone's Collections tab while a TV collection was
        created — ~36s on a large library, measured live."""
        plex = self._plex(movies, shows)
        plex.fetch_items.side_effect = lambda keys: ([MagicMock(ratingKey=k) for k in keys], [])
        events: list = []
        create = plex.create_collection.side_effect
        plex.create_collection.side_effect = lambda section, title, items: (
            events.append(("create", section.title)) or create(section, title, items)
        )
        stored_labels: dict[str, str] = {}
        mixed = picks(1) + picks(1, MediaType.SHOW, start=5)

        deliver_rows(
            plex,
            make_profile(),
            mixed,
            engine_config,
            stored_labels=stored_labels,
            on_label_stored=lambda: events.append(("label stored", dict(stored_labels))),
        )

        assert events[0] == ("create", "Movies")
        assert events[1] == ("label stored", {"sarah": "Shortlist_sarah"})
        assert events[2] == ("create", "TV Shows")

    def test_a_library_whose_label_never_landed_reports_nothing(self, engine_config, movies, shows):
        """Reporting it anyway would spend the caller's run-once hide on a row that has no label to exclude."""
        plex = self._plex(movies, shows)
        plex.stored_label.side_effect = RuntimeError("label write failed")
        called: list = []

        with pytest.raises(RuntimeError):
            deliver_rows(
                plex,
                make_profile(),
                picks(),
                engine_config,
                stored_labels={},
                on_label_stored=lambda: called.append(True),
            )

        assert called == []

    def test_a_dry_run_reports_no_stored_label(self, engine_config, movies, shows):
        plex = self._plex(movies, shows)
        called: list = []

        deliver_rows(
            plex,
            make_profile(),
            picks(),
            engine_config,
            stored_labels={},
            dry_run=True,
            on_label_stored=lambda: called.append(True),
        )

        assert called == []

    def test_says_nothing_when_the_row_is_unchanged(self, engine_config, movies, shows):
        """An unchanged row writes nothing, so announcing a write would be a lie."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        unchanged = MagicMock()
        unchanged.title = "✨ Movies Picked for You" + row_marker(profile.plex_account_id)
        unchanged.items.return_value = [
            MagicMock(title="Movie 1", ratingKey=1001),
            MagicMock(title="Movie 2", ratingKey=1002),
        ]
        plex.find_owned_collections.side_effect = lambda section, label: [unchanged] if section is movies else []
        events: list = []

        deliver_rows(plex, profile, picks(), engine_config, on_write=events.append)

        assert events == []

    def test_a_dry_run_create_announces_nothing(self, engine_config, movies, shows):
        plex = self._plex(movies, shows)
        events: list = []

        deliver_rows(plex, make_profile(), picks(), engine_config, dry_run=True, on_write=events.append)

        assert events == []

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


class TestSharedRowDuplicates:
    """A rename before 2026-09-15 stripped a shared row's `row_marker(0)`, so the next run could not find
    that collection and built a second, MARKED one beside it — and both carry the shared label, so both
    stayed on Home (#124 review).

    The loser is removed HERE and not in `sweep_broken_rows`. This is the one place that has already
    resolved which collection IS the row — by exact title match, or by the ledger's ratingKey — so the
    duplicate is identified by IDENTITY rather than by a title suffix. An Architecture Review blocked the
    sweep version on 2026-09-18: there, a leftover name-freeing helper carrying the shared label and
    `row_marker(0)` counted as the sibling authorising the delete, and the live row went with it.
    """

    SHARED_LABEL = f"{SHARED_LABEL_PREFIX}popular"

    def _spec(self):
        from shortlist.engine.models import RowSpec

        return RowSpec(slug="popular", name_template="Popular on Home Server", size=5, shared=True, media="movie")

    def _collection(self, title: str, key: int) -> MagicMock:
        collection = MagicMock(ratingKey=key)
        collection.title = title
        collection.items.return_value = []
        collection.labels = [SimpleNamespace(tag=self.SHARED_LABEL)]
        return collection

    def _plex(self, movies, shows, *owned: MagicMock) -> MagicMock:
        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies, MediaType.SHOW: shows}
        plex.matches_section.return_value = True
        plex.find_owned_collections.side_effect = lambda section, label: list(owned) if section is movies else []
        return plex

    def _deliver(self, plex, engine_config, movies, shows, dry_run: bool = False, **kwargs):
        picks = [Pick(1, 1001, "Dune", rank=1, reason="r", media_type=MediaType.MOVIE)]
        return deliver_rows(
            plex,
            make_profile(),
            picks,
            engine_config,
            self._spec(),
            dry_run=dry_run,
            sections=[movies, shows],
            **kwargs,
        )

    def _deleted(self, plex) -> list[int]:
        return [c.args[0].ratingKey for c in plex.delete_owned_collection.call_args_list]

    def test_the_unmarked_duplicate_is_deleted_and_the_resolved_row_is_kept(self, engine_config, movies, shows):
        live = self._collection("Popular on Home Server" + row_marker(0), 100)
        stale = self._collection("Popular on Home Server", 200)
        plex = self._plex(movies, shows, live, stale)

        self._deliver(plex, engine_config, movies, shows)

        assert self._deleted(plex) == [200], "the unmarked duplicate must go and the resolved row must stay"

    def test_the_removed_duplicate_reaches_the_diff_the_run_audits(self, engine_config, movies, shows):
        """A collection destroyed on someone's real server has to be answerable from the UI, not just from
        a container log that rotates in days (plex-safety rule 10). `duplicates_removed` is what carries it
        into the events row and the library's breakdown on the run page."""
        live = self._collection("Popular on Home Server" + row_marker(0), 100)
        stale = self._collection("Popular on Home Server", 200)
        plex = self._plex(movies, shows, live, stale)
        breakdown: list[dict] = []

        diff, _ = self._deliver(plex, engine_config, movies, shows, breakdown=breakdown)

        assert diff.duplicates_removed == ["Popular on Home Server"], "the delete must be in the audited diff"
        assert [entry["duplicates_removed"] for entry in breakdown] == [["Popular on Home Server"]], (
            "and in the breakdown of the library it happened in"
        )
        assert plex.delete_owned_collection.call_args.args[1] == LABEL_PREFIX, (
            "the label prefix is the ownership proof delete_owned_collection checks"
        )

    def test_a_removed_duplicate_is_not_reported_as_a_deleted_row(self, engine_config, movies, shows):
        """`deleted` means the row itself is gone — the run page renders it as "Row deleted (this person no
        longer gets this row)". After a duplicate is removed the row is still live, so reporting it there
        contradicts the picks shown beside it (v1.9.2 release review, MED)."""
        live = self._collection("Popular on Home Server" + row_marker(0), 100)
        stale = self._collection("Popular on Home Server", 200)
        plex = self._plex(movies, shows, live, stale)
        breakdown: list[dict] = []

        diff, _ = self._deliver(plex, engine_config, movies, shows, breakdown=breakdown)

        assert self._deleted(plex) == [200], "precondition: the duplicate really was removed"
        assert diff.deleted == []
        assert [entry["deleted"] for entry in breakdown] == [[]]

    def test_a_failed_delete_never_costs_the_audience_their_row(self, engine_config, movies, shows):
        """The "never raises" promise is the whole reason this lives in `_deliver_one` rather than in
        `sweep_broken_rows`, where a raise aborts the entire run. Nothing else exercises that branch, so
        narrowing the `except` later would go unnoticed until a PMS hiccup took out a shared row."""
        live = self._collection("Popular on Home Server" + row_marker(0), 100)
        stale = self._collection("Popular on Home Server", 200)
        plex = self._plex(movies, shows, live, stale)
        plex.delete_owned_collection.side_effect = BadRequest("(500) internal_server_error")

        diff, stored = self._deliver(plex, engine_config, movies, shows)

        assert stored, "the row itself must still have been delivered"
        # The duplicate is still on Plex, so the audit must not say it was removed; the WARNING says why.
        assert diff.duplicates_removed == [], "a delete that failed must not be reported as done"
        assert diff.deleted == []

    def test_a_removed_duplicate_is_audited_even_when_the_rows_own_write_then_fails(self, engine_config, movies, shows):
        """The cleanup runs before the row's membership write, and a shared row has no retry wrapper: if
        that write raises, `_deliver_one` never returns its diff. The delete has already happened on Plex,
        so it has to be in the caller's accumulator — the diff `_run_shared` keeps on its report and writes
        to the `run.shared` events row whatever the outcome (plex-safety rule 10)."""
        live = self._collection("Popular on Home Server" + row_marker(0), 100)
        stale = self._collection("Popular on Home Server", 200)
        plex = self._plex(movies, shows, live, stale)
        plex.set_items.side_effect = BadRequest("(500) internal_server_error")
        accumulator = CollectionDiff()

        with pytest.raises(BadRequest):
            self._deliver(plex, engine_config, movies, shows, diff=accumulator)

        assert self._deleted(plex) == [200], "precondition: the duplicate was deleted before the write failed"
        assert accumulator.duplicates_removed == ["Popular on Home Server"]

    def test_a_name_freeing_helper_under_the_shared_label_is_left_for_the_sweep(self, engine_config, movies, shows):
        """The helper is debris from a stopped run and `sweep_broken_rows` owns it. Deleting it here
        would be the same conflation that made the sweep version destroy live rows."""
        live = self._collection("Popular on Home Server" + row_marker(0), 100)
        helper = self._collection(delivery.FREED_NAME_PREFIX + "f94fded57abc", 300)
        plex = self._plex(movies, shows, live, helper)

        self._deliver(plex, engine_config, movies, shows)

        assert self._deleted(plex) == []

    def test_a_wrong_typed_collection_is_left_for_the_sweep(self, engine_config, movies, shows):
        """Wrong type for its library means no share filter can hide it — that is the sweep's leak case,
        and it deletes it as `unhidable`. Doing it here too would double-delete."""
        live = self._collection("Popular on Home Server" + row_marker(0), 100)
        mistyped = self._collection("Popular on Home Server", 200)
        plex = self._plex(movies, shows, live, mistyped)
        plex.matches_section.side_effect = lambda c, section: c is not mistyped

        self._deliver(plex, engine_config, movies, shows)

        assert self._deleted(plex) == []

    def test_another_MARKED_copy_is_left_alone(self, engine_config, movies, shows):
        """A rename can leave a marked copy under the old title. It is not the shape this cleans up (that
        one is UNMARKED), and every resolution path in `_find_this_rows_collection` requires the marker —
        so refusing to touch a marked collection is what keeps the live row safe, whichever copy resolved."""
        live = self._collection("Popular on Home Server" + row_marker(0), 100)
        old_marked = self._collection("Old Name" + row_marker(0), 200)
        plex = self._plex(movies, shows, live, old_marked)

        self._deliver(plex, engine_config, movies, shows)

        assert self._deleted(plex) == []

    def test_nothing_is_deleted_when_the_row_could_not_be_resolved(self, engine_config, movies, shows):
        """No positive identity, no delete. Both copies are unmarked, so neither is provably the row."""
        one = self._collection("Popular on Home Server", 200)
        two = self._collection("Something Else", 201)
        plex = self._plex(movies, shows, one, two)

        self._deliver(plex, engine_config, movies, shows)

        assert self._deleted(plex) == []

    def test_dry_run_reports_the_duplicate_without_deleting_it(self, engine_config, movies, shows):
        live = self._collection("Popular on Home Server" + row_marker(0), 100)
        stale = self._collection("Popular on Home Server", 200)
        plex = self._plex(movies, shows, live, stale)

        diff, _ = self._deliver(plex, engine_config, movies, shows, dry_run=True)

        plex.delete_owned_collection.assert_not_called()
        assert diff.duplicates_removed == ["Popular on Home Server"], "a dry run reports the would-be delete"

    def test_a_per_person_rows_unmarked_sibling_is_not_touched_here(self, engine_config, movies, shows):
        """Only a SHARED label gets this treatment. A per-person row's marker is its account id, and an
        unmarked collection under a per-person label is the sweep's `shares_tag` leak, not a duplicate."""
        from shortlist.engine.models import RowSpec

        profile = make_profile()
        live = self._collection("✨ Picked for You" + row_marker(profile.plex_account_id), 100)
        stale = self._collection("✨ Picked for You", 200)
        for c in (live, stale):
            c.labels = [SimpleNamespace(tag=f"{LABEL_PREFIX}_{profile.username}")]
        plex = self._plex(movies, shows, live, stale)
        picks = [Pick(1, 1001, "Dune", rank=1, reason="r", media_type=MediaType.MOVIE)]

        deliver_rows(
            plex,
            profile,
            picks,
            engine_config,
            RowSpec(slug="picked", name_template="✨ Picked for You", size=5, media="movie"),
            sections=[movies, shows],
        )

        assert self._deleted(plex) == []
