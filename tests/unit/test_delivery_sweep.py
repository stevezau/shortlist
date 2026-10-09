"""Sweeping, finding and removing a row's collections."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from shortlist.engine import delivery
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.delivery import deliver_rows, row_marker, sweep_broken_rows
from shortlist.engine.models import EngineConfig, MediaType
from tests.conftest import make_profile
from tests.unit.delivery_support import _section, movies, picks, shows  # noqa: F401


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

    def test_deletes_a_name_freeing_helper_a_killed_run_left_behind(self, engine_config: EngineConfig, movies, shows):
        from shortlist.engine.delivery import FREED_NAME_HELPER_KEY

        """`_reclaim_orphaned_name` deletes its helper in a `finally`, which a killed process never reaches.
        Nothing else matches the helper to a row, so it would sit on that person's Home for good."""
        marker = row_marker(4242)
        helper = self._collection(movies, "Shortlist_mike", title=f"Shortlist freed name 0123456789ab{marker}")
        plex = self._plex(movies, shows, helper)
        plex.matches_section.return_value = True

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": marker})

        # Not filed under "mike": that is their deleted ROWS, which the run page lists as theirs.
        assert deleted == {f"{FREED_NAME_HELPER_KEY}mike": [helper.title]}
        plex.delete_owned_collection.assert_called_once_with(helper, "shortlist")

    def test_an_unlabelled_helper_is_filed_as_a_helper_not_as_the_persons_row(
        self, engine_config: EngineConfig, movies, shows
    ):
        """Killed between the helper's rename and its label write, it is swept as an unlabelled orphan."""
        from shortlist.engine.delivery import FREED_NAME_HELPER_KEY

        marker = row_marker(4242)
        helper = self._collection(movies, title=f"Shortlist freed name 0123456789ab{marker}")
        healthy = self._collection(movies, "Shortlist_mike", title=f"✨ Picked for You{marker}")
        plex = self._plex(movies, shows, helper, healthy)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.side_effect = lambda c, _prefix: c is helper

        deleted = sweep_broken_rows(
            plex,
            engine_config.__class__(**{**engine_config.__dict__, "orphan_confirm_delay_s": 0}),
            markers={"mike": marker},
        )

        assert deleted == {f"{FREED_NAME_HELPER_KEY}mike": [helper.title]}

    def test_a_row_someone_named_like_a_helper_is_left_alone(self, engine_config: EngineConfig, movies, shows):
        marker = row_marker(4242)
        row = self._collection(movies, "Shortlist_mike", title=f"Shortlist freed name of the week{marker}")
        plex = self._plex(movies, shows, row)
        plex.matches_section.return_value = True

        assert sweep_broken_rows(plex, engine_config, markers={"mike": marker}) == {}

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
        # `label!=` can hide a label-less collection, so it leaks to EVERY user — the production incident.
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

        # TWO confirms, not one. This asserted `assert_called_once_with` until the lone-candidate
        # bypass was removed: one confirm was enough to authorise a delete, and on a single-row
        # server one hiccup answering both the listing read and that confirm destroyed a real row.
        # Both reads must agree, separated by `orphan_confirm_delay_s` (0 here, so no wall clock is
        # spent in tests).
        assert plex.confirm_unlabelled.call_args_list == [
            call(orphan, "shortlist"),
            call(orphan, "shortlist"),
        ]

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

    def test_confirm_unlabelled_is_required_twice_with_a_real_gap_before_deleting(
        self, engine_config: EngineConfig, movies, shows, monkeypatch
    ):
        """The lone-candidate bypass was the dangerous one, not the systemic case.

        `orphan_candidates <= 1` can only be true when there is at most ONE Shortlist collection on
        the entire server — so it fired exactly when there was nothing to corroborate the read
        against, which is backwards from every other guard here. On a single-row deployment (which
        the documented 5 -> 15 -> 40 rollout guarantees exists for days on every install) one PMS
        hiccup during the sweep answered both reads "no label", and a genuine, months-old, correctly
        labelled row was deleted permanently while the run reported success.

        A single confirm cannot tell the two apart, so nothing here trusts a single confirm any more:
        two independent reads, separated by real wall-clock time. A transient hiccup clears; a
        genuine orphan's label never arrives however long you wait.
        """
        orphan = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, orphan)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = True
        engine_config.orphan_confirm_delay_s = 30.0
        slept: list[float] = []
        monkeypatch.setattr(delivery.time, "sleep", slept.append)

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

        assert deleted == {"mike": [orphan.title]}
        assert plex.confirm_unlabelled.call_count == 2, "a single confirm must never authorise a delete"
        assert slept == [30.0], "the two confirms must be separated by a real gap, not back-to-back"

    def test_a_row_that_reads_as_labelled_on_the_second_look_is_spared(
        self, engine_config: EngineConfig, movies, shows, monkeypatch
    ):
        """The case the delay exists for: the first read missed the label, the second one sees it.
        Before the fix this row was deleted on the strength of one read."""
        established = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, established)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.side_effect = [True, False]  # transient miss, then the truth
        engine_config.orphan_confirm_delay_s = 5.0
        monkeypatch.setattr(delivery.time, "sleep", lambda _s: None)

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

        assert deleted == {}, "deleted a row the server said was labelled"
        plex.delete_owned_collection.assert_not_called()

    def test_the_second_confirm_is_not_even_attempted_when_the_first_says_labelled(
        self, engine_config: EngineConfig, movies, shows
    ):
        """Fail closed on the cheap answer — no delay, no second round-trip, for the common case."""
        established = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
        plex = self._plex(movies, shows, established)
        plex.matches_section.return_value = True
        plex.confirm_unlabelled.return_value = False

        deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

        assert deleted == {}
        assert plex.confirm_unlabelled.call_count == 1

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

        assert strip_marker("Picked for You" + row_marker(1000001)) == "Picked for You"
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

    def test_a_top_seed_rows_fallback_title_never_selects_a_collection(self):
        """With no picks a `{top_seed}` row renders its FALLBACK name, but a seeded person's collection wears
        "Because you watched X". Matched on the fallback, removal would find nothing of this row's and could
        delete a sibling row that happens to carry that title, so only the ledger may select one."""
        from shortlist.engine.delivery import remove_row, row_marker
        from shortlist.engine.models import CollectionDiff, EngineConfig, RowSpec, UserProfile, UserType

        sibling = self._collection("Weekend picks" + row_marker(100), 777)
        plex, sections, deleted = self._plex([sibling])

        remove_row(
            plex,
            UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah"),
            EngineConfig(),
            RowSpec(
                slug="because", name_template="Because you watched {top_seed}", size=5, fallback_name="Weekend picks"
            ),
            dry_run=False,
            diff=CollectionDiff(),
            sections=sections,
            delivered_keys={},
        )

        assert deleted == []

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
