"""Unit tests for shelf_mirror.plan_home_order — what we ask agregarr to store.

These pin the decisions that make the mirror safe to run after every run: it writes NOTHING when
agregarr already agrees (the steady state), it takes the order FROM Plex so agregarr's own rows keep
their relative order, and it never rewrites an item agregarr has parked as unplaced.

The shapes here are the ones a real agregarr 2.4.2 serves: `/preexisting` items carry
`collectionRatingKey` and join to the Plex identifier "custom.collection.<lib>.<ratingKey>", hub
configs carry `hubIdentifier`, and `sortOrderHome` is a relative key where 0 means "unplaced, sort
me last" rather than "first".
"""

from shortlist.engine.shelf_mirror import plan_home_order

LIB = "1"


def row(rating_key: str, sort_order: int, name: str = "row") -> dict:
    """A Shortlist-style pre-existing collection config as agregarr stores it."""
    return {
        "id": f"cfg-{rating_key}",
        "collectionRatingKey": rating_key,
        "libraryId": LIB,
        "name": name,
        "sortOrderHome": sort_order,
        "configType": "preExisting",
    }


def hub(identifier: str, sort_order: int) -> dict:
    return {
        "id": f"{LIB}:{identifier}",
        "hubIdentifier": identifier,
        "libraryId": LIB,
        "name": identifier,
        "sortOrderHome": sort_order,
        "configType": "hub",
    }


def ident(rating_key: str) -> str:
    return f"custom.collection.{LIB}.{rating_key}"


