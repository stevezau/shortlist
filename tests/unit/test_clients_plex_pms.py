"""PlexClient: collections, hubs, sections, the promote retry and the timing adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from plexapi.exceptions import BadRequest

from shortlist.engine.clients.plex_pms import (
    MIN_PMS_VERSION,
    CollectionRejectedItems,
    LibraryTitle,
    PlexClient,
    parse_pms_version,
)
from shortlist.engine.models import MediaType, OwnedRow
from tests.conftest import fake_media_item
from tests.unit.clients_support import FIXTURES


class TestPmsVersion:
    def test_parse_strips_build_hash(self):
        assert parse_pms_version("1.43.3.10793-cd55560bb") == (1, 43, 3, 10793)

    def test_min_version_comparison(self):
        assert parse_pms_version("1.43.3.10793-x") >= MIN_PMS_VERSION
        assert parse_pms_version("1.42.1.9999-x") < MIN_PMS_VERSION


class TestPlexClient:
    def test_build_library_index_maps_tmdb_guids(self, mock_plex: PlexClient):
        section = MagicMock()
        section.title = "Movies"
        section.totalSize = 3
        section.all.return_value = [
            fake_media_item(1, "Has Guid", tmdb_id=42),
            fake_media_item(2, "No Guid"),
            SimpleNamespace(ratingKey=3, title="Other Guid", guids=[SimpleNamespace(id="imdb://tt1")]),
        ]
        index = mock_plex.build_library_index(section)
        assert index == {42: 1}

    def test_build_library_index_skips_a_malformed_tmdb_guid(self, mock_plex: PlexClient):
        """A guid whose id isn't a real integer (a bad scrape, a corrupted agent match) must not raise
        out of the whole section scan — every other tolerant spot in this file skips a bad row rather
        than failing the caller, and this was the one place that didn't."""
        section = MagicMock()
        section.title = "Movies"
        section.totalSize = 2
        section.all.return_value = [
            SimpleNamespace(ratingKey=1, title="Malformed", guids=[SimpleNamespace(id="tmdb://not-a-number")]),
            fake_media_item(2, "Good", tmdb_id=99),
        ]
        index = mock_plex.build_library_index(section)
        assert index == {99: 2}

    def test_build_library_index_can_tally_genres_in_the_same_scan(self, mock_plex: PlexClient):
        """The library-genre baseline rides along on the scan that already happens, so measuring a
        person's genre avoidance costs no extra PMS request."""
        from collections import Counter

        section = MagicMock()
        section.title = "Movies"
        section.totalSize = 2
        first = fake_media_item(1, "A", tmdb_id=42)
        first.genres = [SimpleNamespace(tag="Horror"), SimpleNamespace(tag="Thriller")]
        second = fake_media_item(2, "B", tmdb_id=43)
        second.genres = [SimpleNamespace(tag="Horror")]
        section.all.return_value = [first, second]
        counts: Counter[str] = Counter()

        index = mock_plex.build_library_index(section, genre_counts=counts)

        assert index == {42: 1, 43: 2}
        assert counts == Counter({"Horror": 2, "Thriller": 1})

    def test_one_item_with_an_odd_genre_shape_does_not_abort_the_scan(self, mock_plex: PlexClient):
        """Tolerant like every other row-level read here: a bad scrape costs its own genres, never the
        whole section's index."""
        from collections import Counter

        section = MagicMock()
        section.title = "Movies"
        section.totalSize = 2

        class RaisesOnGenres:
            ratingKey = 1
            title = "Bad"
            guids: ClassVar = [SimpleNamespace(id="tmdb://42")]

            @property
            def genres(self):
                raise RuntimeError("a corrupted agent match")

        bad = RaisesOnGenres()
        good = fake_media_item(2, "Good", tmdb_id=43)
        good.genres = [SimpleNamespace(tag="Drama")]
        section.all.return_value = [bad, good]
        counts: Counter[str] = Counter()

        index = mock_plex.build_library_index(section, genre_counts=counts)

        assert index == {42: 1, 43: 2}, "a bad genre read cost the whole index"
        assert counts == Counter({"Drama": 1})

    def test_genre_tallying_is_off_unless_asked_for(self, mock_plex: PlexClient):
        section = MagicMock()
        section.title = "Movies"
        section.totalSize = 1
        item = fake_media_item(1, "A", tmdb_id=42)
        item.genres = [SimpleNamespace(tag="Horror")]
        section.all.return_value = [item]

        assert mock_plex.build_library_index(section) == {42: 1}

    def test_genres_ride_free_on_the_section_listing_but_labels_do_not(self):
        """The asymmetry the genre profile is built on, pinned against a RECORDED real response.

        plexapi lazily re-reads a collection when `.labels` is touched, because a real PMS serves no
        `<Label>` children in a listing — that re-read is the whole reason `plex-safety.md` rule 4
        needs two guards before deleting an orphan. `<Genre>` children ARE served inline, so
        `.genres` is answered from the parsed listing with no second request.

        Asserted here rather than assumed because the cheap library-genre profile depends on it
        entirely: if genres ever stop riding along, the scan silently becomes one HTTP round trip per
        title and the profile has to move to a cached TMDB lookup instead (rule 11).

        `server=None` is the proof: any lazy re-read has nothing to query and raises, so a passing
        `.genres` assertion cannot be an accidental network read.
        """
        from xml.etree import ElementTree

        from plexapi.video import Movie

        xml = (FIXTURES / "pms_in_progress_movies.xml.txt").read_text()
        video = ElementTree.fromstring(xml).find("Video")

        movie = Movie(server=None, data=video)

        assert [g.tag for g in movie.genres] == ["Horror", "Science Fiction"]
        with pytest.raises(AttributeError):
            _ = movie.labels

    def test_stored_label_returns_existing_title_cased_form_without_write(self, mock_plex: PlexClient):
        collection = MagicMock()
        collection.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        assert mock_plex.stored_label(collection, "shortlist_sarah") == "Shortlist_sarah"
        collection.addLabel.assert_not_called()

    def test_stored_label_keeps_the_labels_already_there(self, mock_plex: PlexClient):
        """Adding the constant `shortlist` label must not cost a row its OWNER label.

        `addLabel` is not additive on the wire: plexapi builds the new tag list as
        `collection.labels + [new]` (mixins/edit.py:294) and PUTs it as an ABSOLUTE set. So what
        protects `Shortlist_sarah` is that it is re-sent from the in-memory list — and a row that
        lost it would match no `label!=shortlist_sarah` exclude and be visible to every shared
        account. This asserts the surviving set, which is the thing that actually matters.
        """
        collection = MagicMock()
        collection.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        added: list[str] = []

        def add(labels):
            # `stored_label` always hands plexapi a LIST now (it may carry a second label in the
            # same write); plexapi normalises a bare string to one anyway. What is asserted below is
            # unchanged: the owner label must still be on the row afterwards.
            added.extend(labels)
            # What a real PUT does: the union, written back as the whole set.
            collection.labels = [*collection.labels, *(SimpleNamespace(tag=x.replace("s", "S", 1)) for x in labels)]

        collection.addLabel.side_effect = add

        stored = mock_plex.stored_label(collection, "shortlist")

        assert stored == "Shortlist"
        assert added == ["shortlist"]
        assert [t.tag for t in collection.labels] == ["Shortlist_sarah", "Shortlist"], (
            "the owner label must still be on the collection — it is the only thing hiding this row"
        )

    def test_stored_label_adds_and_reads_back_when_missing(self, mock_plex: PlexClient):
        collection = MagicMock()
        collection.labels = []

        def add(label):
            collection.labels = [SimpleNamespace(tag="Shortlist_sarah")]  # Plex title-cases on write

        collection.addLabel.side_effect = add
        assert mock_plex.stored_label(collection, "shortlist_sarah") == "Shortlist_sarah"
        collection.reload.assert_called()

    def test_delete_refuses_collections_without_shortlist_label(self, mock_plex: PlexClient):
        foreign = MagicMock()
        foreign.title = "Kometa Collection"
        foreign.labels = [SimpleNamespace(tag="Overlay")]
        with pytest.raises(PermissionError, match="not ours"):
            mock_plex.delete_owned_collection(foreign, "shortlist")
        foreign.delete.assert_not_called()

    def test_delete_accepts_an_unlabelled_orphan_that_carries_our_marker(self, mock_plex: PlexClient):
        # An orphan whose label write never landed still carries the invisible 64-char marker, which
        # proves it's ours even with no label — the sweep must be able to delete it (else it leaks).
        from shortlist.engine.delivery import row_marker

        orphan = MagicMock()
        orphan.title = "✨ Movies Picked for You" + row_marker(202)
        orphan.labels = []
        mock_plex.delete_owned_collection(orphan, "shortlist")
        # Demote off every shelf BEFORE deleting, exactly as the labelled path does.
        assert orphan.visibility.return_value.updateVisibility.call_args.kwargs == {
            "recommended": False,
            "home": False,
            "shared": False,
        }
        orphan.delete.assert_called_once()

    def test_the_marker_predicate_matches_delivery_verbatim(self):
        # The orphan-ownership check is duplicated in plex_pms (to avoid an import cycle); if the two
        # marker definitions ever drift, the sweep would find an orphan but delete_owned_collection
        # would refuse it and abort the run. Pin them together so drift can't ship silently.
        from shortlist.engine.clients.plex_pms import has_shortlist_marker
        from shortlist.engine.delivery import has_marker, row_marker

        for title in (
            "✨ Movies Picked for You" + row_marker(202),  # marked → ours
            "Kometa: Best of the 90s",  # foreign → not ours
            "x" + "​" * 63,  # 63 trailing marker chars — one short of a marker
            "x" + "‌" * 65,  # 65 — a valid 64 marker preceded by another zero-width char
        ):
            assert has_shortlist_marker(title) == has_marker(title), title

    def test_delete_demotes_then_deletes_owned(self, mock_plex: PlexClient):
        owned = MagicMock()
        owned.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        mock_plex.delete_owned_collection(owned, "shortlist")
        vis = owned.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": False, "home": False, "shared": False}
        owned.delete.assert_called_once()

    def test_promote_hides_from_library_and_never_defaults_onto_the_owners_home(self, mock_plex: PlexClient):
        """`home` defaults OFF. It is promotedToOwnHome — the SERVER OWNER's Home shelf — and the
        owner has no share filter, so anything that lands there is visible to them with nothing able
        to hide it. Defaulting it on is how every user's row ended up on the owner's Home."""
        collection = MagicMock()
        mock_plex.promote(collection)
        collection.modeUpdate.assert_called_once_with(mode="hide")
        vis = collection.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": True, "home": False, "shared": True}
        vis.reload.return_value.move.assert_not_called()  # not pinned by default

    def test_hide_from_browse_hides_the_collection_and_touches_nothing_else(self, mock_plex: PlexClient):
        collection = MagicMock()

        mock_plex.hide_from_browse(collection)

        collection.modeUpdate.assert_called_once_with(mode="hide")
        assert [c[0] for c in collection.method_calls] == ["modeUpdate"]  # where it is SHOWN is promotion's

    def test_promote_passes_placement_flags_through(self, mock_plex: PlexClient):
        """A library-only row must be hidden from Home and friends' Home — recommended only."""
        collection = MagicMock()
        mock_plex.promote(collection, recommended=True, home=False, shared=False)
        vis = collection.visibility.return_value
        assert vis.updateVisibility.call_args.kwargs == {"recommended": True, "home": False, "shared": False}

    def test_promote_never_moves_the_hub(self, mock_plex):
        """Promotion sets surfaces; it must not set POSITION.

        It used to honour `pin_top` with `hub.reload().move(after=None)` — the one primitive
        `place_rows` documents as unusable on its own (a built-in stuck at the minimum float makes
        everything sent above it land on that value; one rebuild collapsed 72 of 94 hubs). The
        argument is gone, so assert the write is gone with it rather than that the flag is unread.
        """
        import inspect

        from shortlist.engine.clients.plex_pms import PlexClient

        collection = MagicMock()
        mock_plex.promote(collection)

        collection.visibility.return_value.reload.assert_not_called()
        collection.visibility.return_value.move.assert_not_called()
        assert "pin_top" not in inspect.signature(PlexClient.promote).parameters

    def test_owned_collections_maps_slug_to_stored_label_and_id(self, mock_plex: PlexClient):
        ours = MagicMock(ratingKey=571285)
        ours.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        kometa = MagicMock(ratingKey=9)
        kometa.labels = [SimpleNamespace(tag="Overlay")]
        section = MagicMock()
        section.type = "movie"
        section.collections.return_value = [ours, kometa]
        mock_plex._server.library.sections.return_value = [section]
        assert mock_plex.owned_collections("shortlist") == {"sarah": OwnedRow("Shortlist_sarah", [571285], {"movie"})}

    def test_owned_collections_collects_a_users_row_from_every_library(self, mock_plex: PlexClient):
        """One user, one collection per library. Collapsing them to a single id once hid a real
        leak: T2 compared only the last collection it saw and passed while another was visible."""
        movie_row = MagicMock(ratingKey=571285)
        movie_row.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        show_row = MagicMock(ratingKey=571290)
        show_row.labels = [SimpleNamespace(tag="Shortlist_sarah")]
        movies, shows = MagicMock(), MagicMock()
        movies.type, shows.type = "movie", "show"
        movies.collections.return_value = [movie_row]
        shows.collections.return_value = [show_row]
        mock_plex._server.library.sections.return_value = [movies, shows]

        assert mock_plex.owned_collections("shortlist") == {
            "sarah": OwnedRow("Shortlist_sarah", [571285, 571290], {"movie", "show"})
        }

    def test_collections_titled_reads_the_server_not_the_runs_cache(self, mock_plex: PlexClient):
        """It is asked only after Plex refused a rename, and the cache holds objects renamed earlier in the
        same run under their OLD titles (plexapi's `editTitle` does not update them)."""
        stale = MagicMock(ratingKey=7, title="Old Name")
        fresh = MagicMock(ratingKey=7, title="New Name")
        section = MagicMock(type="movie")
        section.collections.side_effect = [[stale], [fresh]]
        mock_plex._server.library.sections.return_value = [section]
        mock_plex.find_owned_collections(section, "x")  # warms the cache with the stale object

        assert [c.ratingKey for c in mock_plex.collections_titled("new name")] == [7]

    def test_section_collections_are_cached_within_a_run(self, mock_plex: PlexClient):
        # The section's collection list is otherwise re-pulled for every owned/find scan. Two reads
        # of the same section fetch it once.
        section = MagicMock(type="movie")
        section.collections.return_value = []
        mock_plex._server.library.sections.return_value = [section]
        mock_plex.owned_collections("shortlist")
        mock_plex.find_owned_collections(section, "Shortlist_sarah")
        assert section.collections.call_count == 1

    def test_create_then_label_is_findable_from_the_warm_cache(self, mock_plex: PlexClient):
        # The rollout fix + its real safety mechanism: create APPENDS the collection to the cached list
        # (no whole-cache wipe -> one section.collections() per run, not O(N^2) per user). The appended
        # object is LABEL-LESS at append time; it only becomes findable because stored_label reloads
        # THAT SAME reference in place. This proves that end-to-end (not just "an already-labeled object
        # is findable"), because a fresh read would never have missed it.
        section = MagicMock(type="movie")
        section.collections.return_value = []
        mock_plex._server.library.sections.return_value = [section]
        mock_plex.find_owned_collections(section, "x")  # populates the cache (one fetch)

        created = MagicMock(labels=[])  # created WITHOUT a shortlist label yet
        created.reload.side_effect = lambda: setattr(created, "labels", [SimpleNamespace(tag="Shortlist_sarah")])
        mock_plex._server.createCollection.return_value = created

        mock_plex.create_collection(section, "New Row", [])
        # Before labelling it is NOT findable (correctly — it has no label yet).
        assert created not in mock_plex.find_owned_collections(section, "Shortlist_sarah")
        # stored_label labels + reloads the SAME cached object in place...
        mock_plex.stored_label(created, "shortlist_sarah")
        # ...so now it IS findable, from the still-warm cache (no second section.collections() read).
        assert created in mock_plex.find_owned_collections(section, "Shortlist_sarah")
        assert section.collections.call_count == 1

    def test_delete_busts_the_collections_cache(self, mock_plex: PlexClient):
        section = MagicMock(type="movie")
        section.collections.return_value = []
        mock_plex._server.library.sections.return_value = [section]
        mock_plex.find_owned_collections(section, "x")  # populates the cache
        doomed = MagicMock(labels=[SimpleNamespace(tag="shortlist_sarah")])
        mock_plex.delete_owned_collection(doomed, "shortlist")
        mock_plex.find_owned_collections(section, "x")  # must re-fetch
        assert section.collections.call_count == 2

    def test_section_signature_uses_count_and_updated_timestamp(self, mock_plex: PlexClient):
        # The index cache invalidates ONLY on this signature, so its shape assumptions matter: a real
        # LibrarySection carries a datetime `updatedAt`, which we key on as an int timestamp.

        updated = datetime(2026, 7, 15, tzinfo=UTC)
        section = SimpleNamespace(totalSize=1234, updatedAt=updated)
        assert mock_plex.section_signature(section) == f"1234:{int(updated.timestamp())}"

    def test_section_signature_passes_through_a_non_datetime_updated(self, mock_plex: PlexClient):
        section = SimpleNamespace(totalSize=1234, updatedAt=999)  # already numeric — used as-is
        assert mock_plex.section_signature(section) == "1234:999"

    def test_section_signature_falls_back_to_count_alone(self, mock_plex: PlexClient):
        section = SimpleNamespace(totalSize=1234)  # no updatedAt available
        assert mock_plex.section_signature(section) == "1234:None"

    def test_section_signature_is_none_when_nothing_is_available(self, mock_plex: PlexClient):
        # No signal at all -> the cache is disabled (the pipeline always re-scans), never wrongly reused.
        assert mock_plex.section_signature(SimpleNamespace()) is None

    def test_server_name_returns_friendly_name(self, mock_plex: PlexClient):
        mock_plex._server.friendlyName = "Home Server"
        assert mock_plex.server_name == "Home Server"

    def test_top_rated_returns_tmdb_pairs_skipping_items_without_guids(self, mock_plex: PlexClient):
        """The cold-start guid parse lives here now; items with no tmdb guid are skipped, and the
        search over-fetches (2x) so a sparse library still fills the request."""
        section = MagicMock()
        section.search.return_value = [
            fake_media_item(1, "A", tmdb_id=50),
            fake_media_item(2, "No Guid"),
            fake_media_item(3, "B", tmdb_id=60),
        ]
        pairs = mock_plex.top_rated(section, 2)
        assert [(tmdb_id, item.title) for tmdb_id, item in pairs] == [(50, "A"), (60, "B")]
        assert section.search.call_args.kwargs == {"sort": "audienceRating:desc", "limit": 4}

    @staticmethod
    def _item(rating_key: int) -> MagicMock:
        it = MagicMock()
        it.ratingKey = rating_key
        return it

    def test_set_items_adds_removes_from_prefetched_membership_without_reading(self, mock_plex: PlexClient):
        """set_items now takes the caller's already-fetched membership + only the items to add, so it
        makes ZERO extra PMS reads (no collection.items() here). It adds/removes + pins custom sort;
        ordering is the deferred order_collection pass, so no moveItem here."""
        item = self._item
        existing = [item(1), item(2), item(3)]  # 2 will be removed
        add_items = [item(4)]  # caller fetched ONLY the delta (item 4)
        wanted_keys = [4, 1, 3]
        collection = MagicMock()

        mock_plex.set_items(collection, existing, add_items, wanted_keys)

        collection.items.assert_not_called()  # no re-read — uses the passed-in membership
        assert [i.ratingKey for i in collection.addItems.call_args.args[0]] == [4]
        assert [i.ratingKey for i in collection.removeItems.call_args.args[0]] == [2]
        collection.sortUpdate.assert_called_once_with(sort="custom")
        collection.moveItem.assert_not_called()  # ordering happens later, in order_collection

    def test_set_items_retries_a_transient_500_instead_of_failing_the_user(self, mock_plex: PlexClient, monkeypatch):
        """A 5xx from addItems is Plex under load, not a rejected request.

        a large production server 2026-09-06: `PUT /library/collections/687180/items` answered 500 after exactly 10.0s
        while that same collection served eight GETs and a children read as 200 either side of it.
        It arrives as a plexapi BadRequest rather than a timeout, so the retry ladder never saw it
        and user j.fm failed for the whole run after 9.5 minutes of work - having already had their
        other row delivered.
        """
        monkeypatch.setattr("shortlist.engine.clients.plex_pms.time.sleep", lambda _s: None)
        item = self._item
        collection = MagicMock()
        collection.addItems.side_effect = [BadRequest("(500) internal_server_error; http://pms/1500"), None]

        mock_plex.set_items(collection, [item(1)], [item(4)], [1, 4])

        assert collection.addItems.call_count == 2, "a 500 must be retried, not raised"
        collection.sortUpdate.assert_called_once_with(sort="custom")

    def test_set_items_does_not_retry_a_400_which_means_the_row_is_broken(self, mock_plex: PlexClient, monkeypatch):
        """A 400 is a verdict about the request, so repeating it just wastes the ladder — and the
        caller needs CollectionRejectedItems promptly to rebuild the row."""
        monkeypatch.setattr("shortlist.engine.clients.plex_pms.time.sleep", lambda _s: None)
        item = self._item
        collection = MagicMock()
        collection.addItems.side_effect = BadRequest("(400) bad_request; http://pms/500")

        with pytest.raises(CollectionRejectedItems):
            mock_plex.set_items(collection, [item(1)], [item(4)], [1, 4])

        assert collection.addItems.call_count == 1, "a 400 must fail on the first attempt"

    def test_a_ratingkey_containing_500_is_not_mistaken_for_a_server_error(self, mock_plex: PlexClient, monkeypatch):
        """The url in a plexapi message carries the collection's own ratingKey, so a substring test
        for "500" matches key 1500 and would retry a genuine 400 four times."""
        monkeypatch.setattr("shortlist.engine.clients.plex_pms.time.sleep", lambda _s: None)
        item = self._item
        collection = MagicMock()
        collection.addItems.side_effect = BadRequest("(400) bad_request; http://pms/library/collections/1500/items")

        with pytest.raises(CollectionRejectedItems):
            mock_plex.set_items(collection, [item(1)], [item(4)], [1, 4])

        assert collection.addItems.call_count == 1

    def test_set_items_retries_a_transient_500_on_REMOVAL_too(self, mock_plex: PlexClient, monkeypatch):
        """The second failure of run 1 was a DELETE, not the add.

        a large production server 2026-09-06: user uid=20 died on
        `DELETE /library/collections/687190/items/604259 -> 500`, on a collection that served 11
        GETs and another DELETE as 200. Retrying only the add would have left this user failing.
        """
        monkeypatch.setattr("shortlist.engine.clients.plex_pms.time.sleep", lambda _s: None)
        item = self._item
        collection = MagicMock()
        collection.removeItems.side_effect = [BadRequest("(500) internal_server_error; http://pms/1"), None]

        mock_plex.set_items(collection, [item(1), item(2)], [], [1])

        assert collection.removeItems.call_count == 2, "a 500 on removal must be retried"

    def test_removals_go_one_at_a_time_so_a_retry_never_redeletes(self, mock_plex: PlexClient):
        """plexapi's removeItems loops a DELETE per item, so batching the retry would re-send the
        deletes that already succeeded. Each call must carry exactly one item."""
        item = self._item
        collection = MagicMock()

        mock_plex.set_items(collection, [item(1), item(2), item(3)], [], [1])

        sent = [c.args[0] for c in collection.removeItems.call_args_list]
        assert [len(batch) for batch in sent] == [1, 1], "each removal must be its own call"
        assert sorted(i.ratingKey for batch in sent for i in batch) == [2, 3]

    def test_stored_label_writes_both_labels_in_one_call(self, mock_plex: PlexClient):
        """Two labels, ONE PUT. Verified against the live PMS 2026-09-06 before this was written:
        addLabel([a, b]) came back as a single PUT with both labels present."""
        collection = MagicMock()
        collection.labels = []

        def _applied(tags):
            collection.labels = [SimpleNamespace(tag=x.title()) for x in tags]

        collection.addLabel.side_effect = _applied

        stored = mock_plex.stored_label(collection, "shortlist_alice", extra="shortlist")

        assert collection.addLabel.call_count == 1, "both labels must go in a single write"
        assert collection.addLabel.call_args.args[0] == ["shortlist_alice", "shortlist"]
        assert stored == "Shortlist_Alice", "the CRITICAL label's stored casing is what filters use"

    def test_stored_label_returns_the_critical_labels_casing_not_the_extras(self, mock_plex: PlexClient):
        """The returned string is written into every OTHER account's `label!=` exclude. Return the
        wrong one and nobody's filter hides this row."""
        collection = MagicMock()
        collection.labels = [SimpleNamespace(tag="Shortlist_Alice"), SimpleNamespace(tag="Shortlist")]

        stored = mock_plex.stored_label(collection, "shortlist_alice", extra="shortlist")

        assert stored == "Shortlist_Alice"
        assert collection.addLabel.call_count == 0, "both already present — no write at all"

    def test_a_missing_critical_label_still_raises_so_the_caller_deletes_the_row(self, mock_plex: PlexClient):
        """An unlabelled row is one no share filter can hide, so it must never survive."""
        collection = MagicMock()
        collection.labels = []
        collection.addLabel.side_effect = lambda tags: None  # Plex accepts, nothing persists

        with pytest.raises(RuntimeError, match="did not persist"):
            mock_plex.stored_label(collection, "shortlist_alice", extra="shortlist")

    def test_a_missing_EXTRA_label_is_not_fatal(self, mock_plex: PlexClient):
        """The constant label is cosmetic (Kometa coexistence). Losing it must not fail a row that
        already reached Plex with its privacy label intact."""
        collection = MagicMock()
        collection.labels = []
        collection.addLabel.side_effect = lambda tags: setattr(
            collection, "labels", [SimpleNamespace(tag="Shortlist_Alice")]
        )

        assert mock_plex.stored_label(collection, "shortlist_alice", extra="shortlist") == "Shortlist_Alice"

    def test_a_batched_failure_falls_back_to_the_critical_label_alone(self, mock_plex: PlexClient, monkeypatch):
        """Batching must not make a cosmetic failure fatal. Before this, `label` succeeding and
        `extra` failing left the row alive and private; one combined write must not turn that into a
        deleted row."""
        monkeypatch.setattr("shortlist.engine.clients.plex_pms.time.sleep", lambda _s: None)
        collection = MagicMock()
        collection.labels = []

        def _addLabel(tags):
            if len(tags) > 1:
                raise BadRequest("(400) bad_request; http://pms/1")
            collection.labels = [SimpleNamespace(tag="Shortlist_Alice")]

        collection.addLabel.side_effect = _addLabel

        assert mock_plex.stored_label(collection, "shortlist_alice", extra="shortlist") == "Shortlist_Alice"
        assert collection.addLabel.call_count == 2, "batched attempt, then the critical label alone"
        assert collection.addLabel.call_args.args[0] == ["shortlist_alice"]

    def test_stored_label_retries_a_transient_500_rather_than_losing_the_row(self, mock_plex: PlexClient, monkeypatch):
        """A 5xx here makes the CALLER DELETE the row, so an un-retried wobble bins a good row.
        a large production server 2026-09-06: creating one collection needed four attempts under load."""
        monkeypatch.setattr("shortlist.engine.clients.plex_pms.time.sleep", lambda _s: None)
        collection = MagicMock()
        collection.labels = []
        calls = {"n": 0}

        def _addLabel(tags):
            calls["n"] += 1
            if calls["n"] == 1:
                raise BadRequest("(500) internal_server_error; http://pms/1")
            collection.labels = [SimpleNamespace(tag=x.title()) for x in tags]

        collection.addLabel.side_effect = _addLabel

        assert mock_plex.stored_label(collection, "shortlist_alice", extra="shortlist") == "Shortlist_Alice"
        assert calls["n"] == 2, "the 500 must be retried, not surfaced as a lost row"

    def test_stored_label_without_extra_is_unchanged(self, mock_plex: PlexClient):
        """The backfill path still calls this with one label; it must behave exactly as before."""
        collection = MagicMock()
        collection.labels = []
        collection.addLabel.side_effect = lambda tags: setattr(collection, "labels", [SimpleNamespace(tag="Shortlist")])

        assert mock_plex.stored_label(collection, "shortlist") == "Shortlist"
        assert collection.addLabel.call_args.args[0] == ["shortlist"]

    def test_order_collection_moves_only_displaced_items(self, mock_plex: PlexClient):
        """order_collection reorders with the FEWEST moveItem calls: only items out of place move,
        since Plex's moveItem is one PMS round-trip each (the slow part). Live order 1,3,4 -> 4,1,3."""
        item = self._item
        collection = MagicMock()
        collection.items.return_value = [item(1), item(3), item(4)]

        moves_made = mock_plex.order_collection(collection, [4, 1, 3])  # wanted ranked ratingKeys

        collection.reload.assert_called_once()
        moves = collection.moveItem.call_args_list
        assert [c.args[0].ratingKey for c in moves] == [4]  # only 4 is out of place
        assert moves[0].kwargs["after"] is None  # 4 goes to the front
        assert moves_made == 1

    def test_order_collection_reverses_order_with_after_previous_chain(self, mock_plex: PlexClient):
        """The insert-after-previous math the one-move case never exercises. [1,2,3] -> [3,2,1] is two
        moves: 3 to front, 2 after 3."""
        item = self._item
        collection = MagicMock()
        collection.items.return_value = [item(1), item(2), item(3)]

        mock_plex.order_collection(collection, [3, 2, 1])

        moves = [
            (c.args[0].ratingKey, c.kwargs["after"].ratingKey if c.kwargs["after"] else None)
            for c in collection.moveItem.call_args_list
        ]
        assert moves == [(3, None), (2, 3)], f"expected 3→front then 2→after 3, got {moves}"

    def test_order_collection_makes_no_moves_when_already_in_order(self, mock_plex: PlexClient):
        """The steady-state win: a row whose order is unchanged issues ZERO moveItem calls."""
        item = self._item
        collection = MagicMock()
        collection.items.return_value = [item(1), item(2), item(3)]

        assert mock_plex.order_collection(collection, [1, 2, 3]) == 0
        collection.moveItem.assert_not_called()

    def test_order_collection_orders_the_whole_row_not_just_the_head(self, mock_plex: PlexClient):
        """The WHOLE row is ordered, tail included. This ordered only the top 15, so a 30-pick row read
        as ranked-then-alphabetical and the ranking looked broken to the person it was built for.

        40 items — the largest a row may be (``row.size`` is validated 5..40) — fully reversed, so every
        position is out of place.

        Replays each ``moveItem(item, after=...)`` against a model of the collection and asserts the
        ORDER that results, because the order is the entire point. Asserting only WHICH items moved
        cannot see it: a SUT degraded to ``after=None`` on every call produces a byte-identical move
        list (39 moves, all of them past the old cap) and leaves the row in exactly the wrong order.
        ``after`` is the one kwarg this function is responsible for, so it is the one to assert
        (tests/testing.md: if removing a parameter wouldn't break the test, it isn't covered).
        """
        size = 40
        live = [self._item(i) for i in range(size, 0, -1)]
        collection = MagicMock()
        collection.items.return_value = live
        wanted = list(range(1, size + 1))

        mock_plex.order_collection(collection, wanted)

        order = [i.ratingKey for i in live]
        for call in collection.moveItem.call_args_list:
            key = call.args[0].ratingKey
            after = call.kwargs.get("after")
            order.remove(key)
            order.insert(0 if after is None else order.index(after.ratingKey) + 1, key)

        assert order == wanted, f"the collection must end up in ranked order, got {order}"
        moved = [c.args[0].ratingKey for c in collection.moveItem.call_args_list]
        assert [k for k in moved if k > 15], "items past the old cap of 15 must move, not sit in the tail"

    def test_sections_by_type_maps_each_media_type_to_its_library(self, mock_plex: PlexClient):
        movies, shows = MagicMock(), MagicMock()
        movies.type, movies.key = "movie", "1"
        shows.type, shows.key = "show", "2"
        mock_plex._server.library.sections.return_value = [movies, shows]

        assert mock_plex.sections_by_type() == {MediaType.MOVIE: movies, MediaType.SHOW: shows}

    def test_a_tv_collection_reads_its_shows_and_skips_episodes(self, mock_plex: PlexClient):
        """Kometa builds season- and episode-level collections. An episode's ``tmdb://`` guid is an EPISODE id,
        which read as a show id would put an unrelated series in the season."""
        show = SimpleNamespace(type="show", title="Doctor Who", year=2005, guids=[SimpleNamespace(id="tmdb://57243")])
        episode = SimpleNamespace(
            type="episode", title="The Christmas Invasion", year=2005, guids=[SimpleNamespace(id="tmdb://1008562")]
        )
        collection = SimpleNamespace(title="Christmas Specials", items=lambda: [show, episode])
        mock_plex._sections_cache = [
            SimpleNamespace(key="2", type="show", title="TV Shows", collections=lambda: [collection])
        ]

        assert mock_plex.collection_members("2", "Christmas Specials") == [
            LibraryTitle(57243, MediaType.SHOW, "Doctor Who", 2005)
        ]

    def test_a_collection_says_how_many_of_its_items_it_could_not_use(self, mock_plex: PlexClient):
        """The season editor counts what a collection gives the season, so a member skipped for having no TMDB
        id, or for being an episode, must not vanish without a word."""
        from loguru import logger

        show = SimpleNamespace(type="show", title="Doctor Who", year=2005, guids=[SimpleNamespace(id="tmdb://57243")])
        episode = SimpleNamespace(type="episode", title="Ep", year=2005, guids=[SimpleNamespace(id="tmdb://1")])
        unmatched = SimpleNamespace(type="show", title="Local Show", year=2001, guids=[])
        collection = SimpleNamespace(title="Christmas Specials", items=lambda: [show, episode, unmatched])
        mock_plex._sections_cache = [
            SimpleNamespace(key="2", type="show", title="TV Shows", collections=lambda: [collection])
        ]
        lines: list[str] = []
        sink = logger.add(lines.append, level="INFO", format="{message}")
        try:
            members = mock_plex.collection_members("2", "Christmas Specials")
        finally:
            logger.remove(sink)

        assert [m.tmdb_id for m in members] == [57243]
        assert any("Christmas Specials" in line and "2 of its 3" in line for line in lines), lines


