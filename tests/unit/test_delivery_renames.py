"""Renames, shared titles across rows and libraries, and on-demand reconciles."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, call

import pytest
from plexapi.exceptions import BadRequest

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.delivery import deliver_rows, render_row_name, row_marker
from shortlist.engine.models import LABEL_PREFIX, EngineConfig, MediaType
from tests.conftest import make_profile
from tests.unit.delivery_support import _labelling_plex_mock, _section, movies, picks, shows  # noqa: F401


class TestAConflictingRenameDoesNotTakeThePersonDown:
    """A Plex collection is keyed by TITLE within a library, so renaming onto a title that already
    exists there answers 409 Conflict.

    An unguarded `editTitle` propagated — recorded on a real 46-user server (run 4, 2026-08-15):
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

    @staticmethod
    def _warnings(fn) -> str:
        """loguru sink — this codebase does not log through the stdlib, so `caplog` stays empty."""
        from loguru import logger

        seen: list[str] = []
        sink = logger.add(seen.append, level="WARNING")
        try:
            fn()
        finally:
            logger.remove(sink)
        return "".join(seen)

    @staticmethod
    def _held(rating_key: int, title: str, section_key) -> MagicMock:
        holder = MagicMock(ratingKey=rating_key, title=title)
        holder.librarySectionID = section_key
        return holder

    def _refused_row(self, profile, *, then=None) -> MagicMock:
        """A row whose first rename is refused; `then` is what every later rename does (default: succeed)."""
        collection = MagicMock(ratingKey=771)
        collection.title = "Old Name" + row_marker(profile.plex_account_id)
        collection.editTitle.side_effect = [BadRequest(self.CONFLICT), then]
        return collection

    def _rename(self, plex, collection, target, profile, section, spare="spare"):
        from shortlist.engine.delivery import rename_or_keep

        outcome: list[str] = []
        text = self._warnings(
            lambda: outcome.append(
                rename_or_keep(
                    plex,
                    collection,
                    target,
                    profile,
                    section,
                    label="Shortlist_sarah",
                    marker=row_marker(profile.plex_account_id),
                    spare_item=spare,
                )
            )
        )
        return outcome[0], text

    def test_a_rename_plex_accepts_needs_nothing_else(self, movies):
        from shortlist.engine.delivery import RENAMED

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        collection = MagicMock()

        outcome, _ = self._rename(plex, collection, "New", profile, movies)

        assert outcome == RENAMED
        plex.collections_titled.assert_not_called()
        plex.create_collection.assert_not_called()

    def test_a_name_something_in_this_library_holds_is_its_own_outcome(self, movies):
        """So a caller can say "something there has that name" only when something does."""
        from shortlist.engine.delivery import HELD

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        plex.collections_titled.return_value = [self._held(99887, "New", movies.key)]

        outcome, _ = self._rename(plex, self._refused_row(profile), "New", profile, movies)

        assert outcome == HELD

    def test_freeing_a_name_waits_when_a_caller_says_plex_is_busy(self, movies):
        """A rename from the row editor runs beside the nightly run: its helper holding a name the run is
        about to deliver would move that row's tag away from its title."""
        from shortlist.engine.delivery import DEFERRED, rename_or_keep

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        plex.collections_titled.return_value = []

        outcome = rename_or_keep(
            plex,
            self._refused_row(profile),
            "New",
            profile,
            movies,
            label="Shortlist_sarah",
            marker=row_marker(profile.plex_account_id),
            spare_item="item",
            may_free_name=lambda: False,
        )

        assert outcome == DEFERRED
        plex.create_collection.assert_not_called()

    def test_the_conflict_warning_names_what_is_holding_the_title(self, movies):
        """A name another collection in THIS library really has is the one refusal nothing here can fix,
        and "a collection already has that title" is untriageable on its own. The ratingKey is what makes
        the squatter findable, because the title carries invisible marker characters.
        """
        from shortlist.engine.delivery import HELD

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        target = "New Name" + row_marker(profile.plex_account_id)
        plex.collections_titled.return_value = [self._held(99887, target, movies.key)]
        collection = self._refused_row(profile)

        outcome, text = self._rename(plex, collection, target, profile, movies)

        assert outcome == HELD
        assert "99887" in text, "the ratingKey is the only way to find it in Plex"
        assert "also a Shortlist row" in text
        plex.create_collection.assert_not_called()

    def test_identifying_the_squatter_never_costs_the_row(self, movies):
        """The lookup is diagnostics. A PMS that fails it must not turn a survivable rename into the
        raised exception that once cost a person every row they had."""
        from shortlist.engine.delivery import KEPT

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        plex.collections_titled.side_effect = RuntimeError("PMS down")

        outcome, text = self._rename(plex, self._refused_row(profile), "New Name", profile, movies)

        assert outcome == KEPT
        assert "could not check what holds it" in text
        plex.create_collection.assert_not_called()

    def test_a_name_only_a_twin_in_another_library_has_asks_for_a_rebuild(self, movies):
        """A helper there would share the twin's tag row, and renaming the helper would rename the twin."""
        from shortlist.engine.delivery import REBUILD

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        target = "New Name" + row_marker(profile.plex_account_id)
        plex.collections_titled.return_value = [self._held(4242, target, 2)]

        outcome, _ = self._rename(plex, self._refused_row(profile), target, profile, movies)

        assert outcome == REBUILD
        plex.create_collection.assert_not_called()

    def test_an_orphaned_name_is_freed_and_the_same_row_takes_it(self, movies):
        """The production shape: nothing on the server has the name, a deleted collection's tag row does."""
        from shortlist.engine.delivery import RENAMED

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        marker = row_marker(profile.plex_account_id)
        target = "New Name" + marker
        plex.collections_titled.return_value = []
        helper = MagicMock(ratingKey=5555)
        plex.create_collection.return_value = helper
        collection = self._refused_row(profile)
        order = MagicMock()
        order.attach_mock(plex.stored_label, "label")
        order.attach_mock(helper.editTitle, "helper_rename")
        order.attach_mock(collection.editTitle, "row_rename")
        order.attach_mock(plex.delete_owned_collection, "delete")

        outcome, _ = self._rename(plex, collection, target, profile, movies, spare="an item of the row")

        assert outcome == RENAMED
        plex.create_collection.assert_called_once_with(movies, target, ["an item of the row"])
        steps = [c[0] for c in order.mock_calls]
        # The helper gives the name up BEFORE anything slow: while it holds the row's exact title, a process
        # killed there would leave a labelled collection the next run takes for the row itself.
        assert steps == ["row_rename", "helper_rename", "label", "row_rename", "delete"], steps
        # Labelled with this person's label, as a new row is: hidden from everyone the row is hidden from.
        assert plex.stored_label.call_args == call(helper, "Shortlist_sarah", extra=LABEL_PREFIX)
        assert collection.title == target, "the cached object must carry the name Plex now has"
        freed = helper.editTitle.call_args.args[0]
        assert freed != target and freed.endswith(marker), "the helper must move away under a name that is ours"
        # plexapi leaves the object's title as it was, and `delete_owned_collection` proves ownership by the
        # marker on THAT title: a shared row's unmarked name would leave a helper whose label failed undeletable.
        assert helper.title == freed
        assert collection.editTitle.call_args_list[-1] == call(target)
        plex.delete_owned_collection.assert_called_once_with(helper, LABEL_PREFIX)

    def test_a_rename_plex_accepts_updates_the_cached_title(self, movies):
        """plexapi's `editTitle` does not, and the run's collection cache keeps the object: a later lookup
        in the same run would see the old name (architecture review 2026-09-14)."""
        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        collection = MagicMock()
        collection.title = "Old"

        self._rename(plex, collection, "New", profile, movies)

        assert collection.title == "New"

    def test_a_helper_that_could_not_be_deleted_is_not_claimed_hidden_when_its_label_failed(self, movies):
        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        plex.collections_titled.return_value = []
        helper = MagicMock(ratingKey=5555)
        plex.create_collection.return_value = helper
        plex.stored_label.side_effect = BadRequest("(500) internal_server_error; http://pms/x")
        plex.delete_owned_collection.side_effect = BadRequest("(500) internal_server_error; http://pms/y")
        from loguru import logger

        seen: list[str] = []
        sink = logger.add(seen.append, level="ERROR")
        try:
            self._rename(plex, self._refused_row(profile), "New", profile, movies)
        finally:
            logger.remove(sink)

        assert "5555" in "".join(seen)
        assert "no one else can see it" not in "".join(seen)

    def test_every_helper_moves_to_a_name_no_earlier_helper_left_behind(self, movies):
        """A freed name stays behind as an orphan, so reusing one would be refused the next time."""
        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        plex.collections_titled.return_value = []
        names = []
        for _ in range(2):
            helper = MagicMock()
            plex.create_collection.return_value = helper
            self._rename(plex, self._refused_row(profile), "New", profile, movies)
            names.append(helper.editTitle.call_args.args[0])

        assert names[0] != names[1]

    def test_the_helper_is_deleted_even_when_the_row_is_refused_again(self, movies):
        from shortlist.engine.delivery import KEPT

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        plex.collections_titled.return_value = []
        helper = MagicMock()
        plex.create_collection.return_value = helper
        collection = self._refused_row(profile, then=BadRequest(self.CONFLICT))

        outcome, text = self._rename(plex, collection, "New", profile, movies)

        assert outcome == KEPT
        plex.delete_owned_collection.assert_called_once_with(helper, LABEL_PREFIX)
        assert "freeing it failed" in text

    def test_a_shared_rows_helper_whose_label_failed_is_still_deleted(self, movies):
        """Review 2026-09-14 (HIGH). A shared row is renamed to an unmarked title, so the helper is created
        unmarked; if its label write fails, only the marker on its freed name proves it is ours, and the real
        `delete_owned_collection` reads that from the object's title."""
        from shortlist.engine.delivery import KEPT

        real = PlexClient.__new__(PlexClient)
        real._collections_cache = {}
        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        plex.collections_titled.return_value = []
        plex.stored_label.side_effect = BadRequest("(500) internal_server_error; http://pms/x")
        plex.delete_owned_collection.side_effect = lambda c, prefix: PlexClient.delete_owned_collection(real, c, prefix)
        helper = MagicMock(ratingKey=5555, labels=[])
        helper.title = "Popular on the server"  # what the create was given, unmarked
        plex.create_collection.return_value = helper
        profile = make_profile()
        collection = self._refused_row(profile)

        from shortlist.engine.delivery import rename_or_keep

        outcome = rename_or_keep(
            plex,
            collection,
            "Popular on the server",
            profile,
            movies,
            label="Shortlist__shared_popular",
            marker=row_marker(0),
            spare_item="item",
        )

        assert outcome == KEPT
        helper.delete.assert_called_once()

    def test_a_helper_whose_own_rename_failed_is_still_deleted(self, movies):
        """Created by this very call, so it is ours whatever its title says: a failed rename left it on the
        row's unmarked shared name with no label, which `delete_owned_collection` cannot prove ours."""
        from shortlist.engine.delivery import rename_or_keep

        real = PlexClient.__new__(PlexClient)
        real._collections_cache = {}
        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        plex.collections_titled.return_value = []
        plex.delete_owned_collection.side_effect = lambda c, prefix: PlexClient.delete_owned_collection(real, c, prefix)
        helper = MagicMock(ratingKey=5555, labels=[])
        helper.title = "Popular on the server"
        helper.editTitle.side_effect = BadRequest("(500) internal_server_error; http://pms/x")
        plex.create_collection.return_value = helper
        profile = make_profile()

        rename_or_keep(
            plex,
            self._refused_row(profile),
            "Popular on the server",
            profile,
            movies,
            label="Shortlist__shared_popular",
            marker=row_marker(0),
            spare_item="item",
        )

        helper.delete.assert_called_once()

    def test_a_helper_that_was_never_created_is_not_deleted(self, movies):
        from shortlist.engine.delivery import KEPT

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        plex.collections_titled.return_value = []
        plex.create_collection.side_effect = BadRequest("(400) bad_request; http://pms/library/collections")

        outcome, _ = self._rename(plex, self._refused_row(profile), "New", profile, movies)

        assert outcome == KEPT
        plex.delete_owned_collection.assert_not_called()

    def test_an_empty_row_keeps_its_name_rather_than_creating_an_empty_helper(self, movies):
        from shortlist.engine.delivery import KEPT

        plex = _labelling_plex_mock(MagicMock(spec=PlexClient))
        profile = make_profile()
        plex.collections_titled.return_value = []

        outcome, _ = self._rename(plex, self._refused_row(profile), "New", profile, movies, spare=None)

        assert outcome == KEPT
        plex.create_collection.assert_not_called()

    def test_the_row_still_gets_its_titles_when_plex_refuses_the_rename(
        self, engine_config: EngineConfig, movies, shows
    ):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing(profile, Exception(self.CONFLICT))
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        # Something in this library really has the name: the one refusal that stays refused.
        plex.collections_titled.side_effect = lambda title: [self._held(99887, title, movies.key)]

        diff, _ = deliver_rows(plex, profile, picks(), engine_config)

        # Membership is the part that matters, and it is written either way.
        existing.editTitle.assert_called_once()
        assert diff.added == ["Movie 2"]
        assert diff.kept == ["Movie 1"]
        plex.set_items.assert_called_once()

    def test_a_kept_name_is_what_the_run_reports(self, engine_config: EngineConfig, movies, shows):
        """The run page and the ledger reported the name Plex refused, so that server's run pages showed four
        rows under names they did not have, and the reconcile looks a `{top_seed}` row up by that title."""
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing(profile, Exception(self.CONFLICT))
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.collections_titled.side_effect = lambda title: [self._held(99887, title, movies.key)]

        breakdown: list[dict] = []
        deliver_rows(plex, profile, picks(), engine_config, breakdown=breakdown)

        assert [entry["row_title"] for entry in breakdown] == ["Old Name"]

    def test_a_rebuild_that_fails_to_create_updates_the_old_row_under_its_old_name(
        self, engine_config: EngineConfig, movies, shows
    ):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing(profile, Exception(self.CONFLICT))
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.collections_titled.side_effect = lambda title: [self._held(4242, title, shows.key)]
        plex.create_collection.side_effect = BadRequest("(400) bad_request; http://pms/library/collections")

        diff, _ = deliver_rows(plex, profile, picks(), engine_config)

        plex.delete_owned_collection.assert_not_called()
        plex.set_items.assert_called_once()
        assert diff.added == ["Movie 2"]

    def test_a_rebuild_replaces_the_old_row_with_one_under_the_twins_name(
        self, engine_config: EngineConfig, movies, shows
    ):
        plex = self._plex(movies, shows)
        profile = make_profile()
        existing = self._existing(profile, Exception(self.CONFLICT))
        plex.find_owned_collections.side_effect = lambda section, label: [existing] if section is movies else []
        plex.collections_titled.side_effect = lambda title: [self._held(4242, title, shows.key)]
        rebuilt = MagicMock(ratingKey=6001, labels=[])
        plex.create_collection.return_value = rebuilt

        breakdown: list[dict] = []
        deliver_rows(plex, profile, picks(), engine_config, breakdown=breakdown)

        wanted = plex.create_collection.call_args.args[1]
        assert wanted.endswith(row_marker(profile.plex_account_id)) and not wanted.startswith("Old Name")
        plex.delete_owned_collection.assert_called_once_with(existing, LABEL_PREFIX)
        (entry,) = breakdown
        assert entry["rating_key"] == 6001 and entry["created"] is True

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


