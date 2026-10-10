"""The constant label, the owner prefix and what a delivery diff reports."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.delivery import deliver_rows, row_marker
from shortlist.engine.models import LABEL_PREFIX, EngineConfig, MediaType, Pick
from tests.conftest import make_profile
from tests.unit.delivery_support import (
    _labelling_plex_mock,
    _section,
    movies,  # noqa: F401
    picks,
)


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

        # Both labels still go on; the constant one now rides along as `extra` in the SAME write,
        # so gather from both the positional label and that kwarg.
        applied = [c.args[1] for c in plex.stored_label.call_args_list]
        applied += [c.kwargs["extra"] for c in plex.stored_label.call_args_list if c.kwargs.get("extra")]
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

        def boom(collection, label, *, extra=None):
            if label == LABEL_PREFIX:
                raise RuntimeError("PMS said no")
            # The create write lands the OWNER label but NOT the constant one — what the real client
            # does when the batched write fails and it falls back to the critical label alone. That
            # leaves `_apply_shortlist_label` with work to do, so its swallow is what this exercises;
            # passing `extra` through here would apply the label and the delete path would never be
            # approached at all.
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
        client.sections = lambda: [SimpleNamespace(title="Movies", type="movie")]

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

    def test_poster_reset_refuses_it_too(self, engine_config: EngineConfig, movies):
        from shortlist.engine.delivery import reset_row_posters

        plex = MagicMock(spec=PlexClient)

        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]

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
        from shortlist.engine.delivery import reset_row_posters

        plex = MagicMock(spec=PlexClient)

        plex.fetch_items.return_value = ([], [])
        plex.sections.return_value = [movies]
        plex.find_owned_collections.return_value = []

        reset_row_posters(plex, engine_config, label="Shortlist_sarah", displays=None, dry_run=True)

        # Reached the search rather than being turned away at the door — asserted per function, since
        # one of the two passing would otherwise hide the other failing.
        assert plex.find_owned_collections.call_count == 1
        assert {c.args[1] for c in plex.find_owned_collections.call_args_list} == {"Shortlist_sarah"}


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
        breakdown = []

        reports = deliver_rows(
            plex,
            make_profile(),
            [alive, gone],
            engine_config,
            RowSpec(slug="picked", name_template="Picked", size=10, media="movie"),
            sections=[movies],
            section_picks={movies.key: [alive, gone]},
            dry_run=False,
            breakdown=breakdown,
        )

        # `deliver_rows` returns (diff, label) — the diff is what the run report and the stats read.
        diff = reports[0] if isinstance(reports, tuple) else reports
        added = diff.added if hasattr(diff, "added") else diff[0].added
        assert "Still Here" in added
        assert "Deleted Since" not in added, "the run must not claim it delivered a title Plex dropped"
        assert [p["rating_key"] for p in breakdown[0]["picks"]] == [101], "watch membership must use actual delivery"


class TestMarkedAccountIds:
    """`marked_account_ids` is the title-marker half of `privacy.dead_private`'s double check (rule 4)."""

    @staticmethod
    def _client(titles_by_section: list[list[str]]) -> PlexClient:
        client = PlexClient.__new__(PlexClient)
        sections = [SimpleNamespace(title=f"S{i}", type="movie") for i in range(len(titles_by_section))]
        by_title = {
            s.title: [SimpleNamespace(title=t) for t in titles]
            for s, titles in zip(sections, titles_by_section, strict=True)
        }
        client.sections = lambda: sections
        client._section_collections = lambda section: by_title[section.title]
        return client

    def test_marked_titles_yield_their_account_ids_and_foreign_titles_none(self):
        client = self._client(
            [
                ["✨ Picked for You" + row_marker(100), "Kometa: Top 10", "✨ Picked for You" + row_marker(200)],
                ["✨ Picked for You" + row_marker(100)],
            ]
        )

        assert client.marked_account_ids() == {100, 200}

    def test_an_empty_listing_yields_an_empty_set(self):
        assert self._client([[], []]).marked_account_ids() == set()
