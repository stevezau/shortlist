from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.delivery import DEFAULT_ROW_NAME, deliver_rows, render_row_name, row_marker, sweep_broken_rows
from shortlist.engine.models import EngineConfig, MediaType, Pick
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

    def test_unfillable_template_falls_back_to_the_default_row_name(self):
        cold = [Pick(1, 1, "X", 1, "r", MediaType.MOVIE)]
        assert render_row_name("{top_seed}", make_profile(), cold) == DEFAULT_ROW_NAME

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
        plex.stored_label.return_value = "Shortlist_sarah"
        return plex

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
        assert plex.stored_label.call_args.args[1] == "shortlist_sarah"
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
        assert [c.args[1] for c in plex.stored_label.call_args_list] == ["shortlist_sarah", "shortlist_sarah"]

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
            plex.fetch_items.return_value,
            [1001, 1002],
        )
        existing.items.assert_called_once()  # membership read exactly once, not twice

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
        # The ownership-guard prefix is the one the SUT threads from config (rule 4) — not a hardcode.
        assert plex.delete_owned_collection.call_args.args[1] == engine_config.label_prefix
        plex.create_collection.assert_called_once()  # rebuilt via one batched create
        plex.set_items.assert_not_called()  # NOT the per-item update path
        existing.editTitle.assert_not_called()  # nothing to rename — it's being deleted
        plex.fetch_items.assert_called_once_with([1001, 1002])  # the fresh row holds the wanted picks
        assert stored == "Shortlist_sarah"
        assert diff.removed == [f"Stale {k}" for k in range(6)]

    def test_rebuild_threads_the_configured_label_prefix_to_the_delete_guard(self, engine_config, movies, shows):
        """A non-default label_prefix must reach delete_owned_collection — the rule-4 ownership guard —
        so a broken thread can't silently pass by matching the default."""
        from dataclasses import replace as dc_replace

        cfg = dc_replace(engine_config, label_prefix="rowz")
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing_with_stale(profile, 6)
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []

        deliver_rows(plex, profile, picks(), cfg)

        assert plex.delete_owned_collection.call_args.args[1] == "rowz"

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
        assert [call.args[1] for call in plex.stored_label.call_args_list] == ["shortlist_sarah", "shortlist_sarah"]

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

        deleted = sweep_broken_rows(plex, engine_config)

        assert deleted == {"orphan:202": [orphan.title]}
        plex.delete_owned_collection.assert_called_once()

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
