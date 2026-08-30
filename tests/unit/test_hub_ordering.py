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

import threading
from datetime import UTC, datetime

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.models import HubAnchor as HubAnchorModel

_UNSET = "UNSET"  # sentinel: move() was never called on this hub


class FakeHub:
    """A managed hub. Carries the three promotion flags a real ``managedHubs()`` entry has.

    They default to promoted-on-shared-Home because that is what a row on the shelf looks like, and
    because a fake WITHOUT these attributes would have hidden the fact that `managedHubs()` also
    lists hubs promoted nowhere — which is exactly what `order_owned_hubs` was wasting moves on.
    """

    def __init__(self, title: str, ident: str, *, promoted: bool = True, collection: bool = True, identifier: str = ""):
        self.title = title
        # A COLLECTION's hub carries an identifier in the `custom.collection` FAMILY. Not a format:
        # the two shapes recorded off a real PMS are `custom.collection.1.527794.527794` and
        # `custom.collection.571285`, and plexapi's synthesized
        # `custom.collection.<sectionID>.<ratingKey>` matches neither — so the string built below is
        # one arbitrary member of the family, never evidence of what Plex sends. A built-in hub
        # carries an identifier of another kind; `collection=False` models one, which is what tells
        # the ordering guard the two apart. See `is_collection_hub`.
        self.identifier = identifier or (f"custom.collection.2.{ident}" if collection else ident)
        self.promotedToSharedHome = promoted
        self.promotedToOwnHome = False
        self.promotedToRecommended = False
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


def test_the_guard_against_the_real_manage_endpoint_capture():
    """`is_collection_hub` / `is_promoted` / `can_anchor` against a RECORDED
    `GET /hubs/sections/1/manage` — the exact endpoint `managedHubs()` reads (plex-safety rule 11).

    Everything the #106 guard rests on was inferred until this capture existed: the identifier family,
    and whether Plex's built-in hubs even carry the promotion flags. They do — so an owner who
    switches a built-in off in Manage Recommendations gets all three at 0, which is precisely why the
    guard must never judge one. `can_anchor` keeps every built-in usable regardless.
    """
    import xml.etree.ElementTree as ET
    from pathlib import Path
    from types import SimpleNamespace

    from shortlist.engine.clients.plex_pms import can_anchor, is_collection_hub, is_promoted

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "pms_managed_hubs.xml.txt"
    hubs = [
        SimpleNamespace(
            identifier=el.get("identifier"),
            title=el.get("title"),
            promotedToRecommended=el.get("promotedToRecommended") == "1",
            promotedToOwnHome=el.get("promotedToOwnHome") == "1",
            promotedToSharedHome=el.get("promotedToSharedHome") == "1",
        )
        for el in ET.parse(fixture).getroot()
    ]
    assert len(hubs) >= 8  # a re-record that empties this must fail, not pass vacuously

    by_ident = {h.identifier: h for h in hubs}
    # Collections are told apart by identifier family, built-ins by theirs.
    assert [h.identifier for h in hubs if is_collection_hub(h)] == [
        "custom.collection.1.683081",
        "custom.collection.1.577628",
        "custom.collection.1.343546",
    ]
    # Built-ins really do carry the flags, and a switched-off one reads unpromoted...
    assert not is_promoted(by_ident["movie.recentlyreleased"])
    assert not is_promoted(by_ident["movie.genre"])
    # ...yet stays a usable anchor, because the guard judges collections only. This is the review
    # finding that would have been a worse bug than #106: refusing "Recently Released" as an anchor.
    assert all(can_anchor(h) for h in hubs if not is_collection_hub(h))
    # Every collection on this server was on the Recommended shelf, so all of them can anchor. The
    # off-shelf case #106 is about is NOT in this capture — see tests/fixtures/README.md.
    assert all(can_anchor(h) for h in hubs)


def test_is_collection_hub_accepts_every_identifier_shape_a_real_pms_has_produced():
    """The guard's one piece of evidence, pinned to RECORDED captures rather than to plexapi's guess.

    The two shapes in the fixtures agree on nothing after the family name — one carries a section id
    and a DOUBLED ratingKey, the other no section id at all. Matching plexapi's synthesized
    `custom.collection.<sectionID>.<ratingKey>` rejects both, and each rejection silently disables the
    #106 guard for that hub. Read from `hubIdentifier` because that is the key `/hubs` uses; the
    manage endpoint calls it `identifier`, and no fixture records it (see `is_collection_hub`).
    """
    import json
    from pathlib import Path

    from shortlist.engine.clients.plex_pms import is_collection_hub

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    recorded = {
        ident
        for name in ("pms_hubs_home.json", "pms_hubs_shared_account.json")
        for ident in _hub_identifiers(json.loads((fixtures / name).read_text()))
        if ident.startswith("custom.collection")
    }
    # Guard the guard: if a re-record ever drops these, this test must fail rather than pass vacuously.
    assert len(recorded) >= 2, recorded
    for ident in recorded:
        assert is_collection_hub(FakeHub("x", "", collection=False, identifier=ident)), ident

    # Built-ins are a different family and must never be judged.
    for ident in ("home.television.recentlyadded", "movie.recentlyadded", "home.movies.toprated", ""):
        assert not is_collection_hub(FakeHub("x", "", collection=False, identifier=ident)), ident


def _hub_identifiers(node) -> list[str]:
    """Every hub identifier anywhere in a recorded hubs payload, under either key `/hubs` uses."""
    if isinstance(node, dict):
        found = [v for k in ("hubIdentifier", "identifier") if isinstance(v := node.get(k), str)]
        return found + [i for v in node.values() for i in _hub_identifiers(v)]
    if isinstance(node, list):
        return [i for v in node for i in _hub_identifiers(v)]
    return []


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


def test_an_unpromoted_collection_anchor_leaves_the_shelf_untouched():
    """Issue #106: the anchor is a COLLECTION in this library that is on no shelf.

    The matrix cell every neighbour missed — `promoted=False` was only ever tested on OUR rows and on
    a ROW anchor, never on a foreign collection one, which is the kind the picker actually offers.
    Following it moved the row after a hub with no visible position and buried it below every
    standard Plex hub, and the verify pass then agreed the shelf was exactly what we asked for.
    """
    archive = FakeHub("Archive 2019", "a", promoted=False)  # a real collection, not on the shelf
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([FakeHub("Recently Added", "g"), r1, archive])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"]), FakeColl("Archive 2019", [])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="Archive 2019")

    assert result["skipped"] is True
    # Told apart from a DELETED anchor on purpose: the two need different things from the owner.
    assert result["reason"] == "anchor not on the shelf"
    assert r1.moved_after == _UNSET
    assert section.titles() == ["Recently Added", "Picked for You", "Archive 2019"]


