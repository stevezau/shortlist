"""Unit tests for PlexClient.order_owned_hubs — the Recommended-shelf placement of Shortlist rows.

These pin the DECISION logic: only our hubs move, the anchor is read-only (Kometa coexistence), it's
idempotent, dry-run is inert — and, since 2026-08-12, that it moves only hubs actually OUT OF PLACE
and re-reads the shelf to verify rather than trusting Plex's 200.

``FakeSection`` APPLIES each move to its own list, so the shelf a test reads back is the shelf the
moves produced (testing rule: the fake must be no easier than the real server — one that ignored
moves would make every assertion here about a shape Plex never returns). ``DroppingSection`` models
a shelf we do not win: a move that returns 200 and leaves the order unchanged, which is what a
co-managing tool (agregarr, Kometa) reordering the same shelf between our passes looks like from here.
"""

from shortlist.engine.clients.plex_pms import PlexClient

_UNSET = "UNSET"  # sentinel: move() was never called on this hub


class FakeHub:
    def __init__(self, title: str, ident: str):
        self.title = title
        self.identifier = ident
        self.moved_after = _UNSET
        self.moves = 0  # how many times we asked Plex to move THIS hub
        self.shelf = None

    def reload(self):
        return self

    def move(self, after=None):
        self.moved_after = after
        self.moves += 1
        if self.shelf is not None:
            self.shelf.apply(self, after)


class FakeLabel:
    def __init__(self, tag: str):
        self.tag = tag


class FakeColl:
    def __init__(self, title: str, tags: list[str], rating_key: int = 0):
        self.title = title
        self.labels = [FakeLabel(t) for t in tags]
        self.ratingKey = rating_key


class FakeSection:
    """A managed shelf that really reorders when a hub is moved."""

    def __init__(self, hubs: list[FakeHub], title: str = "TV Shows", key: int = 2):
        self._hubs = list(hubs)
        self.title = title
        self.key = key
        for hub in self._hubs:
            hub.shelf = self

    def managedHubs(self):
        return list(self._hubs)

    def apply(self, hub: FakeHub, after) -> None:
        self._hubs.remove(hub)
        self._hubs.insert(0 if after is None else self._hubs.index(after) + 1, hub)

    def titles(self) -> list[str]:
        return [h.title for h in self._hubs]


class DroppingSection(FakeSection):
    """A shelf we never win: the move is accepted, and the order is unchanged when we look again."""

    def apply(self, hub: FakeHub, after) -> None:
        return None


def _client(colls: list[FakeColl]) -> PlexClient:
    client = PlexClient.__new__(PlexClient)  # bypass __init__ (no real PlexServer)
    client._section_collections = lambda section: colls
    return client


def test_moves_our_rows_immediately_after_the_anchor():
    anchor = FakeHub("New Series", "a")
    genre = FakeHub("Genre", "g")
    r1 = FakeHub("Picked for You", "o1")
    r2 = FakeHub("Because you watched X", "o2")
    section = FakeSection([anchor, genre, r1, r2])  # our rows buried at the bottom
    client = _client(
        [
            FakeColl("Picked for You", ["shortlist_sarah"]),
            FakeColl("Because you watched X", ["shortlist_mike"]),
            FakeColl("Genre", ["kometa"]),
        ]
    )

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert result["skipped"] is False
    assert set(result["moved"]) == {"Picked for You", "Because you watched X"}
    assert r1.moved_after is anchor  # first row lands right after the anchor
    assert r2.moved_after is r1  # second chains after the first, preserving their order
    assert anchor.moved_after == _UNSET  # anchor is READ-ONLY (Kometa coexistence)
    assert genre.moved_after == _UNSET  # a foreign hub is never touched


def test_to_top_moves_our_rows_to_position_zero_ignoring_any_anchor():
    kometa = FakeHub("Kometa Genre", "g")
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([kometa, r1])  # our row buried below a foreign hub
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"]), FakeColl("Kometa Genre", ["kometa"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", to_top=True)

    assert result["skipped"] is False and result["anchor"] == "top"
    assert r1.moved_after is None  # after=None -> the very top of the shelf
    assert kometa.moved_after == _UNSET  # foreign hub untouched


