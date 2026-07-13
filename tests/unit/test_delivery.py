from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rowarr.engine.clients.plex_pms import PlexClient
from rowarr.engine.delivery import DEFAULT_ROW_NAME, deliver_rows, render_row_name, row_marker, sweep_broken_rows
from rowarr.engine.models import EngineConfig, MediaType, Pick
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

    def test_unfillable_template_falls_back_to_the_default_row_name(self):
        cold = [Pick(1, 1, "X", 1, "r", MediaType.MOVIE)]
        assert render_row_name("{top_seed}", make_profile(), cold) == DEFAULT_ROW_NAME


class TestColdStartRowName:
    """A cold-start user has no seed — the row must not read 'Because you watched'."""

    def test_seeded_user_gets_the_dynamic_title(self):
        name = render_row_name("Because you watched {top_seed}", make_profile(), [_named_pick("Fargo")])
        assert name == "Because you watched Fargo"

    def test_cold_start_user_falls_back_instead_of_dangling(self):
        assert (
            render_row_name("Because you watched {top_seed}", make_profile(), [_named_pick(None)]) == DEFAULT_ROW_NAME
        )
        assert render_row_name("Because you watched {top_seed}", make_profile(), []) == DEFAULT_ROW_NAME

    def test_static_template_is_untouched(self):
        assert render_row_name("✨ Picked for You", make_profile(), [_named_pick(None)]) == "✨ Picked for You"


class TestDeliverRows:
    """Delivery is split by media type because Plex collections belong to exactly one library.

    The matrix that matters is the pick mix: movies only, shows only, both, and neither — the
    "both" and "neither" cells are the ones that leaked on a live server.
    """

    def _plex(self, movies: MagicMock, shows: MagicMock) -> MagicMock:
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies, shows]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies, MediaType.SHOW: shows}
        plex.find_owned_collections.return_value = []
        plex.matches_section.return_value = True
        plex.stored_label.return_value = "Rowarr_sarah"
        return plex

    def test_creates_collection_when_missing(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)

        diff, stored = deliver_rows(plex, make_profile(), picks(), engine_config)

        assert diff.created is True
        assert diff.added == ["Movie 1", "Movie 2"]
        assert stored == "Rowarr_sarah"
        plex.fetch_items.assert_called_once_with([1001, 1002])
        create = plex.create_collection.call_args
        assert create.args[0] is movies
        # The title Plex is given carries an INVISIBLE per-account marker. Without it every user's
        # row is the same collection tag in that library, holding everyone's picks.
        assert create.args[1] == "✨ Picked for You" + row_marker(make_profile().plex_account_id)
        assert create.args[1].startswith("✨ Picked for You"), "what a human reads must not change"
        # The report shows the human title — the marker is Plex's business, not the owner's.
        assert diff.collection_title == "✨ Picked for You"
        assert plex.stored_label.call_args.args[1] == "rowarr_sarah"
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
        # Both collections carry the SAME label — that one label is what every other user's
        # share filter excludes, so a second label would leave one of the two rows visible.
        assert [c.args[1] for c in plex.stored_label.call_args_list] == ["rowarr_sarah", "rowarr_sarah"]

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
        existing.items.return_value = [MagicMock(title="Movie 1"), MagicMock(title="Stale Movie")]
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        diff, _ = deliver_rows(plex, profile, picks(), engine_config)

        assert diff.created is False
        assert diff.added == ["Movie 2"]
        assert diff.removed == ["Stale Movie"]
        assert diff.kept == ["Movie 1"]
        plex.delete_owned_collection.assert_not_called()  # its tag is not shared: no rebuild needed
        existing.editTitle.assert_called_once_with("✨ Picked for You" + row_marker(profile.plex_account_id))
        # The items actually pushed, not just "set_items happened": feeding one library's picks
        # into the other library's collection would otherwise pass this test.
        plex.fetch_items.assert_called_once_with([1001, 1002])
        assert plex.set_items.call_args.args == (existing, plex.fetch_items.return_value)

    def test_dry_run_makes_zero_writes(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)

        diff, stored = deliver_rows(plex, make_profile(), picks(), engine_config, dry_run=True)

        assert diff.created is True
        assert stored == "rowarr_sarah"  # requested form; nothing was written to read back
        plex.create_collection.assert_not_called()
        plex.set_items.assert_not_called()
        plex.stored_label.assert_not_called()
        plex.promote.assert_not_called()

    def test_picks_for_a_library_the_server_lacks_are_dropped(self, engine_config: EngineConfig, movies):
        """A movies-only server must not crash on a show pick — it just can't deliver it."""
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        plex.find_owned_collections.return_value = []
        plex.matches_section.return_value = True
        plex.stored_label.return_value = "Rowarr_sarah"

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
        assert stored == "Rowarr_sarah"
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
        back would write `label!=rowarr_sarah` onto every other user's share, and since excludes
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
    """ "Movies" + "4K Movies" is a very common Plex layout. Rows are built in exactly one of
    them, and the choice must not depend on the order the PMS happens to list them in."""

    def test_rows_go_to_the_lowest_keyed_library_of_each_type(self, engine_config: EngineConfig):
        movies, movies_4k = _section("Movies", "movie", 1), _section("4K Movies", "movie", 3)
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies_4k, movies]  # PMS lists 4K first — must not matter
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        plex.find_owned_collections.return_value = []
        plex.matches_section.return_value = True
        plex.stored_label.return_value = "Rowarr_sarah"

        deliver_rows(plex, make_profile(), picks(), engine_config)

        assert plex.create_collection.call_args.args[0] is movies
        assert plex.create_collection.call_count == 1  # never both

    def test_a_well_typed_row_in_the_other_library_is_left_alone(self, engine_config: EngineConfig):
        """It is not the row we maintain, but it still carries its label — so it is still hidden
        from everyone else, and deleting someone's collection we aren't going to replace is not
        our call to make."""
        movies, movies_4k = _section("Movies", "movie", 1), _section("4K Movies", "movie", 3)
        stray = MagicMock()
        stray.title = "✨ Picked for You"
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies, movies_4k]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        plex.find_owned_collections.side_effect = lambda section, label: [stray] if section is movies_4k else []
        plex.matches_section.return_value = True
        plex.stored_label.return_value = "Rowarr_sarah"

        deliver_rows(plex, make_profile(), picks(), engine_config)

        plex.delete_owned_collection.assert_not_called()