class TestNoOp:
    def test_no_write_when_agregarr_already_matches_the_shelf(self):
        # Arrange — stored order and live order agree.
        items = [hub("movie.recentlyadded", 1), row("100", 2), row("200", 3)]
        live = ["movie.recentlyadded", ident("100"), ident("200")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100", "200"})

        # Assert — the caller skips the POST entirely.
        assert plan.changed is False
        assert plan.moved == 0
        assert "already in step" in plan.summary()

    def test_no_write_when_agregarr_manages_nothing_here(self):
        plan = plan_home_order(LIB, ["movie.recentlyadded"], [], owned_rating_keys=set())

        assert plan.ordered == []
        assert plan.changed is False
        assert plan.unknown_to_agregarr == ["movie.recentlyadded"]


class TestOrdering:
    def test_orders_items_to_match_the_live_shelf_when_scattered(self):
        # Arrange — our two rows are stored below a foreign collection.
        items = [hub("movie.recentlyadded", 1), row("100", 40), row("200", 41), row("900", 2, "Action Movies")]
        live = ["movie.recentlyadded", ident("100"), ident("200"), ident("900")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100", "200"})

        # Assert — sent in live order, which is what agregarr numbers 1..N.
        assert plan.changed is True
        assert [i.get("collectionRatingKey") or i["hubIdentifier"] for i in plan.ordered] == [
            "movie.recentlyadded",
            "100",
            "200",
            "900",
        ]
        assert plan.owned_placed == 2
        assert plan.owned_contiguous is True

    def test_preserves_foreign_rows_relative_order(self):
        # Arrange — three foreign collections whose live order came from agregarr's own last sync.
        items = [row("901", 9, "A"), row("902", 3, "B"), row("903", 7, "C"), row("100", 0)]
        live = [ident("100"), ident("902"), ident("903"), ident("901")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100"})

        # Assert — B, C, A: exactly the live sequence, not re-sorted by their stored keys.
        foreign = [i["name"] for i in plan.ordered if i["name"] in {"A", "B", "C"}]
        assert foreign == ["B", "C", "A"]

    def test_places_a_row_agregarr_had_parked_as_unplaced(self):
        # Arrange — sortOrderHome 0 means "unplaced", and agregarr sorts those LAST. This row is
        # second on the live shelf, so leaving it at 0 would sink it below the foreign row.
        # (Four of SFLIX's 46 Movies rows were sitting at 0 exactly like this.)
        items = [hub("movie.recentlyadded", 1), row("100", 0), row("900", 2, "Foreign")]
        live = ["movie.recentlyadded", ident("100"), ident("900")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100"})

        # Assert — it gets a real place, above the foreign row.
        assert plan.changed is True
        assert [i.get("collectionRatingKey") or i["hubIdentifier"] for i in plan.ordered] == [
            "movie.recentlyadded",
            "100",
            "900",
        ]

    def test_an_unplaced_row_is_pinned_even_when_it_already_falls_last(self):
        # Arrange — agregarr appends unplaced items to the end, which happens to be where this one
        # belongs today. But "0" pins nothing: its position rests on how agregarr's own GET ordered
        # equal keys, so a second unplaced item could swap with it at any time.
        items = [hub("movie.recentlyadded", 1), row("100", 0)]
        live = ["movie.recentlyadded", ident("100")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100"})

        # Assert — write it, so the order is stored rather than lucky.
        assert plan.changed is True


class TestItemsOffTheShelf:
    def test_pushes_a_known_row_that_is_not_on_the_shelf_to_the_end(self):
        # Arrange — 900 holds key 2, which would otherwise land it inside our block.
        items = [hub("movie.recentlyadded", 1), row("100", 30), row("900", 2, "Off-shelf")]
        live = ["movie.recentlyadded", ident("100")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100"})

        # Assert — still sent (so it gets renumbered), but after everything visible.
        assert [i.get("collectionRatingKey") or i["hubIdentifier"] for i in plan.ordered] == [
            "movie.recentlyadded",
            "100",
            "900",
        ]

    def test_leaves_an_unplaced_off_shelf_row_alone(self):
        # Arrange — key 0 and not on the shelf: agregarr already sorts it last.
        items = [hub("movie.recentlyadded", 1), row("100", 2), row("900", 0, "Parked")]
        live = ["movie.recentlyadded", ident("100")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100"})

        # Assert — rewriting it would change nothing and could opt it into randomised shuffling.
        assert all(i.get("collectionRatingKey") != "900" for i in plan.ordered)
        assert plan.changed is False

    def test_reports_shelf_hubs_agregarr_does_not_know(self):
        # Arrange — a row created since agregarr's last discovery pass.
        items = [hub("movie.recentlyadded", 1)]
        live = ["movie.recentlyadded", ident("777")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"777"})

        # Assert — surfaced for the audit line; agregarr cannot place what it has not discovered.
        assert plan.unknown_to_agregarr == [ident("777")]
        assert plan.owned_placed == 0


class TestConfigsThatCannotBeJoined:
    """A config carrying neither `collectionRatingKey` nor `hubIdentifier`.

    It still holds a sort key, so it can sort straight into the middle of our block — but agregarr's
    type guards skip anything without a join key, so we cannot tell it where to put one. The danger
    is doing that SILENTLY: before this was handled, such an item was dropped from the plan and
    invisible to the diff, so every run reported "already in step" while the shelf stayed contested.
    """

    def test_it_is_counted_rather_than_silently_dropped(self):
        # Arrange — a mystery config holding key 1, which sorts ABOVE our row.
        mystery = {"id": "mystery", "libraryId": LIB, "sortOrderHome": 1, "configType": "collection"}
        items = [mystery, row("100", 30)]
        live = [ident("100")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100"})

        assert plan.unjoinable == 1
        assert "cannot place" in plan.summary()

    def test_it_is_not_sent_because_agregarr_would_skip_it_forever(self):
        # Sending it would mean a write every run that never converges — agregarr answers 200 and
        # ignores the item, so its stored key never changes and the next run sees the same diff.
        mystery = {"id": "mystery", "libraryId": LIB, "sortOrderHome": 1, "configType": "collection"}

        plan = plan_home_order(LIB, [ident("100")], [mystery, row("100", 30)], owned_rating_keys={"100"})

        assert all(i.get("id") != "mystery" for i in plan.ordered)

    def test_a_second_config_shadowing_the_same_identifier_still_gets_placed(self):
        # Two configs for one ratingKey: the second used to vanish with the first's identifier, keep
        # its low key, and sort inside our block. It CAN be written (it has a join key), so it is.
        first = row("100", 30)
        shadow = {**row("100", 2), "id": "cfg-100-dupe"}

        plan = plan_home_order(LIB, [ident("100")], [first, shadow], owned_rating_keys={"100"})

        assert [i["id"] for i in plan.ordered] == ["cfg-100", "cfg-100-dupe"]
        assert plan.unjoinable == 0


class TestOddSortKeys:
    def test_a_float_key_is_not_mistaken_for_unplaced(self):
        # JSON numbers need not arrive as ints. Read as "unplaced", this item would be left out of
        # the write entirely and keep a key that sorts it above our rows.
        foreign = {**row("900", 0, "Foreign"), "sortOrderHome": 2.0}

        plan = plan_home_order(LIB, [ident("100")], [row("100", 30), foreign], owned_rating_keys={"100"})

        assert [i["id"] for i in plan.ordered] == ["cfg-100", "cfg-900"]

    def test_a_numeric_string_key_is_not_mistaken_for_unplaced(self):
        foreign = {**row("900", 0, "Foreign"), "sortOrderHome": "2"}

        plan = plan_home_order(LIB, [ident("100")], [row("100", 30), foreign], owned_rating_keys={"100"})

        assert [i["id"] for i in plan.ordered] == ["cfg-100", "cfg-900"]

    def test_a_nonsense_key_is_treated_as_unplaced_not_a_crash(self):
        foreign = {**row("900", 0, "Foreign"), "sortOrderHome": "later please"}

        plan = plan_home_order(LIB, [ident("100")], [row("100", 30), foreign], owned_rating_keys={"100"})

        assert all(i["id"] != "cfg-900" for i in plan.ordered)


class TestMovedCount:
    def test_never_exceeds_the_number_of_items_sent(self):
        """A live run logged "131 of 125 items reordered": an item both out of position AND unplaced
        was counted twice. The number is read by a human, so it has to mean something."""
        # Every row here is unplaced AND in the wrong order — the double-count case.
        items = [row("100", 0), row("200", 0), row("300", 0), hub("movie.recentlyadded", 9)]
        live = ["movie.recentlyadded", ident("300"), ident("200"), ident("100")]

        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100", "200", "300"})

        assert plan.moved <= len(plan.ordered)
        assert plan.changed is True


class TestVisibilityAwareContiguity:
    """`owned_contiguous` answers "do users see one unbroken block?", so it counts only the rows
    actually on shared Home. SFLIX has 4 rows promoted nowhere sitting at the bottom of the managed
    list; counting them reported a perfectly healthy shelf as broken every single night."""

    def test_rows_promoted_nowhere_do_not_break_the_block(self):
        # Arrange — two visible rows on top, a foreign row, then a dormant row of ours at the end.
        items = [row("100", 2), row("200", 3), row("900", 4, "Foreign"), row("300", 5)]
        live = [ident("100"), ident("200"), ident("900"), ident("300")]
        visible = {ident("100"), ident("200"), ident("900")}  # 300 is on no surface

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100", "200", "300"}, visible_identifiers=visible)

        # Assert — the dormant row is still counted as placed, but cannot break contiguity.
        assert plan.owned_placed == 3
        assert plan.owned_contiguous is True

    def test_a_visible_foreign_row_inside_the_block_still_breaks_it(self):
        # The flag must not become useless: a foreign row users CAN see between two of ours is
        # exactly the failure it exists to report.
        items = [row("100", 2), row("900", 3, "Foreign"), row("200", 4)]
        live = [ident("100"), ident("900"), ident("200")]
        visible = {ident("100"), ident("900"), ident("200")}

        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100", "200"}, visible_identifiers=visible)

        assert plan.owned_contiguous is False

    def test_everything_counts_when_visibility_is_unknown(self):
        items = [row("100", 2), row("900", 3, "Foreign"), row("200", 4)]
        live = [ident("100"), ident("900"), ident("200")]

        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100", "200"})

        assert plan.owned_contiguous is False


class TestOwnedBlock:
    def test_flags_our_rows_as_split_when_a_foreign_row_interleaves(self):
        # Arrange — the shelf itself has a foreign row between two of ours.
        items = [row("100", 2), row("900", 3, "Foreign"), row("200", 4)]
        live = [ident("100"), ident("900"), ident("200")]

        # Act
        plan = plan_home_order(LIB, live, items, owned_rating_keys={"100", "200"})

        # Assert — we mirror the shelf faithfully, but the caller can see it is not one block.
        assert plan.owned_placed == 2
        assert plan.owned_contiguous is False