def test_to_top_is_idempotent_when_already_at_the_top():
    r1 = FakeHub("Picked for You", "o1")  # already first
    section = FakeSection([r1, FakeHub("Genre", "g")])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", to_top=True)

    assert result["skipped"] is True and result["reason"] == "already in place"
    assert r1.moved_after == _UNSET


def test_before_places_rows_ahead_of_the_anchor():
    other = FakeHub("Trending", "t")
    anchor = FakeHub("New Series", "a")
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([other, anchor, r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series", before=True)

    assert result["skipped"] is False
    assert r1.moved_after is other  # 'before New Series' == right after the hub preceding it


def test_skips_when_already_in_place():
    anchor = FakeHub("New Series", "a")
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([anchor, r1, FakeHub("Genre", "g")])  # already directly after the anchor
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert result["skipped"] is True
    assert result["reason"] == "already in place"
    assert r1.moved_after == _UNSET  # no write when nothing needs moving


def test_missing_anchor_leaves_the_shelf_untouched():
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([FakeHub("Genre", "g"), r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="Nonexistent")

    assert result["skipped"] is True
    assert result["reason"] == "anchor not found"
    assert r1.moved_after == _UNSET


def test_before_with_the_anchor_at_the_top_moves_our_row_to_position_zero():
    anchor = FakeHub("New Series", "a")  # already first
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([anchor, FakeHub("Genre", "g"), r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series", before=True)

    assert result["skipped"] is False
    assert r1.moved_after is None  # 'before' the top hub -> the very top of the shelf


def test_before_is_idempotent_when_our_row_already_precedes_the_anchor():
    r1 = FakeHub("Picked for You", "o1")  # already directly before the anchor (and at the top)
    anchor = FakeHub("New Series", "a")
    section = FakeSection([r1, anchor, FakeHub("Genre", "g")])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series", before=True)

    assert result["skipped"] is True
    assert result["reason"] == "already in place"
    assert r1.moved_after == _UNSET


def test_skips_when_our_rows_are_not_promoted_yet():
    # An owned collection exists (labelled) but isn't a managed hub — the row hasn't been promoted, so
    # there is nothing to move (managedHubs only lists promoted recommendations).
    section = FakeSection([FakeHub("New Series", "a"), FakeHub("Genre", "g")])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert result["skipped"] is True
    assert result["reason"] == "rows not promoted yet"


def test_only_keys_moves_just_that_subset_and_never_a_sibling_or_foreign_hub():
    anchor = FakeHub("New Series", "a")
    sibling = FakeHub("Picked for You", "o1")  # ours, but NOT in the requested subset
    target_row = FakeHub("Hidden Gems", "o2")  # ours, IN the subset
    foreign = FakeHub("Kometa Genre", "g")
    section = FakeSection([anchor, sibling, foreign, target_row])
    client = _client(
        [
            FakeColl("Picked for You", ["shortlist_sarah"], 101),
            FakeColl("Hidden Gems", ["shortlist_sarah"], 202),
            FakeColl("Kometa Genre", ["kometa"], 303),
        ]
    )

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series", only_keys={202})

    assert result["moved"] == ["Hidden Gems"]
    assert target_row.moved_after is anchor  # only the requested subset moves
    assert sibling.moved_after == _UNSET  # a sibling Shortlist row outside the subset is untouched
    assert foreign.moved_after == _UNSET  # a foreign (Kometa) hub is never touched


def test_only_keys_is_idempotent_when_the_subset_already_sits_after_the_anchor():
    anchor = FakeHub("New Series", "a")
    target_row = FakeHub("Hidden Gems", "o2")  # already directly after the anchor
    sibling = FakeHub("Picked for You", "o1")
    section = FakeSection([anchor, target_row, sibling])
    client = _client(
        [FakeColl("Picked for You", ["shortlist_sarah"], 101), FakeColl("Hidden Gems", ["shortlist_sarah"], 202)]
    )

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series", only_keys={202})

    assert result["skipped"] is True and result["reason"] == "already in place"
    assert target_row.moved_after == _UNSET


def test_a_row_can_never_be_anchored_to_a_sibling_shortlist_hub():
    sibling = FakeHub("Picked for You", "o1")
    target_row = FakeHub("Hidden Gems", "o2")
    section = FakeSection([sibling, target_row])
    client = _client(
        [FakeColl("Picked for You", ["shortlist_sarah"], 101), FakeColl("Hidden Gems", ["shortlist_sarah"], 202)]
    )

    # Naming our OWN sibling row as the anchor is refused (it's excluded from anchor candidates).
    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="Picked for You", only_keys={202})

    assert result["skipped"] is True and result["reason"] == "anchor not found"
    assert target_row.moved_after == _UNSET


def test_a_row_whose_label_read_comes_back_empty_is_still_ordered():
    """A collection listing that returns no <Label> must not silently disable ordering for the library.

    `collection.labels` is a per-collection re-read; one that succeeds carrying nothing looks exactly
    like an unlabelled row. Ordering only changes a position, so the invisible title marker is enough
    to recognise our own row here — otherwise the whole library is skipped, in silence.
    """
    marker = "​" * 64  # a real Shortlist marker: 64 zero-width chars
    anchor = FakeHub("New Series", "a")
    foreign = FakeHub("Kometa Genre", "g")
    r1 = FakeHub("Picked for You" + marker, "o1")
    section = FakeSection([anchor, foreign, r1])
    client = _client([FakeColl("Picked for You" + marker, [], 101), FakeColl("Kometa Genre", ["kometa"], 9)])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert result["skipped"] is False and result["verified"] is True
    assert r1.moves == 1 and foreign.moves == 0  # ours moved, the unlabelled foreign hub untouched
    assert section.titles()[1] == "Picked for You" + marker


def test_moves_only_the_hubs_that_are_out_of_place():
    """One straggler must not re-move the rows already in position.

    The old loop chained move() over every one of our hubs whenever ANY of them was misplaced, so a
    single row at the bottom of the shelf cost one PUT per row — 47 of them in 344ms on SFLIX where
    19 were needed. Fewer writes is the point; it also shrinks the window another tool can win.
    """
    anchor = FakeHub("New Series", "a")
    r1, r2 = FakeHub("Picked A", "o1"), FakeHub("Picked B", "o2")  # already in place
    foreign = FakeHub("Kometa Genre", "g")
    straggler = FakeHub("Picked C", "o3")  # stranded at the bottom
    section = FakeSection([anchor, r1, r2, foreign, straggler])
    client = _client(
        [
            FakeColl("Picked A", ["shortlist_a"], 1),
            FakeColl("Picked B", ["shortlist_b"], 2),
            FakeColl("Picked C", ["shortlist_c"], 3),
            FakeColl("Kometa Genre", ["kometa"], 9),
        ]
    )

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert result["moved"] == ["Picked C"] and result["verified"] is True
    assert (r1.moves, r2.moves, straggler.moves) == (0, 0, 1)  # only the straggler is written
    assert foreign.moves == 0
    assert section.titles() == ["New Series", "Picked A", "Picked B", "Picked C", "Kometa Genre"]


def test_reports_unverified_when_plex_accepts_the_moves_without_applying_them():
    """SFLIX 2026-08-12: 47 moves, 47 HTTP 200s, a shelf still in three blocks — reported as success.

    The shelf was being reordered by another tool every 30 minutes; the moves were fine, the CLAIM was
    not. The result must say `verified: False` rather than assert the rows were placed, because the run
    report and the logs were the only place a lost shelf could ever have been noticed.
    """
    anchor = FakeHub("New Series", "a")
    foreign = FakeHub("Kometa Genre", "g")
    r1 = FakeHub("Picked for You", "o1")
    section = DroppingSection([anchor, foreign, r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"], 101)])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series", attempts=3)

    assert result["skipped"] is False
    assert result["verified"] is False  # never claims a shelf it could not confirm
    assert result["moved"] == ["Picked for You"]  # audited once, not once per attempt
    assert r1.moves == 3  # it really did retry, re-reading the shelf between each
    assert section.titles() == ["New Series", "Kometa Genre", "Picked for You"]  # unchanged, and said so


def test_a_shelf_that_converges_on_the_very_last_attempt_is_reported_as_verified():
    """The final write must still be checked, or success gets reported as failure.

    With `attempts=3` the loop wrote on attempt 3 and fell straight through to `verified: False` —
    so a shelf this actually fixed on its last try was audited as a warning saying Plex had ignored
    us. That is the same "assert an outcome you did not check" defect this function exists to remove,
    pointed the other way. There is now one extra read that only verifies.
    """

    class LastChanceSection(FakeSection):
        def __init__(self, hubs):
            super().__init__(hubs)
            self.drops = 2  # Plex takes the first two moves and does nothing

        def apply(self, hub, after):
            if self.drops:
                self.drops -= 1
                return None
            super().apply(hub, after)

    anchor = FakeHub("New Series", "a")
    foreign = FakeHub("Kometa Genre", "g")
    r1 = FakeHub("Picked for You", "o1")
    section = LastChanceSection([anchor, foreign, r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"], 101)])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series", attempts=3)

    assert r1.moves == 3  # it took all three tries...
    assert result["verified"] is True  # ...and the third is not assumed to have failed
    assert section.titles() == ["New Series", "Picked for You", "Kometa Genre"]


def test_giving_up_still_reports_the_moves_it_already_wrote():
    """An early exit after real writes must not report `moved: []`.

    `_apply_order` drops skipped results, so a bail-out that forgot its own writes put a real Plex
    mutation outside the audit entirely (plex-safety rule 10). Here the anchor disappears between
    attempts — Kometa deleting the collection we anchor to — after we have already moved a row.
    """

    class VanishingAnchorSection(FakeSection):
        def apply(self, hub, after):
            super().apply(hub, after)
            self._hubs = [h for h in self._hubs if h.title != "New Series"]  # anchor gone

    anchor = FakeHub("New Series", "a")
    foreign = FakeHub("Kometa Genre", "g")
    r1 = FakeHub("Picked for You", "o1")
    section = VanishingAnchorSection([anchor, foreign, r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"], 101)])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert result["reason"] == "anchor not found"
    assert result["skipped"] is False  # a write happened, so this is NOT a no-op to be filtered out
    assert result["moved"] == ["Picked for You"] and result["verified"] is False


def test_retries_place_a_row_plex_dropped_on_the_first_attempt():
    """A single dropped move self-heals within the run instead of waiting for the next one."""

    class FlakySection(FakeSection):
        def __init__(self, hubs):
            super().__init__(hubs)
            self.drops = 1

        def apply(self, hub, after):
            if self.drops:  # Plex takes the first move and quietly does nothing
                self.drops -= 1
                return None
            super().apply(hub, after)

    anchor = FakeHub("New Series", "a")
    foreign = FakeHub("Kometa Genre", "g")
    r1 = FakeHub("Picked for You", "o1")
    section = FlakySection([anchor, foreign, r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"], 101)])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert result["verified"] is True
    assert r1.moves == 2  # first dropped, second stuck
    assert section.titles() == ["New Series", "Picked for You", "Kometa Genre"]


#: The delivery LEDGER — (user, row, section) -> ratingKey — which is what tells one row's hubs from
#: another's now. Durable, so it answers the same on a run with no users at all.
_LEDGER = {
    ("a", "picked", "2"): 11,
    ("b", "picked", "2"): 12,
    ("a", "gems", "2"): 21,
    ("b", "gems", "2"): 22,
}


def _order_ctx(cfg, plex, delivered_keys=None):
    import threading
    from types import SimpleNamespace

    section = SimpleNamespace(key=2, title="TV Shows")
    return SimpleNamespace(
        config=cfg,
        delivery_sections=[section],
        plex=plex,
        write_lock=threading.Lock(),
        delivered_keys=_LEDGER if delivered_keys is None else delivered_keys,
    )


def _report_with_titles():
    from datetime import UTC, datetime

    from shortlist.engine.models import RunReport, UserRunReport

    return RunReport(
        started_at=datetime.now(UTC),
        users=[
            UserRunReport(username="a", slug="a", placement_titles={"Picked A": "picked", "Gems A": "gems"}),
            UserRunReport(username="b", slug="b", placement_titles={"Picked B": "picked", "Gems B": "gems"}),
        ],
    )


def _empty_report():
    """What a `privacy.sync` produces: `engine_run(ctx, [])` — no users, and so no placement titles."""
    from datetime import UTC, datetime

    from shortlist.engine.models import RunReport

    return RunReport(started_at=datetime.now(UTC), users=[])


def test_order_phase_moves_all_rows_to_the_library_default_when_no_row_overrides():
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    cfg = EngineConfig(
        hub_anchors={"2": HubAnchor("Default Anchor", False)},
        rows=[RowSpec(slug="picked", name_template="", size=10)],
    )
    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    # One call, whole library, no row subset (the robust global path).
    plex.order_owned_hubs.assert_called_once()
    assert plex.order_owned_hubs.call_args.kwargs["only_keys"] is None
    assert plex.order_owned_hubs.call_args.kwargs["anchor_title"] == "Default Anchor"


def test_order_phase_groups_rows_by_effective_anchor_when_one_overrides():
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    cfg = EngineConfig(
        hub_anchors={"2": HubAnchor("Default Anchor", False)},  # global default
        rows=[
            RowSpec(slug="picked", name_template="", size=10),  # inherits the default
            RowSpec(slug="gems", name_template="Gems", size=10, hub_anchors={"2": HubAnchor("Gems Anchor", False)}),
        ],
    )
    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    # Two groups: the default-anchored 'picked' rows and the overridden 'gems' rows, each its own subset,
    # partitioned by the ledger's ratingKeys rather than by what this run happened to deliver.
    groups = {frozenset(c.kwargs["only_keys"]): c.kwargs["anchor_title"] for c in plex.order_owned_hubs.call_args_list}
    assert groups == {frozenset({11, 12}): "Default Anchor", frozenset({21, 22}): "Gems Anchor"}


def test_order_phase_mixes_a_top_override_with_an_anchor_override():
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    cfg = EngineConfig(
        hub_anchors={},
        rows=[
            RowSpec(slug="picked", name_template="", size=10, hub_anchors={"2": HubAnchor(to_top=True)}),
            RowSpec(
                slug="gems", name_template="Gems", size=10, hub_anchors={"2": HubAnchor(anchor_title="New Series")}
            ),
        ],
    )
    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    calls = {frozenset(c.kwargs["only_keys"]): c.kwargs for c in plex.order_owned_hubs.call_args_list}
    assert calls[frozenset({11, 12})]["to_top"] is True
    gems = calls[frozenset({21, 22})]
    assert gems["to_top"] is False and gems["anchor_title"] == "New Series"


def test_order_phase_applies_a_before_override_with_no_global_default():
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    cfg = EngineConfig(
        hub_anchors={},  # no global default at all
        rows=[RowSpec(slug="gems", name_template="Gems", size=10, hub_anchors={"2": HubAnchor("Gems Anchor", True)})],
    )
    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    plex.order_owned_hubs.assert_called_once()
    kwargs = plex.order_owned_hubs.call_args.kwargs
    assert kwargs["before"] is True and kwargs["anchor_title"] == "Gems Anchor"
    # The one row here is the only one anchored, so there is nothing to tell apart: move them all.
    assert kwargs["only_keys"] is None


def test_order_phase_still_orders_on_a_run_with_no_users():
    """The SFLIX bug: a `privacy.sync` reached the ordering phase 31 times in a day and moved nothing.

    A per-library `hub_anchor` override sent this down a path that took the rows to move from
    `report.users[].placement_titles` — only ever populated by the run in progress. `engine_run(ctx, [])`
    has no users, so the set came out empty, no group was built, and NOT ONE move was issued, silently.
    That is why "Fix privacy" and "Fix rows" could never repair a shelf.
    """
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"], "verified": True}
    cfg = EngineConfig(  # exactly SFLIX's shape: one row, a per-library override, no global default
        hub_anchors={},
        rows=[
            RowSpec(
                slug="picked",
                name_template="",
                size=10,
                hub_anchors={"2": HubAnchor("Recently Added TV", False)},
            )
        ],
    )

    _order_phase(_order_ctx(cfg, plex), _empty_report())

    plex.order_owned_hubs.assert_called_once()
    kwargs = plex.order_owned_hubs.call_args.kwargs
    assert kwargs["anchor_title"] == "Recently Added TV"
    assert kwargs["only_keys"] is None  # every row, not "whoever ran tonight"


def test_order_phase_orders_a_row_that_delivered_nothing_this_run():
    """A row nobody was delivered tonight still owns hubs on the shelf, and they still need placing.

    This used to be asserted the other way round ("skips an overridden row with no delivered titles"),
    which is the bug written down as a requirement: a paused, errored or simply skipped user's row was
    dropped from the ordering pass and drifted to the bottom of the shelf for good.
    """
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"], "verified": True}
    cfg = EngineConfig(
        hub_anchors={"2": HubAnchor("Default", False)},
        rows=[
            RowSpec(slug="ghost", name_template="Ghost", size=10, hub_anchors={"2": HubAnchor("Ghost Anchor", False)})
        ],
    )

    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    plex.order_owned_hubs.assert_called_once()
    assert plex.order_owned_hubs.call_args.kwargs["anchor_title"] == "Ghost Anchor"


def test_order_phase_uses_the_library_default_when_no_row_is_left_to_ask():
    """Every row deleted or switched off, but the library still has an anchor — and still has our rows.

    Retired collections stay on the shelf, so skipping the library because `config.rows` is empty is
    another silent do-nothing in the function whose whole bug was a silent do-nothing.
    """
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"], "verified": True}
    cfg = EngineConfig(hub_anchors={"2": HubAnchor("Recently Added TV", False)}, rows=[], rows_defined=True)

    _order_phase(_order_ctx(cfg, plex), _empty_report())

    plex.order_owned_hubs.assert_called_once()
    kwargs = plex.order_owned_hubs.call_args.kwargs
    assert kwargs["anchor_title"] == "Recently Added TV" and kwargs["only_keys"] is None


def test_order_phase_leaves_a_diverging_row_alone_until_the_ledger_knows_it():
    """When rows genuinely disagree they must be partitioned, and the ledger is the only handle.

    A row with no delivered collection here yet cannot be told apart from its siblings, so it is left
    for the next run rather than swept into another row's group.
    """
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"], "verified": True}
    cfg = EngineConfig(
        hub_anchors={},
        rows=[
            RowSpec(slug="picked", name_template="", size=10, hub_anchors={"2": HubAnchor("Anchor A", False)}),
            RowSpec(slug="ghost", name_template="Ghost", size=10, hub_anchors={"2": HubAnchor("Anchor B", False)}),
        ],
    )

    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    # 'picked' is in the ledger and gets placed; 'ghost' has never delivered here, so it is skipped.
    plex.order_owned_hubs.assert_called_once()
    kwargs = plex.order_owned_hubs.call_args.kwargs
    assert kwargs["anchor_title"] == "Anchor A" and set(kwargs["only_keys"]) == {11, 12}


def test_dry_run_reports_the_move_without_writing():
    anchor = FakeHub("New Series", "a")
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([anchor, FakeHub("Genre", "g"), r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series", dry_run=True)

    assert result["dry_run"] is True
    assert result["moved"] == ["Picked for You"]
    assert r1.moved_after == _UNSET  # dry-run never actually moves a hub