class TestATitleAnotherRowBuildsUnderIsNeverThisRows:
    """Issue #121: two of one person's rows may share a title when they build in different libraries.

    All of a person's rows carry one label and marker and are told apart by title, and the removal
    paths scan EVERY library — so muting, disabling or cold-skipping a Movies-only row matched a
    TV-only row's collection by that same title and deleted it. A title another row builds under in a
    library now names THAT row's collection there; only this row's ledger entry can say otherwise.
    """

    MARK = row_marker(100)
    A: ClassVar[dict] = {"slug": "a", "size": 5, "media": "movie"}
    B: ClassVar[dict] = {"slug": "b", "size": 5, "media": "show"}

    def _plex(self, owned_by_section: dict[str, list[tuple[str, int]]]):
        movies, shows = _section("Movies", "movie", "1"), _section("TV Shows", "show", "2")
        deleted: list[str] = []
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.side_effect = lambda section, label: [
            SimpleNamespace(title=title, ratingKey=key) for title, key in owned_by_section[str(section.key)]
        ]
        plex.delete_owned_collection.side_effect = lambda collection, prefix: deleted.append(collection.ratingKey)
        return plex, [movies, shows], deleted

    def _remove(self, plex, sections, a: dict, others: list[dict], delivered_keys: dict[str, int] | None = None):
        from shortlist.engine.delivery import remove_row
        from shortlist.engine.models import CollectionDiff, RowSpec

        spec = RowSpec(**a)
        return remove_row(
            plex,
            make_profile("sarah", account_id=100),
            EngineConfig(),
            spec,
            dry_run=False,
            diff=CollectionDiff(),
            sections=sections,
            delivered_keys=delivered_keys,
            other_rows=[spec, *(RowSpec(**o) for o in others)],
        )

    @pytest.mark.parametrize("template", ["{library_name} Picked For You", "Friday Picks"])
    def test_removing_a_movies_row_leaves_a_tv_row_of_the_same_title_alone(self, template):
        profile = make_profile("sarah", account_id=100)
        a_movies = render_row_name(template, profile, [], library_name="Movies") + self.MARK
        b_shows = render_row_name(template, profile, [], library_name="TV Shows") + self.MARK
        plex, sections, deleted = self._plex({"1": [(a_movies, 11)], "2": [(b_shows, 22)]})

        removed_in = self._remove(
            plex, sections, {**self.A, "name_template": template}, [{**self.B, "name_template": template}], {"1": 11}
        )

        assert deleted == [11], "only row A's own collection may go — B's TV collection wears the same title"
        assert removed_in == ["1"]

    def test_explicit_libraries_claim_their_titles_the_same_way(self):
        plex, sections, deleted = self._plex({"1": [("Friday" + self.MARK, 11)], "2": [("Friday" + self.MARK, 22)]})

        self._remove(
            plex,
            sections,
            {"slug": "a", "size": 5, "name_template": "Friday", "library_keys": ["1"]},
            [{"slug": "b", "size": 5, "name_template": "Friday", "library_keys": ["2"]}],
        )

        assert deleted == [11]

    def test_a_leftover_in_a_library_no_other_row_claims_still_goes_by_title(self):
        """Unchanged, and the reason the scan covers every library: a row narrowed away from TV whose
        narrowing cleanup never ran still has a copy there, and it would otherwise sit on this person's
        Home for ever. Nothing else builds "Friday" in TV, so the title is still this row's."""
        plex, sections, deleted = self._plex({"1": [("Friday" + self.MARK, 11)], "2": [("Friday" + self.MARK, 22)]})

        removed_in = self._remove(
            plex, sections, {**self.A, "name_template": "Friday"}, [{**self.B, "name_template": "Something Else"}]
        )

        assert sorted(removed_in) == ["1", "2"]
        assert sorted(deleted) == [11, 22]

    def test_the_ledger_can_still_name_a_claimed_title_as_this_rows(self):
        plex, sections, deleted = self._plex({"1": [], "2": [("Friday" + self.MARK, 22)]})

        self._remove(
            plex, sections, {**self.A, "name_template": "Friday"}, [{**self.B, "name_template": "Friday"}], {"2": 22}
        )

        assert deleted == [22]

    def test_a_row_that_builds_for_nobody_named_claims_its_fallback_name(self):
        """A `{top_seed}` row with a fallback wears that fallback for anyone with nothing watched — the
        one title of its that can be predicted, so the one it claims."""
        plex, sections, deleted = self._plex({"1": [], "2": [("New Here" + self.MARK, 22)]})

        self._remove(
            plex,
            sections,
            {**self.A, "name_template": "New Here"},
            [{**self.B, "name_template": "Because you watched {top_seed}", "fallback_name": "New Here"}],
        )

        assert deleted == []

    def test_a_row_this_person_is_not_in_the_audience_of_claims_nothing_for_them(self):
        """It builds no collection for them, so its title there cannot be theirs — and claiming it would
        strand this row's leftover copy on their Home for ever."""
        plex, sections, deleted = self._plex({"1": [], "2": [("Friday" + self.MARK, 22)]})

        self._remove(
            plex,
            sections,
            {**self.A, "name_template": "Friday"},
            [{**self.B, "name_template": "Friday", "audience": {999}}],
        )

        assert deleted == [22]

    def test_an_unrenderable_row_is_still_removed_by_ledger_identity(self):
        """Unchanged: a `{top_seed}` row has no title to guard, and the ledger was already its only handle."""
        plex, sections, _deleted = self._plex({"1": [], "2": [("Because you watched Fargo" + self.MARK, 22)]})

        removed_in = self._remove(
            plex, sections, {**self.A, "name_template": "Because you watched {top_seed}"}, [], {"2": 22}
        )

        assert removed_in == ["2"]