def test_a_built_in_hub_is_not_refused_even_when_a_collection_shares_its_title():
    """Titles COLLIDE: "Top Rated" is both a stock Plex hub and a stock Kometa collection.

    The guard identifies a collection by the hub's own `custom.collection.*` identifier, never by
    matching its title against the library's collection list. A title check refused the built-in here
    and stopped ordering the whole library — and a title check has to be answered from
    `section.collections()`, a listing that can come back SHORT, which would reclassify a real
    collection as a built-in and wave the #106 burial straight back through.
    """
    builtin = FakeHub("Top Rated", "home.television.toprated", promoted=False, collection=False)
    same_name = FakeHub("Top Rated", "77", promoted=False)  # a Kometa collection, on no shelf
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([builtin, same_name, FakeHub("Kometa Genre", "g"), r1])
    client = _client(
        [
            FakeColl("Picked for You", ["shortlist_sarah"]),
            FakeColl("Top Rated", ["kometa"], 77),
            FakeColl("Kometa Genre", ["kometa"]),
        ]
    )

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="Top Rated")

    assert result["skipped"] is False and result["verified"] is True
    assert section.titles() == ["Top Rated", "Picked for You", "Top Rated", "Kometa Genre"]


def test_a_short_collections_read_cannot_wave_an_off_shelf_anchor_through():
    """The anchor's own identifier says it is a collection, so a listing that omits it changes nothing.

    Modelled on `covers_window` for watched titles: a PMS that under-reports a container is a failure
    mode this codebase already takes seriously, and the previous title-based guard turned one into a
    silent `verified: True` over a buried row.
    """
    archive = FakeHub("Archive 2019", "55", promoted=False)
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([FakeHub("Recently Added", "g"), r1, archive])
    # The listing carries our row but NOT the anchor — a truncated read.
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="Archive 2019")

    assert result["skipped"] is True and result["reason"] == "anchor not on the shelf"
    assert r1.moved_after == _UNSET


def test_the_reasons_that_stop_a_placement_are_the_ones_the_pipeline_records():
    """`pipeline.UNPLACEABLE` matches on reason STRINGS this module returns. Reword one here and the
    audit recording stops silently, with every other test still green — they feed hand-written
    dicts. This drives the real client into each state instead."""
    from shortlist.engine.pipeline import UNPLACEABLE

    def reason_for(hubs, colls, **kwargs) -> str:
        return _client(colls).order_owned_hubs(FakeSection(hubs), label_prefix="shortlist", **kwargs)["reason"]

    ours = [FakeColl("Picked for You", ["shortlist_sarah"], 101)]

    # (a) the named collection is nowhere on the shelf at all
    assert (
        reason_for([FakeHub("Genre", "g"), FakeHub("Picked for You", "101")], ours, anchor_title="Nonexistent")
        in UNPLACEABLE
    )
    # (b) it is there but on no shelf
    off = FakeHub("Archive 2019", "55", promoted=False)
    assert reason_for([off, FakeHub("Picked for You", "101")], ours, anchor_title="Archive 2019") in UNPLACEABLE
    # (c) a ROW anchor with nothing promoted here
    dormant = FakeHub("Gems", "202", promoted=False)
    assert (
        reason_for(
            [dormant, FakeHub("Picked for You", "101")],
            [*ours, FakeColl("Gems", ["shortlist_mike"], 202)],
            anchor_keys={202},
            only_keys={101},
        )
        in UNPLACEABLE
    )