class TestSweepBrokenRows:
    """The sweep is the one thing standing between a stranded row and every user on the server.

    It runs server-wide, before any per-user work, on every run. Its whole branch matrix is here
    because nothing else in the suite can catch a regression in it: an earlier version's dry-run
    guard could be deleted — making `--dry-run` destroy real collections — with the entire suite
    still green.
    """

    def _plex(self, movies: MagicMock, shows: MagicMock, *collections: MagicMock) -> MagicMock:
        plex = MagicMock(spec=PlexClient)
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
        stranded = self._collection(movies, "Rowarr_mike")  # a show-subtype row in the movie library
        plex = self._plex(movies, shows, stranded)
        plex.matches_section.return_value = False

        deleted = sweep_broken_rows(plex, engine_config)

        assert deleted == {"mike": ["✨ Picked for You"]}
        plex.delete_owned_collection.assert_called_once_with(stranded, "rowarr")

    def test_leaves_a_well_typed_row_alone(self, engine_config: EngineConfig, movies, shows):
        healthy = self._collection(movies, "Rowarr_mike")
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
        stranded = self._collection(movies, "Rowarr_mike")
        plex = self._plex(movies, shows, stranded)
        plex.matches_section.return_value = False

        deleted = sweep_broken_rows(plex, engine_config, dry_run=True)

        assert deleted == {"mike": ["✨ Picked for You"]}
        plex.delete_owned_collection.assert_not_called()

    def test_sweeps_every_library_and_every_user(self, engine_config: EngineConfig, movies, shows):
        """It is not scoped to tonight's users: a paused user's leaking row is still a leak."""
        stranded_movie = self._collection(movies, "Rowarr_mike")
        stranded_show = self._collection(shows, "Rowarr_sarah", title="Because you watched Fargo")
        plex = self._plex(movies, shows, stranded_movie, stranded_show)
        plex.matches_section.return_value = False

        deleted = sweep_broken_rows(plex, engine_config)

        assert deleted == {"mike": ["✨ Picked for You"], "sarah": ["Because you watched Fargo"]}
        assert plex.delete_owned_collection.call_count == 2

    def test_an_empty_server_is_not_an_error(self, engine_config: EngineConfig, movies, shows):
        plex = self._plex(movies, shows)
        assert sweep_broken_rows(plex, engine_config) == {}


class TestAnUnlabelledRowIsNeverLeftBehind:
    """A collection without a `rowarr_*` label is invisible to Rowarr forever.

    `find_owned_collection`, `owned_collections`, `sweep_unhidable_rows` and uninstall ALL match
    on that label prefix. So a row created but not labelled can never be found, never be hidden
    by a share filter, and never be cleaned up — it just sits there, visible to everyone. Create
    and label must therefore succeed together or not at all.
    """

    def test_a_failure_to_label_deletes_the_row_it_just_created(self, engine_config: EngineConfig):
        movies = _section("Movies", "movie", 1)
        created = MagicMock()
        plex = MagicMock(spec=PlexClient)
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
        plex.sections.return_value = [movies, shows]
        plex.sections_by_type.return_value = {MediaType.MOVIE: movies, MediaType.SHOW: shows}
        plex.matches_section.return_value = True
        plex.stored_label.return_value = "Rowarr_sarah"
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
        plex.sections.return_value = [movies, shows]
        plex.matches_section.return_value = True  # correctly typed: only the TAG is wrong
        movies.collections.return_value = [c for c in collections if c.section is movies]
        shows.collections.return_value = [c for c in collections if c.section is shows]
        return plex

    def _row(self, section: MagicMock, slug: str, title: str) -> MagicMock:
        collection = MagicMock()
        collection.title = title
        collection.section = section
        collection.labels = [SimpleNamespace(tag=f"Rowarr_{slug}")]
        return collection

    def test_a_row_without_its_owners_marker_is_removed(self, engine_config: EngineConfig, movies, shows):
        legacy = self._row(movies, "mike", "✨ Picked for You")
        plex = self._plex(movies, shows, legacy)

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

        assert deleted == {"mike": ["✨ Picked for You"]}
        plex.delete_owned_collection.assert_called_once_with(legacy, "rowarr")

    def test_a_row_with_its_owners_marker_is_left_alone(self, engine_config: EngineConfig, movies, shows):
        healthy = self._row(movies, "mike", "✨ Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, healthy)

        assert sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)}) == {}
        plex.delete_owned_collection.assert_not_called()

    def test_a_row_whose_owner_rowarr_cannot_identify_is_left_alone(self, engine_config: EngineConfig, movies, shows):
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