class TestRowsCanShareALibrary:
    """The static test the duplicate-title check uses: could two rows ever build in one library?"""

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (("movie", []), ("show", []), False),  # different media types never meet
            (("movie", ["1"]), ("show", ["2"]), False),
            (("movie", ["1"]), ("movie", ["3"]), False),  # two named sets with nothing in common
            (("both", ["1", "2"]), ("both", ["3", "4"]), False),
            (("movie", []), ("movie", ["3"]), True),  # "every movie library" includes library 3
            (("both", []), ("show", ["2"]), True),
            (("movie", ["1"]), ("both", ["1", "2"]), True),
            (("both", []), ("both", []), True),
        ],
    )
    def test_the_matrix(self, a, b, expected):
        from shortlist.engine.delivery import rows_can_share_a_library

        assert rows_can_share_a_library(*a, *b) is expected
        assert rows_can_share_a_library(*b, *a) is expected, "the answer cannot depend on argument order"

    def test_never_says_no_when_delivery_would_put_both_rows_in_one_library(self):
        """Soundness against the function delivery actually uses. A false "no" is the dangerous one:
        it lets two rows take one collection. A false "yes" only refuses a save."""
        from hypothesis import given
        from hypothesis import strategies as st

        from shortlist.engine.delivery import rows_can_share_a_library, target_sections
        from shortlist.engine.models import RowSpec

        keys = st.lists(st.sampled_from(["1", "2", "3", "4"]), unique=True, max_size=4)
        media = st.sampled_from(["movie", "show", "both"])
        kinds = st.lists(st.sampled_from(["movie", "show"]), min_size=4, max_size=4)

        @given(kinds, media, keys, media, keys)
        def check(section_kinds, media_a, keys_a, media_b, keys_b):
            sections = [SimpleNamespace(key=str(i + 1), type=k, title=f"L{i + 1}") for i, k in enumerate(section_kinds)]
            in_a = {s.key for s in target_sections(sections, RowSpec("a", "", 1, media=media_a, library_keys=keys_a))}
            in_b = {s.key for s in target_sections(sections, RowSpec("b", "", 1, media=media_b, library_keys=keys_b))}
            if in_a & in_b:
                assert rows_can_share_a_library(media_a, keys_a, media_b, keys_b)

        check()