class TestUserHubs:
    """Fetch hubs AS another user (that user's server token) — the visibility-check read."""

    _URL = "http://pms:32400/hubs"

    @respx.mock
    def test_reads_hubs_as_the_given_user(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(
            return_value=httpx.Response(200, json={"MediaContainer": {"Hub": [{"title": "Home"}]}})
        )

        hubs = mock_plex.user_hubs("USER-TOK")

        assert hubs == [{"title": "Home"}]
        request = respx.calls.last.request
        assert request.headers["X-Plex-Token"] == "USER-TOK"

    @respx.mock
    def test_a_missing_hub_container_is_an_empty_list_not_an_error(self, mock_plex: PlexClient):
        mock_plex._server.url.return_value = self._URL
        respx.get(self._URL).mock(return_value=httpx.Response(200, json={"MediaContainer": {}}))
        assert mock_plex.user_hubs("USER-TOK") == []

    def test_the_configured_timeout_reaches_the_raw_read(self, mock_plex: PlexClient, monkeypatch):
        """This used to hardcode `timeout=30`, ignoring the operator's configured `plex.timeout_s`."""
        from shortlist.engine.clients import plex_pms

        mock_plex._server.url.return_value = self._URL
        mock_plex._timeout = 77
        seen: list[object] = []

        def fake_get(*_args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return httpx.Response(200, json={"MediaContainer": {}}, request=httpx.Request("GET", self._URL))

        monkeypatch.setattr(plex_pms.http_retry, "get", fake_get)
        mock_plex.user_hubs("USER-TOK")
        assert seen == [77]


class TestSectionsByType:
    def test_the_lowest_keyed_library_of_each_type_wins(self, mock_plex: PlexClient):
        """PMS list order must not decide where rows live: a reordering would silently move
        every user's row into a different library."""
        movies_4k, movies, shows = MagicMock(), MagicMock(), MagicMock()
        movies_4k.type, movies_4k.key = "movie", "3"
        movies.type, movies.key = "movie", "1"
        shows.type, shows.key = "show", "2"
        mock_plex._server.library.sections.return_value = [movies_4k, movies, shows]

        assert mock_plex.sections_by_type() == {MediaType.MOVIE: movies, MediaType.SHOW: shows}


class TestPmsPromoteRetry:
    """A promote is idempotent, so a PMS read-timeout must be retried, not fail the user (the shape
    of the a large production server 48-user rollout, where 42 users died on one un-retried promote timeout)."""

    def test_retries_a_timeout_then_succeeds(self, monkeypatch):
        import requests

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)  # no real backoff waits
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.exceptions.ReadTimeout("slow PMS")

        plex_pms._retry_idempotent(flaky, label="row")
        assert calls["n"] == 3  # failed twice, third try landed

    def test_gives_up_after_the_last_attempt(self, monkeypatch):
        import requests

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)
        calls = {"n": 0}

        def always_times_out():
            calls["n"] += 1
            raise requests.exceptions.ConnectTimeout("dead PMS")

        with pytest.raises(requests.exceptions.ConnectTimeout):
            plex_pms._retry_idempotent(always_times_out, label="row", attempts=4)
        assert calls["n"] == 4  # tried the full budget, then re-raised

    def test_a_non_timeout_error_is_not_retried(self, monkeypatch):
        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise ValueError("a real bug, not a timeout")

        with pytest.raises(ValueError):
            plex_pms._retry_idempotent(boom, label="row")
        assert calls["n"] == 1  # surfaced immediately, no retry