def test_a_built_in_plex_hub_anchor_is_never_refused():
    """The other half of the #106 guard, and the one that would be a WORSE bug than #106 if it broke.

    "Recently Added" is an ordinary anchor — `test_order_phase_still_orders_on_a_run_with_no_users`
    configures exactly that — and it is not a collection, so nothing in this repo has ever read a
    promotion flag off one and there is no recorded fixture for its shape (plex-safety rule 11). The
    guard therefore judges COLLECTIONS only — told apart by the hub's own `custom.collection.*`
    identifier: refusing to place a row needs positive evidence, and a flag we cannot vouch for must
    never be the reason a working placement silently stops. This fake reports every flag off, which is
    the worst case the real server could hand us.
    """
    builtin = FakeHub("Recently Added", "home.television.recentlyadded", promoted=False, collection=False)
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([builtin, FakeHub("Kometa Genre", "g"), r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"]), FakeColl("Kometa Genre", ["kometa"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="Recently Added")

    assert result["skipped"] is False and result["verified"] is True
    assert section.titles() == ["Recently Added", "Picked for You", "Kometa Genre"]


def test_an_anchor_on_the_recommended_shelf_alone_still_places_the_row():
    """ANY one promotion flag is a real position, so the check must not tighten into "promoted to
    shared Home". A collection promoted only to the library's Recommended shelf is the ordinary case
    for a Kometa-managed anchor."""
    anchor = FakeHub("New Series", "a", promoted=False)
    anchor.promotedToRecommended = True
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([anchor, FakeHub("Recently Added", "g"), r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"]), FakeColl("New Series", [])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert result["skipped"] is False and result["verified"] is True
    assert section.titles() == ["New Series", "Picked for You", "Recently Added"]


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


def test_a_row_promoted_nowhere_is_left_where_it_is():
    """A dormant row (paused/disabled user) is on no surface, so its position is invisible.

    `managedHubs()` lists it anyway, and moving it was pure churn: 4 wasted writes per library per
    pass on SFLIX, which also kept a reconciled shelf looking contested — a co-managing tool
    (agregarr) correctly ignores rows promoted nowhere, so Shortlist alone kept shuffling them.
    """
    anchor = FakeHub("New Series", "a")
    live = FakeHub("Picked A", "o1")
    dormant = FakeHub("Picked B", "o2", promoted=False)  # on no surface at all
    section = FakeSection([anchor, dormant, live])
    client = _client([FakeColl("Picked A", ["shortlist_a"], 1), FakeColl("Picked B", ["shortlist_b"], 2)])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert live.moves == 1
    assert dormant.moves == 0  # never written
    assert result["moved"] == ["Picked A"]
    assert section.titles() == ["New Series", "Picked A", "Picked B"]


def test_a_row_on_the_owners_home_alone_is_still_ordered():
    """Any single promotion flag counts — a row the owner can see has a position worth placing."""
    anchor = FakeHub("New Series", "a")
    owner_only = FakeHub("Picked A", "o1", promoted=False)
    owner_only.promotedToOwnHome = True
    section = FakeSection([anchor, FakeHub("Kometa Genre", "g"), owner_only])
    client = _client([FakeColl("Picked A", ["shortlist_a"], 1)])

    client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series")

    assert owner_only.moves == 1


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

    # `type` is not optional on this fake: a real `LibrarySection` always carries it, and the order
    # phase now asks `target_sections` which rows deliver here — a fake without it was easier than
    # the server (testing rule) and hid the perpetual-INFO bug below.
    section = SimpleNamespace(key=2, title="TV Shows", type="show")
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

    # Two groups, named two DIFFERENT ways. The row the owner placed by hand is enumerated from the
    # ledger; everything else is placed by EXCLUSION, naming no ratingKeys of its own.
    #
    # That asymmetry is the fix for the reporter's shelf. Enumerating both meant any collection of
    # ours the ledger did not name belonged to no group, was never passed to the client, and stayed
    # where Plex appended it — the bottom. Only the hand-placed rows now depend on the ledger, and
    # those are the ones it reliably names.
    calls = plex.order_owned_hubs.call_args_list
    rest, gems = calls[0].kwargs, calls[1].kwargs
    assert rest["anchor_title"] == "Default Anchor"
    assert rest["only_keys"] is None and rest["exclude_keys"] == {21, 22}
    assert gems["anchor_title"] == "Gems Anchor"
    assert gems["only_keys"] == {21, 22}
    # The catch-all goes FIRST, so the hand-placed rows anchor against a shelf that has settled.
    assert [c.kwargs["anchor_title"] for c in calls] == ["Default Anchor", "Gems Anchor"]


def test_a_row_the_ledger_does_not_name_is_still_placed():
    """Issue #106 as the reporter actually hit it — a full shelf, not a mocked client.

    Their setup: two rows following the Settings default (top of the shelf), one row anchored after a
    collection. Their logs showed the DEFAULT group holding fewer hubs than the anchored one, and a
    screenshot with rows stranded at the bottom under the standard Plex hubs.

    The only thing varied here is which collections the delivery ledger names. Every group used to be
    enumerated from it, so a collection it did not name was in no group, never reached the client, and
    stayed where Plex appended it. Placing the default group by EXCLUSION removes the dependency for
    exactly the rows that do not need it.
    """
    from types import SimpleNamespace

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec, RunReport
    from shortlist.engine.pipeline import _order_phase

    users = ("ann", "bob")
    hubs = [FakeHub("Letzte Chance", "lc", collection=False), FakeHub("Recently Added", "ra", collection=False)]
    colls = [FakeColl("Letzte Chance", []), FakeColl("Recently Added", [])]
    ledger, key = {}, 100
    for row in ("star", "crown", "bullseye"):
        for user in users:
            key += 1
            hubs.append(FakeHub(f"{row}-{user}", str(key)))
            colls.append(FakeColl(f"{row}-{user}", [f"shortlist_{user}"], key))
            # The ledger names every hand-placed row, and only ONE of the four that follow the
            # default — the gap that stranded the rest.
            if row == "bullseye" or (row == "star" and user == "ann"):
                ledger[(user, row, "1")] = key

    section = FakeSection(hubs, title="Filme", key=1)
    section.type = "movie"
    cfg = EngineConfig(
        manage_shelf_order=True,
        hub_anchors={"1": HubAnchor(to_top=True)},
        rows=[
            RowSpec(slug="star", name_template="Star", size=10),
            RowSpec(slug="crown", name_template="Crown", size=10),
            RowSpec(slug="bullseye", name_template="Bullseye", size=10, hub_anchors={"1": HubAnchor("Letzte Chance")}),
        ],
    )
    ctx = SimpleNamespace(
        config=cfg,
        delivery_sections=[section],
        plex=_client(colls),
        write_lock=threading.Lock(),
        delivered_keys=ledger,
    )
    _order_phase(ctx, RunReport(started_at=datetime.now(UTC), users=[]))

    # Every default row at the top, the hand-placed row behind its anchor, nothing left at the bottom.
    assert section.titles() == [
        "star-ann",
        "star-bob",
        "crown-ann",
        "crown-bob",
        "Letzte Chance",
        "bullseye-ann",
        "bullseye-bob",
        "Recently Added",
    ]

    # And it CONVERGES: the reporter's other complaint was the layout jumping around, so a settled
    # shelf must cost zero further writes.
    moves = sum(h.moves for h in hubs)
    _order_phase(ctx, RunReport(started_at=datetime.now(UTC), users=[]))
    assert sum(h.moves for h in hubs) == moves


def test_a_row_can_still_follow_a_row_that_uses_the_library_default():
    """The cell the catch-all could have broken (issue #81 meets #106).

    Rows following the default are placed by exclusion and so belong to no enumerated group. A row
    anchored to one of them must therefore name that block BY EXCLUSION too — not from the ledger,
    and not from the anchor row's own hubs, both of which aim inside a contiguous block and never
    converge. The follower must also stay out of `group_of_slug`, or the topological sort puts a
    group into the placement order that `groups` has no keys for and the run dies with a KeyError.
    """
    from types import SimpleNamespace

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec, RunReport
    from shortlist.engine.pipeline import _order_phase

    hubs = [FakeHub("Recently Added", "ra", collection=False)]
    colls = [FakeColl("Recently Added", [])]
    ledger, key = {}, 100
    # TWO rows follow the default, over TWO accounts. Both matter: with one of either, the anchor
    # row's last hub IS the block's last hub, which is the single combination that works by accident.
    for row in ("picked", "gems", "extra"):
        for user in ("ann", "bob"):
            key += 1
            hubs.append(FakeHub(f"{row}-{user}", str(key)))
            colls.append(FakeColl(f"{row}-{user}", [f"shortlist_{user}"], key))
            ledger[(user, row, "1")] = key

    section = FakeSection(hubs, title="Filme", key=1)
    section.type = "movie"
    cfg = EngineConfig(
        manage_shelf_order=True,
        hub_anchors={"1": HubAnchor(to_top=True)},  # 'picked' and 'gems' both follow this
        rows=[
            RowSpec(slug="picked", name_template="Picked", size=10),
            RowSpec(slug="gems", name_template="Gems", size=10),
            RowSpec(slug="extra", name_template="Extra", size=10, hub_anchors={"1": HubAnchor(anchor_row="picked")}),
        ],
    )
    ctx = SimpleNamespace(
        config=cfg,
        delivery_sections=[section],
        plex=_client(colls),
        write_lock=threading.Lock(),
        delivered_keys=ledger,
    )
    _order_phase(ctx, RunReport(started_at=datetime.now(UTC), users=[]))

    # 'extra' sits after the whole default BLOCK, which stays contiguous — not wedged inside it.
    assert section.titles() == [
        "picked-ann",
        "picked-bob",
        "gems-ann",
        "gems-bob",
        "extra-ann",
        "extra-bob",
        "Recently Added",
    ]

    # And it CONVERGES. Aimed at the anchor row's own hubs, 'extra' lands inside the default block,
    # the next catch-all evicts it, and the two calls trade places at one PUT per account per library
    # every run — while both report `verified: True` and `_shelf_contention` blames Kometa for it.
    moves = sum(h.moves for h in hubs)
    _order_phase(ctx, RunReport(started_at=datetime.now(UTC), users=[]))
    assert sum(h.moves for h in hubs) == moves


def test_a_hand_placed_row_missing_from_the_ledger_is_reported_not_silently_defaulted():
    """The one row the catch-all cannot honour, and it must say so.

    A row with its own placement is identified by the ledger. Without an entry, its collections
    cannot be told from anyone else's — so they cannot be excluded either, and the catch-all sweeps
    them to the library default instead of the slot the owner picked. Better than the stranding this
    replaced, still not what was asked for, so it is audited rather than reported as a no-op.
    """
    from types import SimpleNamespace

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec, RunReport
    from shortlist.engine.pipeline import _order_phase

    hubs = [FakeHub("Letzte Chance", "lc", collection=False), FakeHub("picked-ann", "11"), FakeHub("extra-ann", "21")]
    colls = [FakeColl("picked-ann", ["shortlist_ann"], 11), FakeColl("extra-ann", ["shortlist_ann"], 21)]
    section = FakeSection(hubs, title="Filme", key=1)
    section.type = "movie"
    cfg = EngineConfig(
        manage_shelf_order=True,
        hub_anchors={"1": HubAnchor(to_top=True)},
        rows=[
            RowSpec(slug="picked", name_template="Picked", size=10),
            RowSpec(slug="extra", name_template="Extra", size=10, hub_anchors={"1": HubAnchor("Letzte Chance")}),
        ],
    )
    ctx = SimpleNamespace(
        config=cfg,
        delivery_sections=[section],
        plex=_client(colls),
        write_lock=threading.Lock(),
        delivered_keys={("ann", "picked", "1"): 11},  # nothing for 'extra'
    )
    report = RunReport(started_at=datetime.now(UTC), users=[])
    _order_phase(ctx, report)

    unplaced = [e for e in report.hub_orderings if e.get("placed") is False]
    assert len(unplaced) == 1
    # The displaced row goes in its OWN key: `anchor` means "the thing we anchored to" everywhere
    # else, and both audit writers emit it under that name (rule 10).
    assert unplaced[0]["row"] == "Extra" and unplaced[0]["anchor"] == ""
    assert "delivery ledger" in unplaced[0]["reason"]
    assert unplaced[0]["library"] == "Filme"


def test_placing_a_row_before_one_we_also_place_is_refused_not_churned():
    """ "Right before <one of our rows>" cannot be satisfied, so it is recorded instead of written.

    We put the anchor row's block at a fixed point and make it contiguous, so anything inserted ahead
    of it is evicted next pass and re-inserted by this one. Measured 6, 6, 6, 6 moves per pass against
    a default-following anchor and 4, 4, 4, 4 against an enumerated one — for ever, with both calls
    reporting `verified: True`, and `_shelf_contention` blaming Kometa for Shortlist's own writes.

    'after' is unaffected, and so is 'before' a FOREIGN collection: neither is a row we position.
    """
    from types import SimpleNamespace

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec, RunReport
    from shortlist.engine.pipeline import _order_phase

    hubs = [FakeHub("Recently Added", "ra", collection=False)]
    colls = [FakeColl("Recently Added", [])]
    ledger, key = {}, 100
    for row in ("picked", "gems", "extra"):
        for user in ("ann", "bob"):
            key += 1
            hubs.append(FakeHub(f"{row}-{user}", str(key)))
            colls.append(FakeColl(f"{row}-{user}", [f"shortlist_{user}"], key))
            ledger[(user, row, "1")] = key
    section = FakeSection(hubs, title="Filme", key=1)
    section.type = "movie"
    cfg = EngineConfig(
        manage_shelf_order=True,
        hub_anchors={"1": HubAnchor(to_top=True)},
        rows=[
            RowSpec(slug="picked", name_template="Picked", size=10),
            RowSpec(slug="gems", name_template="Gems", size=10),
            RowSpec(
                slug="extra",
                name_template="Extra",
                size=10,
                hub_anchors={"1": HubAnchor(anchor_row="picked", before=True)},
            ),
        ],
    )
    ctx = SimpleNamespace(
        config=cfg,
        delivery_sections=[section],
        plex=_client(colls),
        write_lock=threading.Lock(),
        delivered_keys=ledger,
    )
    report = RunReport(started_at=datetime.now(UTC), users=[])
    _order_phase(ctx, report)

    unplaced = [e for e in report.hub_orderings if e.get("placed") is False]
    assert len(unplaced) == 1
    assert "cannot sit before a row Shortlist also places" in unplaced[0]["reason"]
    assert unplaced[0]["anchor"] == "the 'Picked' row"
    assert unplaced[0]["row"] == "Extra"  # the refused row is NAMED, not just the library (rule 10)

    # Refused is not stranded. Its rows take the library default, so they are at the top with the
    # rest — dropping them from every call instead left them under the standard Plex hubs for ever,
    # which is the complaint this whole issue is about.
    assert section.titles() == [
        "picked-ann",
        "picked-bob",
        "gems-ann",
        "gems-bob",
        "extra-ann",
        "extra-bob",
        "Recently Added",
    ]

    # And the shelf settles — no perpetual writes.
    moves = sum(h.moves for h in hubs)
    _order_phase(ctx, RunReport(started_at=datetime.now(UTC), users=[]))
    assert sum(h.moves for h in hubs) == moves


def test_before_is_only_refused_when_shortlist_also_places_the_anchor_row():
    """The other two cells of the refusal, neither of which the sibling test can see.

    Refused when the anchor row has its OWN placement (we position it), and NOT refused when nobody
    positions it — a row nobody moves is already wherever it is, so sitting before it is stable and
    must keep working. Over-refusing would take away a placement that does exactly what was asked.
    """
    from types import SimpleNamespace

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec, RunReport
    from shortlist.engine.pipeline import _order_phase

    def shelf(rows, hub_anchors):
        hubs = [FakeHub("Recently Added", "ra", collection=False)]
        colls = [FakeColl("Recently Added", [])]
        ledger, key = {}, 100
        for spec in rows:
            key += 1
            hubs.append(FakeHub(f"{spec.slug}-ann", str(key)))
            colls.append(FakeColl(f"{spec.slug}-ann", ["shortlist_ann"], key))
            ledger[("ann", spec.slug, "1")] = key
        section = FakeSection(hubs, title="Filme", key=1)
        section.type = "movie"
        ctx = SimpleNamespace(
            config=EngineConfig(manage_shelf_order=True, hub_anchors=hub_anchors, rows=rows),
            delivery_sections=[section],
            plex=_client(colls),
            write_lock=threading.Lock(),
            delivered_keys=ledger,
        )
        report = RunReport(started_at=datetime.now(UTC), users=[])
        _order_phase(ctx, report)
        moves = sum(h.moves for h in hubs)
        _order_phase(ctx, RunReport(started_at=datetime.now(UTC), users=[]))
        return report, section.titles(), sum(h.moves for h in hubs) == moves

    # (a) the anchor row has its own placement -> we position it -> refused.
    report, _, settled = shelf(
        [
            RowSpec(slug="picked", name_template="Picked", size=10, hub_anchors={"1": HubAnchor("Recently Added")}),
            RowSpec(
                slug="extra",
                name_template="Extra",
                size=10,
                hub_anchors={"1": HubAnchor(anchor_row="picked", before=True)},
            ),
        ],
        {"1": HubAnchor(to_top=True)},
    )
    assert [e["row"] for e in report.hub_orderings if e.get("placed") is False] == ["Extra"]
    assert settled

    # (b) nobody positions the anchor row (no library default, no override on it) -> it works.
    report, titles, settled = shelf(
        [
            RowSpec(slug="picked", name_template="Picked", size=10),
            RowSpec(
                slug="extra",
                name_template="Extra",
                size=10,
                hub_anchors={"1": HubAnchor(anchor_row="picked", before=True)},
            ),
        ],
        {},  # Settings leaves this library's order to Plex
    )
    assert [e for e in report.hub_orderings if e.get("placed") is False] == []
    assert titles.index("extra-ann") < titles.index("picked-ann")
    assert settled


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


def test_order_phase_partitions_when_a_row_here_has_no_anchor_at_all():
    """The matrix cell the neighbours miss: anchors that AGREE, plus a row with none.

    `len(distinct) == 1` is true, so the cheap "move everything in one call" branch is one condition
    away from firing — `unanchored` is the only thing forcing the ledger partition. That changes
    `only_keys` from None (every owned row in the library) to a subset, which is a materially
    different call: an unanchored row must be left where it is, not dragged to the anchored slot.
    Every other test here has a single row, so `unanchored` is always empty and this arm never runs.
    """
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    cfg = EngineConfig(
        hub_anchors={},  # no global default, so 'loose' resolves to no anchor anywhere
        rows=[
            RowSpec(slug="picked", name_template="Picked", size=10, hub_anchors={"2": HubAnchor("", False, True)}),
            RowSpec(slug="loose", name_template="Loose", size=10),
        ],
    )
    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    plex.order_owned_hubs.assert_called_once()
    kwargs = plex.order_owned_hubs.call_args.kwargs
    assert kwargs["only_keys"] == {11, 12}, "only the anchored row moves; the unanchored one stays put"
    assert kwargs["to_top"] is True


def test_order_phase_records_a_placement_it_could_not_honour():
    """Issue #106: a configured placement we could not apply must reach the audit, not just the log.

    `_apply_order` recorded only calls that MOVED something, so "your anchor is not on the shelf"
    was a container-log warning and nothing else — the Rows page went on showing a setting that had
    silently done nothing since the night it was saved.
    """
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {
        "anchor": "Archive 2019",
        "moved": [],
        "skipped": True,
        "reason": "anchor not on the shelf",
    }
    cfg = EngineConfig(
        hub_anchors={},
        rows=[RowSpec(slug="gems", name_template="Gems", size=10, hub_anchors={"2": HubAnchor("Archive 2019")})],
    )
    report = _report_with_titles()
    _order_phase(_order_ctx(cfg, plex), report)

    assert report.hub_orderings == [
        {
            "library": "TV Shows",
            # NOT `verified` — nothing was asked of Plex, so that question has no answer here. Its own
            # key, and its own audit scope, so contention detection never counts it (rule 10).
            "placed": False,
            "anchor": "Archive 2019",
            "moved": [],
            "skipped": True,
            "reason": "anchor not on the shelf",
        }
    ]


def test_order_phase_does_not_record_the_ordinary_skips():
    """A converged server skips every library every night. Recording those would put a warning per
    library per run into the audit and bury the one that means something."""
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"anchor": "top", "moved": [], "skipped": True, "reason": "already in place"}
    cfg = EngineConfig(
        hub_anchors={"2": HubAnchor(to_top=True)},
        rows=[RowSpec(slug="picked", name_template="", size=10)],
    )
    report = _report_with_titles()
    _order_phase(_order_ctx(cfg, plex), report)

    assert report.hub_orderings == []


def test_order_phase_ignores_rows_that_never_deliver_to_this_library():
    """A movies-only row has no business in a TV library's shelf decision.

    Without the media/`library_keys` filter it picked up this library's anchor, could never have a
    ledger entry here, and logged "no delivered collection in TV Shows yet — it will be placed once
    that row has been built here" on every run, privacy sync and Fix, for ever. That line is INFO and
    owner-visible, and the promise in it can never come true — worse than the silence it replaced.

    It also cost the shelf. The show row here has its own anchor, so an unfiltered list makes the two
    rows "disagree", which forces this library off the single-call path onto the ledger-partitioned
    one — and there any hub of ours the ledger does not name (a retired row's leftovers) stops being
    repositioned at all. Filtered, the show row is the only one here, so nothing disagrees.
    """
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    cfg = EngineConfig(
        hub_anchors={"2": HubAnchor(to_top=True)},  # the global default the movie rows would inherit
        rows=[
            RowSpec(
                slug="picked",
                name_template="Picked",
                size=10,
                media="show",
                hub_anchors={"2": HubAnchor(anchor_title="Recently Added")},
            ),
            RowSpec(slug="movienight", name_template="Movie Night", size=10, media="movie"),
            # Targets a library by key, and not this one — the other half of `target_sections`.
            RowSpec(slug="elsewhere", name_template="Elsewhere", size=10, library_keys=["9"]),
        ],
    )
    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    # One call, whole library, no row subset: the show row is the only one that builds here, so there
    # is nothing to tell apart. An unfiltered list partitions instead and passes `only_keys`.
    plex.order_owned_hubs.assert_called_once()
    kwargs = plex.order_owned_hubs.call_args.kwargs
    assert kwargs["only_keys"] is None
    assert kwargs["anchor_title"] == "Recently Added"


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


def test_a_plexapi_failure_is_redacted_before_it_reaches_the_log():
    """plexapi error text is credential-bearing, and this log line is derived from it (rule 9).

    plexapi raises `f'({status}) {codename}; {response.url} {errtext}'` (server.py `query`), and
    `response.url` carries `X-Plex-Token` whenever plexapi's `log.show_secrets` is on. `PlexConfig.get`
    reads the ENVIRONMENT first, so `PLEXAPI_LOG_SHOW_SECRETS=true` on the container is enough to put
    a live token in that message without any change to our code. Safe-by-default is not guarded, and
    `redact`'s own docstring says anything derived from an exception message must pass through it.

    This was unguarded for a while by accident: the agregarr mirror was the only `redact` caller in
    this module, and removing it took the import — and the comment explaining why it was needed —
    out with it.
    """
    from unittest.mock import MagicMock

    from shortlist.engine import pipeline as pipeline_mod
    from shortlist.engine.models import EngineConfig, HubAnchor
    from shortlist.engine.pipeline import _order_phase

    token = "SEcReT-live-plex-token-123"
    plex = MagicMock()
    plex.order_owned_hubs.side_effect = RuntimeError(
        f"(401) unauthorized; http://pms:32400/hubs/sections/2/manage?X-Plex-Token={token} <html>no</html>"
    )
    cfg = EngineConfig(hub_anchors={"2": HubAnchor("Recently Added", False)}, rows=[])

    lines: list[str] = []
    sink = pipeline_mod.logger.add(lines.append, level="WARNING", format="{message}")
    try:
        _order_phase(_order_ctx(cfg, plex), _empty_report())
    finally:
        pipeline_mod.logger.remove(sink)

    logged = "\n".join(lines)
    assert "hub ordering failed" in logged  # the failure is still reported, not swallowed
    assert token not in logged
    assert "X-Plex-Token=REDACTED" in logged
    # The non-secret half must survive — a redaction that eats the diagnosis is its own bug.
    assert "401" in logged and "pms:32400" in logged


# ── Anchoring a row to ANOTHER SHORTLIST ROW (issue #81) ────────────────────────────────────────
#
# A per-person row is one Plex collection PER PERSON, so the anchor is a BLOCK of hubs, not a title —
# which is why it is addressed by the row's delivered ratingKeys and not by `anchor_title`.


def _two_row_shelf():
    """A shelf holding two people's copies of two rows, plus a foreign hub. Returns the pieces."""
    foreign = FakeHub("New Series", "f")
    picked_a = FakeHub("Picked for You (sarah)", "p1")
    picked_b = FakeHub("Picked for You (mike)", "p2")
    because_a = FakeHub("Because you watched X", "b1")
    because_b = FakeHub("Because you watched Y", "b2")
    section = FakeSection([foreign, because_a, because_b, picked_a, picked_b])
    client = _client(
        [
            FakeColl("Picked for You (sarah)", ["shortlist_sarah"], rating_key=11),
            FakeColl("Picked for You (mike)", ["shortlist_mike"], rating_key=12),
            FakeColl("Because you watched X", ["shortlist_sarah"], rating_key=21),
            FakeColl("Because you watched Y", ["shortlist_mike"], rating_key=22),
            FakeColl("New Series", ["kometa"], rating_key=99),
        ]
    )
    return section, client, foreign


def test_a_row_can_be_placed_after_another_shortlist_row():
    """The whole point of issue #81. Both of the anchor row's collections stay put and the moving
    row's whole block lands after the LAST of them — not interleaved, because each person sees only
    their own and a block keeps every person's pair adjacent."""
    section, client, _foreign = _two_row_shelf()

    result = client.order_owned_hubs(
        section,
        label_prefix="shortlist",
        anchor_keys={11, 12},  # the "Picked for You" row
        anchor_label="the 'Picked for You' row",
        only_keys={21, 22},  # the "Because you watched" row
    )

    assert result["skipped"] is False
    assert section.titles() == [
        "New Series",
        "Picked for You (sarah)",
        "Picked for You (mike)",
        "Because you watched X",
        "Because you watched Y",
    ]
    assert result["anchor"] == "the 'Picked for You' row", "the audit must name the anchor (rule 10)"


def test_a_row_can_be_placed_before_another_shortlist_row():
    """`before` aims at the FIRST hub of the anchor block, and skips only the hubs being moved — the
    anchor row's own hubs are legitimate landmarks, unlike the foreign-anchor case where everything
    of ours is."""
    section, client, _foreign = _two_row_shelf()

    client.order_owned_hubs(
        section,
        label_prefix="shortlist",
        anchor_keys={11, 12},
        only_keys={21, 22},
        before=True,
    )

    assert section.titles() == [
        "New Series",
        "Because you watched X",
        "Because you watched Y",
        "Picked for You (sarah)",
        "Picked for You (mike)",
    ]


def test_the_anchor_row_itself_is_never_moved():
    """It is one of ours, so the usual protection (`the anchor is read-only`) does not come for free
    here — it comes from `only_keys` excluding it. A regression would shuffle the anchor too, and the
    shelf would drift a little further every night."""
    section, client, _ = _two_row_shelf()
    picked = [h for h in section.managedHubs() if h.title.startswith("Picked")]

    client.order_owned_hubs(section, label_prefix="shortlist", anchor_keys={11, 12}, only_keys={21, 22})

    assert [h.moves for h in picked] == [0, 0]


def test_a_row_that_names_itself_moves_nothing():
    """Meaningless, and left to Plex it would thrash the shelf. The caller rejects it; this is the
    second guard, so a slip cannot reach a real server."""
    section, client, _ = _two_row_shelf()
    before = section.titles()

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_keys={21, 22}, only_keys={21, 22})

    assert result["skipped"] is True
    assert result["reason"] == "anchor row not on this shelf"
    assert section.titles() == before


def test_an_anchor_row_with_nothing_on_this_shelf_leaves_the_order_alone():
    """Never fall back to a different slot. Silently reinterpreting where someone asked their row to
    go is worse than not moving it — the next run places it once that row delivers here."""
    section, client, _ = _two_row_shelf()
    before = section.titles()

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_keys={777}, only_keys={21, 22})

    assert result["skipped"] is True
    assert section.titles() == before


def test_before_a_row_lands_immediately_before_it_even_with_another_of_our_rows_in_the_way():
    """The cell that tells the two skip-sets apart, and the reason `before` cannot reuse the
    foreign-anchor rule.

    A foreign anchor skips back past EVERY row of ours, which is right when all of ours are moving
    together. A row anchor is the case where they are not: rows already placed are landmarks, and
    skipping past them drops this row on the far side of one — visibly the wrong slot, and stable, so
    nothing ever corrects it.
    """
    foreign = FakeHub("New Series", "f")
    popular_a = FakeHub("Popular on SFLIX", "pop1")
    picked_a = FakeHub("Picked for You (sarah)", "p1")
    because_a = FakeHub("Because you watched X", "b1")
    section = FakeSection([foreign, popular_a, picked_a, because_a])
    client = _client(
        [
            FakeColl("Popular on SFLIX", ["shortlist__shared_popular"], rating_key=31),
            FakeColl("Picked for You (sarah)", ["shortlist_sarah"], rating_key=11),
            FakeColl("Because you watched X", ["shortlist_sarah"], rating_key=21),
            FakeColl("New Series", ["kometa"], rating_key=99),
        ]
    )

    client.order_owned_hubs(
        section,
        label_prefix="shortlist",
        anchor_keys={11},  # before the "Picked for You" row
        only_keys={21},  # moving the "Because you watched" row
        before=True,
    )

    assert section.titles() == [
        "New Series",
        "Popular on SFLIX",
        "Because you watched X",
        "Picked for You (sarah)",
    ], "it must land between the row already placed and the anchor, not jump the whole block"


# ── _order_phase: resolving row anchors into a placement ORDER ──────────────────────────────────


def _anchor_cfg(rows):
    from shortlist.engine.models import EngineConfig

    return EngineConfig(hub_anchors={}, rows=rows)


def test_order_phase_places_the_anchor_row_before_the_row_that_follows_it():
    """A row anchored to a sibling can only be placed once that sibling is where it belongs, so the
    calls must come out in dependency order — not in row order, and not in dict order."""
    from unittest.mock import MagicMock

    from shortlist.engine.models import HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    cfg = _anchor_cfg(
        [
            # Declared FOLLOWER-FIRST on purpose: input order must not decide the outcome.
            RowSpec(slug="gems", name_template="Gems", size=10, hub_anchors={"2": HubAnchor(anchor_row="picked")}),
            RowSpec(slug="picked", name_template="Picked", size=10, hub_anchors={"2": HubAnchor("New Series", False)}),
        ]
    )

    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    calls = plex.order_owned_hubs.call_args_list
    assert [c.kwargs["only_keys"] for c in calls] == [{11, 12}, {21, 22}], (
        "'picked' must be placed first — 'gems' anchors to where it ends up"
    )
    assert calls[0].kwargs["anchor_title"] == "New Series"
    assert calls[1].kwargs["anchor_keys"] == {11, 12}
    assert calls[1].kwargs["anchor_label"] == "the 'Picked' row", "the audit names the row, not its slug"


def test_the_audit_names_the_default_row_by_its_title_not_its_slug():
    """The default row carries no template of its own — its title is the global one — so it is the one
    row whose name would fall through to a bare internal slug. It is also the likeliest anchor of all
    ("put this after Picked for You"), so that is the audit line most people would ever read."""
    from unittest.mock import MagicMock

    from shortlist.engine.models import HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    cfg = _anchor_cfg(
        [
            RowSpec(slug="picked", name_template="", size=10, hub_anchors={"2": HubAnchor("New Series", False)}),
            RowSpec(slug="gems", name_template="Gems", size=10, hub_anchors={"2": HubAnchor(anchor_row="picked")}),
        ]
    )
    cfg.row_name_template = "✨ {library_name} Picked for You"

    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    follower = next(c for c in plex.order_owned_hubs.call_args_list if c.kwargs["only_keys"] == {21, 22})
    assert follower.kwargs["anchor_label"] == "the '✨ {library_name} Picked for You' row"


def test_order_phase_moves_nothing_when_two_rows_anchor_to_each_other():
    """A cycle has no right answer. Placing half of it produces a shelf order that flips run to run
    depending on which half won — and with another tool reordering the same shelf, that is
    indistinguishable from losing the race. Leaving them put is stable and gets logged."""
    from unittest.mock import MagicMock

    from shortlist.engine.models import HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    cfg = _anchor_cfg(
        [
            RowSpec(slug="picked", name_template="Picked", size=10, hub_anchors={"2": HubAnchor(anchor_row="gems")}),
            RowSpec(slug="gems", name_template="Gems", size=10, hub_anchors={"2": HubAnchor(anchor_row="picked")}),
        ]
    )

    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    plex.order_owned_hubs.assert_not_called()


def test_order_phase_moves_nothing_when_a_row_anchors_to_itself():
    """A one-node cycle. Reaching Plex it would ask a row to move relative to its own hubs."""
    from unittest.mock import MagicMock

    from shortlist.engine.models import HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    cfg = _anchor_cfg(
        [RowSpec(slug="picked", name_template="Picked", size=10, hub_anchors={"2": HubAnchor(anchor_row="picked")})]
    )

    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    plex.order_owned_hubs.assert_not_called()


def test_order_phase_skips_a_row_whose_anchor_row_has_nothing_in_this_library():
    """Not a fallback to the library default: reinterpreting the placement is worse than skipping it.
    The row that IS placeable still is — one unresolvable anchor must not stop the rest."""
    from unittest.mock import MagicMock

    from shortlist.engine.models import HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    cfg = _anchor_cfg(
        [
            RowSpec(slug="picked", name_template="Picked", size=10, hub_anchors={"2": HubAnchor("New Series", False)}),
            RowSpec(slug="gems", name_template="Gems", size=10, hub_anchors={"2": HubAnchor(anchor_row="ghost")}),
        ]
    )

    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    calls = plex.order_owned_hubs.call_args_list
    assert [c.kwargs["only_keys"] for c in calls] == [{11, 12}], "only the resolvable row is placed"


def test_a_dormant_copy_of_the_anchor_row_does_not_drag_the_follower_to_the_bottom():
    """Paused and disabled people keep a copy of every row, and we never move those — so they sit
    wherever Plex appended them, at the bottom, under the co-managing tool's hubs.

    Anchoring to the block INCLUDING them followed the row down there and reported it verified: the
    exact burial this function exists to undo, wearing a success badge.
    """
    foreign = FakeHub("New Series", "f")
    picked_live = FakeHub("Picked for You (sarah)", "p1")
    kometa = FakeHub("Kometa Genre", "k")
    picked_dormant = FakeHub("Picked for You (paused)", "p2", promoted=False)
    because = FakeHub("Because you watched X", "b1")
    section = FakeSection([foreign, picked_live, kometa, picked_dormant, because])
    client = _client(
        [
            FakeColl("Picked for You (sarah)", ["shortlist_sarah"], rating_key=11),
            FakeColl("Picked for You (paused)", ["shortlist_paused"], rating_key=12),
            FakeColl("Because you watched X", ["shortlist_sarah"], rating_key=21),
            FakeColl("New Series", ["kometa"], rating_key=99),
            FakeColl("Kometa Genre", ["kometa"], rating_key=98),
        ]
    )

    client.order_owned_hubs(section, label_prefix="shortlist", anchor_keys={11, 12}, only_keys={21})

    assert section.titles() == [
        "New Series",
        "Picked for You (sarah)",
        "Because you watched X",
        "Kometa Genre",
        "Picked for You (paused)",
    ], "it must follow the PROMOTED copy, not the dormant one parked at the bottom"


def test_an_all_dormant_anchor_row_leaves_the_shelf_alone():
    """No visible position to be relative to. Inventing one puts the row somewhere nobody asked for."""
    foreign = FakeHub("New Series", "f")
    dormant = FakeHub("Picked for You (paused)", "p1", promoted=False)
    because = FakeHub("Because you watched X", "b1")
    section = FakeSection([foreign, dormant, because])
    client = _client(
        [
            FakeColl("Picked for You (paused)", ["shortlist_paused"], rating_key=11),
            FakeColl("Because you watched X", ["shortlist_sarah"], rating_key=21),
        ]
    )
    before = section.titles()

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_keys={11}, only_keys={21})

    assert result["skipped"] is True and result["reason"] == "anchor row not on this shelf"
    assert section.titles() == before