class TestOnDemandReconcilesNeverMatchAnotherRowsTitle:
    """Issue #121, the on-demand half: row delete/disable/audience-shrink (`remove_row_collections`)
    and a poster reset (`reset_row_posters`) match by title too, across every library."""

    MARK = row_marker(100)
    CLAIMED: ClassVar[set[tuple[str, str]]] = {("2", "Friday")}  # another of sarah's rows builds "Friday" in TV Shows

    def _plex(self):
        movies, shows = _section("Movies", "movie", "1"), _section("TV Shows", "show", "2")
        a = SimpleNamespace(title="Friday" + self.MARK, ratingKey=11)
        b = SimpleNamespace(title="Friday" + self.MARK, ratingKey=22)
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.side_effect = lambda section, label: [a] if str(section.key) == "1" else [b]
        return plex, a, b

    def test_a_removal_never_matches_a_title_another_row_builds_under(self, engine_config: EngineConfig):
        from shortlist.engine.delivery import remove_row_collections

        plex, a, _b = self._plex()

        removed = remove_row_collections(
            plex,
            engine_config,
            label="shortlist_sarah",
            displays={"Friday"},
            dry_run=False,
            claimed_titles=self.CLAIMED,
        )

        assert removed == ["Friday"]
        plex.delete_owned_collection.assert_called_once_with(a, "shortlist")

    def test_a_ledger_key_still_removes_a_collection_whose_title_is_claimed(self, engine_config: EngineConfig):
        """Callers hand this only UNAMBIGUOUS keys — a ratingKey two rows both hold is dropped upstream
        (`pipeline.identity_map`, `collection_reconcile._ledger_keys`) — so a key here is this row's."""
        from shortlist.engine.delivery import remove_row_collections

        plex, a, b = self._plex()

        remove_row_collections(
            plex,
            engine_config,
            label="shortlist_sarah",
            displays={"Friday"},
            rating_keys={22},
            dry_run=False,
            claimed_titles=self.CLAIMED,
        )

        assert [c.args[0] for c in plex.delete_owned_collection.call_args_list] == [a, b]

    def test_a_poster_reset_never_matches_a_title_another_row_builds_under(self, engine_config: EngineConfig):
        from shortlist.engine.delivery import reset_row_posters

        plex, a, _b = self._plex()

        reset = reset_row_posters(
            plex,
            engine_config,
            label="shortlist_sarah",
            displays={"Friday"},
            dry_run=False,
            claimed_titles=self.CLAIMED,
        )

        assert reset == ["Movies"]
        plex.reset_poster.assert_called_once_with(a)