class TestTimingHTTPAdapter:
    """Every PMS HTTP call is timed so the delivery path isn't a black hole — but the PMS URL carries
    ``X-Plex-Token`` in its query string, so the log must show the path and NEVER the token (rule 9)."""

    def _real_request(self, method: str):
        # A REAL PreparedRequest, so the redaction is exercised against how `requests` actually shapes
        # path_url (the load-bearing rule-9 assumption), not a hand-built string (review, rule 11).
        import requests

        pr = requests.PreparedRequest()
        pr.prepare(
            method=method,
            url="http://pms:32400/library/collections/9/items?X-Plex-Token=SECRETTOKEN&excludeAllLeaves=1",
        )
        return pr

    def test_logs_the_path_and_status_without_leaking_the_token(self, monkeypatch):
        from requests.adapters import HTTPAdapter

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kw: SimpleNamespace(status_code=200))
        adapter = plex_pms._TimingHTTPAdapter()
        lines: list[str] = []
        sink = plex_pms.logger.add(lines.append, level="DEBUG", format="{message}")
        try:
            resp = adapter.send(self._real_request("DELETE"))
        finally:
            plex_pms.logger.remove(sink)

        assert resp.status_code == 200
        joined = "\n".join(lines)
        assert "SECRETTOKEN" not in joined  # rule 9: the token must never reach the log
        assert "/library/collections/9/items" in joined
        assert "DELETE" in joined and "200" in joined

    def test_a_slow_call_is_flagged_at_warning(self, monkeypatch):
        from requests.adapters import HTTPAdapter

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kw: SimpleNamespace(status_code=200))
        ticks = iter([100.0, 100.0 + plex_pms._SLOW_PMS_S + 2.0])  # elapsed > the slow threshold
        monkeypatch.setattr(plex_pms.time, "monotonic", lambda: next(ticks))
        adapter = plex_pms._TimingHTTPAdapter()
        lines: list[str] = []
        sink = plex_pms.logger.add(lines.append, level="DEBUG", format="{level}|{message}")
        try:
            adapter.send(self._real_request("PUT"))
        finally:
            plex_pms.logger.remove(sink)

        joined = "\n".join(lines)
        assert "WARNING|" in joined and "SLOW" in joined

    def test_a_failing_call_still_logs_then_re_raises(self, monkeypatch):
        """The retry/timeout path is load-bearing: if super().send() raises, the adapter must time+log
        the attempt (status ERR) and let the ORIGINAL exception propagate unchanged — never swallow it,
        or a PMS timeout would stop reaching _PMS_TIMEOUTS and the whole-user retry."""
        import requests
        from requests.adapters import HTTPAdapter

        from shortlist.engine.clients import plex_pms

        def boom(self, request, **kw):
            raise requests.exceptions.ConnectionError("dead PMS")

        monkeypatch.setattr(HTTPAdapter, "send", boom)
        adapter = plex_pms._TimingHTTPAdapter()
        lines: list[str] = []
        sink = plex_pms.logger.add(lines.append, level="DEBUG", format="{message}")
        try:
            with pytest.raises(requests.exceptions.ConnectionError):
                adapter.send(self._real_request("GET"))
        finally:
            plex_pms.logger.remove(sink)

        joined = "\n".join(lines)
        assert "ERR" in joined  # the failed attempt is still timed and logged
        assert "SECRETTOKEN" not in joined