def test_order_phase_anchors_to_the_whole_group_the_anchor_row_was_placed_with():
    """Convergence. Rows sharing a slot are moved in ONE call and land contiguously, so a follower
    aimed at just the anchor row's own hubs is inserted INSIDE that block — and the next run's group
    pass, restoring contiguity, evicts it again. Neither call ever settles, both report success every
    night, and on a 40-account server that is ~40 needless writes per library forever.
    """
    from unittest.mock import MagicMock

    from shortlist.engine.models import HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    ledger = {
        ("a", "picked", "2"): 11,
        ("b", "picked", "2"): 12,
        ("a", "popular", "2"): 31,
        ("a", "gems", "2"): 21,
        ("b", "gems", "2"): 22,
    }
    cfg = _anchor_cfg(
        [
            # 'picked' and 'popular' share one slot, so they are placed together as one block.
            RowSpec(slug="picked", name_template="Picked", size=10, hub_anchors={"2": HubAnchor("New Series", False)}),
            RowSpec(
                slug="popular", name_template="Popular", size=10, hub_anchors={"2": HubAnchor("New Series", False)}
            ),
            RowSpec(slug="gems", name_template="Gems", size=10, hub_anchors={"2": HubAnchor(anchor_row="picked")}),
        ]
    )

    _order_phase(_order_ctx(cfg, plex, delivered_keys=ledger), _report_with_titles())

    follower = next(c for c in plex.order_owned_hubs.call_args_list if c.kwargs["only_keys"] == {21, 22})
    assert follower.kwargs["anchor_keys"] == {11, 12, 31}, (
        "it must follow the block that was actually placed, not just the anchor row's own hubs"
    )


def test_a_row_anchor_in_the_GLOBAL_default_is_ignored_not_applied():
    """`rows.hub_anchor` applies to EVERY row, so "all rows go after row X" includes X itself — and
    the paths that use the global default pass no `anchor_keys`, so it would reach the client's
    foreign branch with an empty title and match any hub whose title is empty. The settings API
    rejects it on the way in; this is the second guard, so relaxing that one cannot open this door.
    """
    from shortlist.server.services.context_builder import ContextBuilder

    parsed = ContextBuilder._parse_hub_anchors({"2": {"row": "picked"}})
    assert parsed == {"2": HubAnchorModel(anchor_row="picked")}, "the per-ROW parse still reads it"

    class _Store:
        def get(self, _key):
            return {"2": {"row": "picked"}, "3": {"anchor": "New Series"}}

    globals_ = ContextBuilder._build_hub_anchors(_Store())

    assert "2" not in globals_, "a row anchor cannot be a global default"
    assert globals_["3"].anchor_title == "New Series", "the foreign anchor beside it still applies"