class TestRetiringASharedRowsPersonalCopies:
    """A row switched to shared is retired as a per-person row, for everyone, every night — so its sweep meets
    every other collection on the server. A title match needs the person's invisible marker, which the shared
    collection (`row_marker(0)`) and anything foreign lack; a ledger-key match has only the label filter, so
    that is run through the real `PlexClient`."""

    MARK = row_marker(100)

    def _remove(self, spec, by_section: dict[str, list], *, ledger=None, other_rows=()) -> list[int]:
        from shortlist.engine.delivery import remove_row
        from shortlist.engine.models import CollectionDiff, RowSpec

        client = PlexClient.__new__(PlexClient)
        client._section_collections = lambda section: by_section[section.key]
        deleted: list[int] = []
        client.delete_owned_collection = lambda collection, prefix: deleted.append(collection.ratingKey)
        sections = [SimpleNamespace(key=key, title=f"Movies {key}", type="movie") for key in by_section]
        remove_row(
            client,
            make_profile("sarah", account_id=100),
            EngineConfig(row_name_template="Picked for You"),
            spec,
            dry_run=False,
            diff=CollectionDiff(),
            sections=sections,
            delivered_keys=ledger or {},
            other_rows=[RowSpec(slug="picked", name_template="", size=5), *other_rows],
        )
        return deleted

    @staticmethod
    def _collection(key: int, title: str, *labels: str) -> SimpleNamespace:
        return SimpleNamespace(ratingKey=key, title=title, labels=[SimpleNamespace(tag=t) for t in labels])

    def test_only_the_stale_copy_goes(self):
        from shortlist.engine.models import RowSpec

        shared = self._collection(900, "Popular Here" + row_marker(0), "shortlist__shared_popular", "shortlist")
        # Another of sarah's rows builds in library 1 under the very same title (issue #121).
        same_title = self._collection(501, "Popular Here" + self.MARK, "shortlist_sarah", "shortlist")
        default = self._collection(502, "Picked for You" + self.MARK, "shortlist_sarah", "shortlist")
        kometa = self._collection(504, "Popular Here", "Kometa")
        stale = self._collection(503, "Popular Here" + self.MARK, "shortlist_sarah", "shortlist")

        deleted = self._remove(
            RowSpec(slug="popular", name_template="Popular Here", size=5),
            {"1": [shared, same_title, default, kometa], "2": [stale]},
            other_rows=[RowSpec(slug="gems", name_template="Popular Here", size=5, library_keys=["1"])],
        )

        assert deleted == [503]

    @pytest.mark.parametrize("key", [900, 504], ids=["shared-collection", "foreign-collection"])
    def test_a_top_seed_copys_ledger_key_cannot_reach_a_collection_under_another_label(self, key: int):
        from shortlist.engine.models import RowSpec

        shared = self._collection(900, "Popular Here" + row_marker(0), "shortlist__shared_popular", "shortlist")
        kometa = self._collection(504, "Popular Here", "Kometa")
        default = self._collection(502, "Picked for You" + self.MARK, "shortlist_sarah", "shortlist")

        deleted = self._remove(
            RowSpec(slug="popular", name_template="Because you watched {top_seed}", size=5),
            {"1": [shared, kometa, default]},
            ledger={"1": key},
        )

        assert deleted == []
