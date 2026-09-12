"""Unit tests for PlexClient.place_rows — the Recommended-shelf placement of Shortlist rows.

These pin the DECISION logic: only our hubs move, the anchor is read-only (Kometa coexistence), it's
idempotent, dry-run is inert — and, since 2026-08-12, that it moves only hubs actually OUT OF PLACE
and re-reads the shelf to verify rather than trusting Plex's 200.

``FakeSection`` APPLIES each move to its own list, so the shelf a test reads back is the shelf the
moves produced (testing rule: the fake must be no easier than the real server — one that ignored
moves would make every assertion here about a shape Plex never returns). ``DroppingSection`` models
a shelf whose ordering has COLLAPSED: a move that returns 200 and leaves the order unchanged. That is
not another tool racing us — Plex keeps hub positions in a float, an anchored move halves the gap
between two of them, and after ~50 inserts the gap is smaller than a float can hold and every
subsequent move is accepted and dropped (measured on a real server, 2026-09-11).
"""

from itertools import pairwise

from shortlist.engine.clients.plex_pms import PlexClient

_UNSET = "UNSET"  # sentinel: move() was never called on this hub


class FakeHub:
    """A managed hub. Carries the three promotion flags a real ``managedHubs()`` entry has.

    They default to promoted-on-shared-Home because that is what a row on the shelf looks like, and
    because a fake WITHOUT these attributes would have hidden the fact that `managedHubs()` also
    lists hubs promoted nowhere — which is exactly what placement used to waste moves on.
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
    """A managed shelf that really reorders when a hub is moved, and counts the DANGEROUS moves.

    Plex positions hubs by a float and inserts by halving a gap, so only two inserts have room to
    spare: to the very top (``after=None`` -> ``min - 1000``) and after the hub that is currently
    LAST (``max + 1000``). Anything else subdivides, and a gap dies after ~50 subdivisions — after
    which Plex answers 200 and applies nothing until the values are re-spaced. `halving_moves` counts
    those, so a test can assert the safety property directly instead of inspecting call arguments.
    """

    def __init__(self, hubs: list[FakeHub], title: str = "TV Shows", key: int = 2):
        self._hubs = list(hubs)
        self.title = title
        self.key = key
        self.halving_moves = 0
        for hub in self._hubs:
            hub.shelf = self

    def managedHubs(self):
        return list(self._hubs)

    def apply(self, hub: FakeHub, after) -> None:
        if after is not None and self._hubs and self._hubs[-1] is not after:
            self.halving_moves += 1
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
    switches a built-in off in Manage Recommendations gets all three at 0, and that is exactly why
    `can_anchor` HAS to judge one: an off built-in is on no shelf, and a row anchored to it cannot be
    placed relative to anything.
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
    # ...so it is NOT an anchor. This reverses an earlier review decision ("refusing 'Recently
    # Released' as an anchor would be the worse bug"), which was made without noticing that accepting
    # it places nothing: `place_rows` builds its backbone from promoted hubs only, so a block spliced
    # onto an unpromoted anchor drops out of the arrangement, and the pass then compares the backbone
    # with itself and answers "already in place" every night in silence. A refusal is audited.
    assert not can_anchor(by_ident["movie.recentlyreleased"])
    assert not can_anchor(by_ident["movie.genre"])
    # Built-ins the owner has switched ON are anchors, which is the case that decision protected.
    # Named directly: `all(... if is_promoted(h))` cannot fail now that `can_anchor` IS `is_promoted`.
    assert can_anchor(by_ident["movie.recentlyadded"])
    assert can_anchor(by_ident["movie.topunwatched"])
    # Every collection on this server was on the Recommended shelf, so all of them can anchor. The
    # off-shelf case #106 is about is NOT in this capture — see tests/fixtures/README.md.
    assert all(can_anchor(h) for h in hubs if is_collection_hub(h))


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


class TestPlaceRows:
    """`place_rows` — the whole shelf arrangement, built with the only move Plex honours.

    Plex keeps each hub's position in a float (`hub_templates."order"`). `move?after=X` inserts by
    halving the gap between X and its neighbour, so a gap dies after ~50 inserts and Plex then
    accepts every move and applies none — measured on the maintainer's server, 83 of 131 Movies hubs
    collapsed onto the single value 1000. `move()` with no anchor takes a value below the minimum
    instead, so it never subdivides. These tests pin that only the safe move is ever issued.
    """

    @staticmethod
    def _client_and_shelf(hubs, colls):
        return _client(colls), FakeSection(hubs)

    def test_our_rows_land_after_the_anchor_and_foreign_hubs_keep_their_order(self):
        """The anchor is NOT hoisted, and nobody else's hubs are reordered relative to each other —
        the only thing that changes is where our rows sit among them."""
        anchor = FakeHub("Recently Added", "ra", collection=False)
        r1, r2 = FakeHub("Picked Sarah", "p1"), FakeHub("Picked Mike", "p2")
        kometa, genre = FakeHub("Kometa Genre", "k"), FakeHub("Genres", "g")
        client, section = self._client_and_shelf(
            [kometa, r2, anchor, r1, genre],
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Picked Mike", ["shortlist_mike"], 12),
                FakeColl("Kometa Genre", ["kometa"], 90),
                FakeColl("Genres", ["kometa"], 91),
            ],
        )

        result = client.place_rows(
            section, label_prefix="shortlist", sequence=[("anchor", "Recently Added"), ("rows", {11, 12})]
        )

        assert result["verified"] is True
        assert section.titles() == [
            "Kometa Genre",
            "Recently Added",
            "Picked Mike",
            "Picked Sarah",
            "Genres",
        ]
        # Not one halving insert. This is the property that keeps a library orderable.
        assert section.halving_moves == 0

    def test_a_sequence_anchored_to_a_hub_is_left_alone_wherever_it_has_drifted(self):
        """Another tool adding a hub above ours must not cost a single write.

        The rows are still directly after the anchor the owner named, which is what was asked for.
        Demanding an absolute position instead means rewriting every row whenever anything else
        appears above them — 93 writes a night on the maintainer's server, for nothing.
        """
        other1, other2 = FakeHub("Trending", "t1"), FakeHub("New to You", "t2")
        anchor = FakeHub("Recently Added", "ra", collection=False)
        r1, r2 = FakeHub("Picked Sarah", "p1"), FakeHub("Picked Mike", "p2")
        client, section = self._client_and_shelf(
            [other1, other2, anchor, r1, r2],
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Picked Mike", ["shortlist_mike"], 12),
            ],
        )

        result = client.place_rows(
            section, label_prefix="shortlist", sequence=[("anchor", "Recently Added"), ("rows", {11, 12})]
        )

        assert result["skipped"] is True and result["reason"] == "already in place"
        assert [h.moves for h in (anchor, r1, r2, other1, other2)] == [0, 0, 0, 0, 0]

    def test_a_sequence_that_starts_with_rows_does_want_the_very_top(self):
        """ "Top of the shelf" is an absolute request, so a hub above ours IS a reason to move."""
        kometa = FakeHub("Kometa Genre", "k")
        r1 = FakeHub("Picked Sarah", "p1")
        client, section = self._client_and_shelf(
            [kometa, r1], [FakeColl("Picked Sarah", ["shortlist_sarah"], 11), FakeColl("Kometa Genre", ["kometa"], 90)]
        )

        result = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11})])

        assert result["verified"] is True
        assert section.titles() == ["Picked Sarah", "Kometa Genre"]

    def test_a_rebuilt_collection_at_the_bottom_is_pulled_back_into_its_row(self):
        """The case that happens every night: a row losing 5+ titles is deleted and recreated, and
        Plex appends the new hub to the BOTTOM. 20-70 of these a night on the maintainer's server."""
        anchor = FakeHub("Recently Added", "ra", collection=False)
        r1 = FakeHub("Picked Sarah", "p1")
        kometa = FakeHub("Kometa Genre", "k")
        rebuilt = FakeHub("Picked Mike", "p2")  # brand-new hub, appended last
        client, section = self._client_and_shelf(
            [anchor, r1, kometa, rebuilt],
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Picked Mike", ["shortlist_mike"], 12),
                FakeColl("Kometa Genre", ["kometa"], 90),
            ],
        )

        result = client.place_rows(
            section, label_prefix="shortlist", sequence=[("anchor", "Recently Added"), ("rows", {11, 12})]
        )

        assert result["verified"] is True
        assert section.titles()[:3] == ["Recently Added", "Picked Sarah", "Picked Mike"]

    def test_rows_keep_their_configured_order_but_not_their_internal_order(self):
        a1, b1, a2, b2 = (
            FakeHub("Picked Sarah", "a1"),
            FakeHub("Because Sarah", "b1"),
            FakeHub("Picked Mike", "a2"),
            FakeHub("Because Mike", "b2"),
        )
        client, section = self._client_and_shelf(
            [a1, b1, a2, b2],
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Picked Mike", ["shortlist_mike"], 12),
                FakeColl("Because Sarah", ["shortlist_sarah"], 21),
                FakeColl("Because Mike", ["shortlist_mike"], 22),
            ],
        )

        result = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11, 12}), ("rows", {21, 22})])

        assert result["verified"] is True
        titles = section.titles()
        assert set(titles[:2]) == {"Picked Sarah", "Picked Mike"}
        assert set(titles[2:4]) == {"Because Sarah", "Because Mike"}

    def test_a_missing_anchor_leaves_the_shelf_untouched(self):
        r1 = FakeHub("Picked Sarah", "p1")
        client, section = self._client_and_shelf([r1], [FakeColl("Picked Sarah", ["shortlist_sarah"], 11)])

        result = client.place_rows(section, label_prefix="shortlist", sequence=[("anchor", "Gone"), ("rows", {11})])

        assert result["skipped"] is True and result["reason"] == "anchor not found"
        assert r1.moves == 0

    def test_a_dry_run_writes_nothing(self):
        kometa = FakeHub("Kometa Genre", "k")
        r1 = FakeHub("Picked Sarah", "p1")
        client, section = self._client_and_shelf(
            [kometa, r1], [FakeColl("Picked Sarah", ["shortlist_sarah"], 11), FakeColl("Kometa Genre", ["kometa"], 90)]
        )

        result = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11})], dry_run=True)

        assert result["dry_run"] is True
        assert r1.moves == 0 and kometa.moves == 0

    def test_a_shelf_whose_ordering_has_collapsed_is_reported_not_retried_for_ever(self):
        """Once a library's floats have collapsed onto one value, Plex accepts every move and applies
        none. The run must record that and carry on — the order is cosmetic — never claim success."""
        kometa, genre = FakeHub("Kometa Genre", "k"), FakeHub("Genres", "g")
        r1 = FakeHub("Picked Sarah", "p1")
        client = _client(
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Kometa Genre", ["kometa"], 90),
                FakeColl("Genres", ["kometa"], 91),
            ]
        )
        section = DroppingSection([kometa, r1, genre])

        result = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11})])

        assert result["verified"] is False and result["skipped"] is False
        assert result["moved"] == ["Picked Sarah"]
        # The audit says how many hubs it repositioned in total, ours and the backbone, because a
        # bottom-build writes to both (plex-safety rule 10).
        assert result["repositioned"] > 0

    def test_an_anchor_collection_that_is_on_no_shelf_is_refused(self):
        """Issue #106. `managedHubs()` lists every manageable hub, and a COLLECTION promoted nowhere
        has no position a viewer can see — following one buried the rows below every standard Plex hub
        while the run reported success. Plex's own built-ins are never refused: an owner may
        legitimately switch one off, and their flags are read by nothing here."""
        off_shelf = FakeHub("Letzte Chance", "lc", promoted=False)
        r1 = FakeHub("Picked Sarah", "p1")
        client, section = self._client_and_shelf(
            [off_shelf, r1],
            [FakeColl("Picked Sarah", ["shortlist_sarah"], 11), FakeColl("Letzte Chance", ["kometa"], 90)],
        )

        result = client.place_rows(
            section, label_prefix="shortlist", sequence=[("anchor", "Letzte Chance"), ("rows", {11})]
        )

        assert result["skipped"] is True and result["reason"] == "anchor not found"
        assert (r1.moves, off_shelf.moves) == (0, 0)

    def test_a_dormant_row_is_not_positioned(self):
        """A paused or disabled person's row is still in `managedHubs()` but promoted nowhere, so it
        sits on no shelf. Moving it spends a write on a position nobody can see."""
        anchor = FakeHub("Recently Added", "ra", collection=False)
        live = FakeHub("Picked Sarah", "p1")
        dormant = FakeHub("Picked Paused", "p2", promoted=False)
        client, section = self._client_and_shelf(
            [anchor, live, dormant],
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Picked Paused", ["shortlist_paused"], 12),
            ],
        )

        result = client.place_rows(
            section, label_prefix="shortlist", sequence=[("anchor", "Recently Added"), ("rows", {11, 12})]
        )

        assert result["skipped"] is True and result["reason"] == "already in place"
        assert dormant.moves == 0

    def test_a_row_whose_label_read_comes_back_empty_is_still_placed(self):
        """plex-safety rule 4. A real PMS sends no <Label> children in the collections listing; they
        arrive only because plexapi silently re-reads each collection, and a read that SUCCEEDS
        carrying no label is indistinguishable from a genuinely unlabelled row. Ordering only ever
        changes a POSITION, so the invisible title marker alone is sufficient proof of ownership."""
        from shortlist.engine.delivery import row_marker

        titled = "Picked Sarah" + row_marker(555000100)
        kometa = FakeHub("Kometa Genre", "k")
        ours = FakeHub(titled, "p1")
        client, section = self._client_and_shelf(
            [kometa, ours],
            [FakeColl(titled, [], 11), FakeColl("Kometa Genre", ["kometa"], 90)],  # NO labels at all
        )

        result = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11})])

        assert result["verified"] is True
        assert section.titles()[0] == titled
        assert section.halving_moves == 0

    def test_it_retries_a_move_plex_dropped_and_reports_verified(self):
        """Plex genuinely drops the occasional write. One dropped move must not be reported as a lost
        shelf — the retry places it and the pass is verified."""

        class DropsTheFirstMove(FakeSection):
            def __init__(self, hubs):
                super().__init__(hubs)
                self.dropped = False

            def apply(self, hub, after):
                if not self.dropped:
                    self.dropped = True
                    return
                super().apply(hub, after)

        kometa, genre = FakeHub("Kometa Genre", "k"), FakeHub("Genres", "g")
        r1 = FakeHub("Picked Sarah", "p1")
        client = _client(
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Kometa Genre", ["kometa"], 90),
                FakeColl("Genres", ["kometa"], 91),
            ]
        )
        section = DropsTheFirstMove([kometa, r1, genre])

        result = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11})])

        assert result["verified"] is True
        assert section.titles()[0] == "Picked Sarah"

    def test_no_move_ever_subdivides_a_gap(self):
        """The property the whole design rests on, asserted directly.

        Plex inserts by halving the gap between the anchor and its neighbour, and a gap dies after
        ~50 inserts — after which every move on that library is accepted and dropped, for every
        client including Plex's own web app. Only two inserts have room to spare: the very top, and
        after whichever hub is currently LAST. `FakeSection` counts anything else.
        """
        hubs = [FakeHub(f"Hub {n}", f"h{n}") for n in range(6)]
        client, section = self._client_and_shelf(
            hubs, [FakeColl(f"Hub {n}", ["shortlist_sarah" if n % 2 else "kometa"], 10 + n) for n in range(6)]
        )

        client.place_rows(section, label_prefix="shortlist", sequence=[("anchor", "Hub 0"), ("rows", {11, 13, 15})])

        assert section.halving_moves == 0

    def test_the_block_follows_the_anchor_that_was_validated_not_a_namesake(self):
        """Two hubs can share a title — one on a shelf, one promoted nowhere. The block must follow
        the one checked as usable, so keying the splice by TITLE is wrong: it attaches to whichever
        comes first in shelf order."""
        ghost = FakeHub("Recently Added", "ghost", promoted=False)
        real = FakeHub("Recently Added", "ra", collection=False)
        r1 = FakeHub("Picked Sarah", "p1")
        client, section = self._client_and_shelf(
            [ghost, real, r1],
            [FakeColl("Picked Sarah", ["shortlist_sarah"], 11), FakeColl("Recently Added", ["kometa"], 90)],
        )

        result = client.place_rows(
            section, label_prefix="shortlist", sequence=[("anchor", "Recently Added"), ("rows", {11})]
        )

        assert result.get("verified") is True or result.get("skipped") is True
        titles = section.titles()
        # Our row sits straight after the hub that is actually on a shelf, and the dormant namesake
        # is left out of the arrangement entirely.
        assert titles.index("Picked Sarah") == titles.index("Recently Added", 1) + 1
        assert ghost.moves == 0

    def test_a_hub_promoted_nowhere_is_left_out_of_the_arrangement(self):
        """It sits on no shelf, so moving it is a write to a hub we do not own for a position nobody
        can see."""
        anchor = FakeHub("Recently Added", "ra", collection=False)
        r1 = FakeHub("Picked Sarah", "p1")
        dormant_foreign = FakeHub("Retired Kometa Row", "dk", promoted=False)
        client, section = self._client_and_shelf(
            [anchor, dormant_foreign, r1],
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Retired Kometa Row", ["kometa"], 90),
            ],
        )

        client.place_rows(section, label_prefix="shortlist", sequence=[("anchor", "Recently Added"), ("rows", {11})])

        assert dormant_foreign.moves == 0

    def test_a_dry_run_reports_the_whole_cost_not_just_our_rows(self):
        """A bottom-build writes to the backbone too. A preview that counted only our rows would show
        an owner a smaller number than a real pass performs."""
        kometa, genre = FakeHub("Kometa Genre", "k"), FakeHub("Genres", "g")
        r1 = FakeHub("Picked Sarah", "p1")
        client, section = self._client_and_shelf(
            [kometa, genre, r1],
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Kometa Genre", ["kometa"], 90),
                FakeColl("Genres", ["kometa"], 91),
            ],
        )

        result = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11})], dry_run=True)

        assert result["dry_run"] is True
        assert result["moved"] == ["Picked Sarah"]
        assert result["repositioned"] > len(result["moved"])
        assert (kometa.moves, genre.moves, r1.moves) == (0, 0, 0)


class FloatShelf(FakeSection):
    """A shelf that models Plex's actual storage: one FLOAT per hub in `hub_templates."order"`.

    Plex positions a hub by a number, not an index. `move` with no `after` writes ``min - 1000``;
    ``after=<the hub currently last>`` writes ``max + 1000``; anything else writes the MIDPOINT of
    its neighbours' values. That midpoint is what kills a shelf — about 50 of them exhaust double
    precision, and from then on Plex answers 200 and applies nothing, for every client including its
    own web app. This is the shape `FakeSection` only counts, spelled out, so a test can assert what
    a pass does to the numbers rather than to a list index.
    """

    def __init__(self, hubs, title: str = "TV Shows", key: int = 2, orders: list[float] | None = None):
        super().__init__(hubs, title=title, key=key)
        self.order = (
            dict(zip([h.identifier for h in hubs], orders, strict=True))
            if orders
            else {h.identifier: 1000.0 * (n + 1) for n, h in enumerate(hubs)}
        )

    def apply(self, hub, after) -> None:
        super().apply(hub, after)
        ident = hub.identifier
        others = [v for k, v in self.order.items() if k != ident]
        if after is None:
            self.order[ident] = min(others) - 1000.0
            return
        position = self._hubs.index(hub)
        below = self.order[after.identifier]
        above = [self.order[h.identifier] for h in self._hubs[position + 1 :] if h.identifier != ident]
        self.order[ident] = below + 1000.0 if not above else (below + min(above)) / 2.0

    def gaps(self) -> list[float]:
        values = sorted(self.order.values())
        return [b - a for a, b in pairwise(values)]


class TestFloatHealth:
    """What a pass does to the numbers Plex stores, not just to the order it reports."""

    def test_a_pass_repairs_a_shelf_whose_float_space_is_already_exhausted(self):
        """The guarantee that makes this design safe to run forever, rather than merely safe once.

        A shelf damaged by the old midpoint-inserting code arrives with neighbours a few billionths
        apart — Plex will silently drop any insert between them. A bottom-build does not need those
        gaps (it only ever writes ``max + 1000``), so it not only survives the damage, it leaves the
        shelf re-spaced 1000 apart: the shelf heals the first time it needs a move. Measured on a
        real server, a damaged library came back with every gap at exactly 1000.0.
        """
        anchor = FakeHub("Recently Added", "ra", collection=False)
        r1, r2 = FakeHub("Picked Sarah", "p1"), FakeHub("Picked Mike", "p2")
        kometa = FakeHub("Kometa Genre", "k")
        hubs = [anchor, kometa, r2, r1]
        # Three of the four are effectively stacked on one value — what ~50 halvings leaves behind.
        section = FloatShelf(hubs, orders=[1000.0, 2000.0, 2000.000000006, 2000.000000009])
        client = _client(
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Picked Mike", ["shortlist_mike"], 12),
                FakeColl("Kometa Genre", ["kometa"], 13),
            ]
        )
        assert min(section.gaps()) < 1e-8, "precondition: the shelf starts damaged"

        result = client.place_rows(
            section, label_prefix="shortlist", sequence=[("anchor", "Recently Added"), ("rows", {11, 12})]
        )

        assert section.halving_moves == 0
        assert result["verified"] is True
        assert min(section.gaps()) >= 1000.0, f"left damaged: {section.gaps()}"
        assert len(set(section.order.values())) == len(hubs), "two hubs share a position"

    def test_an_unchanged_shelf_is_left_alone_even_when_its_floats_are_damaged(self):
        """Zero writes stays zero writes. A shelf already in the wanted order is not rewritten just
        because its numbers are crowded — the owner asked for moves only when moves are needed, and
        the crowding costs nothing until something actually has to be inserted. It heals on the first
        pass that has real work, which the test above pins.
        """
        anchor = FakeHub("Recently Added", "ra", collection=False)
        r1 = FakeHub("Picked Sarah", "p1")
        section = FloatShelf([anchor, r1], orders=[1000.0, 1000.000000004])
        client = _client([FakeColl("Picked Sarah", ["shortlist_sarah"], 11)])
        damaged = section.gaps()

        result = client.place_rows(
            section, label_prefix="shortlist", sequence=[("anchor", "Recently Added"), ("rows", {11})]
        )

        assert result["skipped"] is True
        assert result["repositioned"] == 0
        assert section.gaps() == damaged


class TestAnchorDirection:
    """Where a block actually LANDS on the shelf — not what shape the sequence had."""

    @staticmethod
    def _client_and_shelf(hubs, colls):
        return _client(colls), FakeSection(hubs)

    def test_before_a_collection_lands_immediately_above_it_not_at_the_top(self):
        """`before` was encoded as [block, anchor], and a block with no anchor ahead of it goes to
        the very TOP — the two cases are indistinguishable in that encoding. So "right before New
        Series" jumped the row above every hub between the top and New Series, while the audit
        recorded New Series as its anchor. Asserted on the resulting shelf, which is the only place
        the bug was visible: the sequence-shape test passed throughout.
        """
        top, two = FakeHub("Kometa Top", "k1"), FakeHub("Kometa Two", "k2")
        target = FakeHub("New Series", "ns")
        row = FakeHub("Picked Sarah", "p1")
        client, section = self._client_and_shelf(
            [top, two, target, row],
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("New Series", ["kometa"], 12),
                FakeColl("Kometa Top", ["kometa"], 13),
                FakeColl("Kometa Two", ["kometa"], 14),
            ],
        )

        client.place_rows(section, label_prefix="shortlist", sequence=[("anchor_before", "New Series"), ("rows", {11})])

        assert section.titles() == ["Kometa Top", "Kometa Two", "Picked Sarah", "New Series"]
        assert section.halving_moves == 0

    def test_one_collection_can_carry_a_row_above_it_and_another_below_it(self):
        """Both directions against ONE anchor, on the shelf. The splice keeps two buckets per anchor
        because an owner can legitimately ask for both."""
        target = FakeHub("New Series", "ns")
        above, below = FakeHub("Picked Sarah", "p1"), FakeHub("Picked Mike", "p2")
        client, section = self._client_and_shelf(
            [target, below, above],
            [
                FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                FakeColl("Picked Mike", ["shortlist_mike"], 12),
                FakeColl("New Series", ["kometa"], 13),
            ],
        )

        client.place_rows(
            section,
            label_prefix="shortlist",
            sequence=[
                ("anchor_before", "New Series"),
                ("rows", {11}),
                ("anchor", "New Series"),
                ("rows", {12}),
            ],
        )

        assert section.titles() == ["Picked Sarah", "New Series", "Picked Mike"]

    def test_an_anchor_the_owner_switched_off_is_refused_rather_than_silently_dropped(self):
        """A built-in the owner turns off in Manage Recommendations reads unpromoted, and the backbone
        is built from promoted hubs only — so the block spliced onto it used to vanish from the
        arrangement, the pass compared the backbone with itself, agreed, and answered "already in
        place" every night with no warning and no audit record. The row sat at the bottom for
        ever. Refusing names the reason, which the audit reports.
        """
        off = FakeHub("Recently Added Movies", "movie.recentlyadded", collection=False, promoted=False)
        keep = FakeHub("Kometa Genre", "k1")
        row = FakeHub("Picked Sarah", "p1")
        client, section = self._client_and_shelf(
            [off, keep, row],
            [FakeColl("Picked Sarah", ["shortlist_sarah"], 11), FakeColl("Kometa Genre", ["kometa"], 12)],
        )

        result = client.place_rows(
            section, label_prefix="shortlist", sequence=[("anchor", "Recently Added Movies"), ("rows", {11})]
        )

        assert result["reason"] == "anchor not found"
        assert result["refused"] == ["Recently Added Movies"], "the owner must be told which anchor failed"
        assert section.titles() == ["Recently Added Movies", "Kometa Genre", "Picked Sarah"], "nothing moved"

    def test_a_foreign_hub_sharing_a_title_with_one_of_our_rows_is_not_claimed(self):
        """Ownership on the shelf was decided by TITLE alone. A hub that is not a collection at all —
        a stock Plex hub — whose title matches one of our rows was moved as part of our block and
        named in the audit as one of our rows (plex-safety rule 4)."""
        impostor = FakeHub("Picked Sarah", "movie.recentlyadded", collection=False)
        real = FakeHub("Picked Sarah", "custom.collection.2.11")
        client, section = self._client_and_shelf([impostor, real], [FakeColl("Picked Sarah", ["shortlist_sarah"], 11)])

        result = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11})])

        # Claimed by title, the impostor joined our block, the block already matched the shelf, and
        # the pass answered "already in place" — leaving our real row BELOW a hub it should top.
        # The teeth are here, not in `moved`: claimed by title the pass answers "already in place"
        # with `moved: []` too, so asserting that alone would pass against the bug it accuses.
        assert section._hubs == [real, impostor]
        assert result["repositioned"] == 1

    def test_one_collection_named_by_two_blocks_does_not_rewrite_the_shelf_for_ever(self):
        """A rating key reachable from two row slugs put an identifier in `wanted` twice. The in-place
        comparison sees each hub once, so it could never hold: the pass burned every attempt
        rebuilding the whole shelf, reported `verified: False`, and did it again the next night."""
        row = FakeHub("Picked Sarah", "p1")
        other = FakeHub("Kometa Genre", "k1")
        client, section = self._client_and_shelf(
            [other, row], [FakeColl("Picked Sarah", ["shortlist_sarah"], 11), FakeColl("Kometa Genre", ["kometa"], 12)]
        )

        first = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11}), ("rows", {11})])
        second = client.place_rows(section, label_prefix="shortlist", sequence=[("rows", {11}), ("rows", {11})])

        assert first["verified"] is True
        assert second["skipped"] is True, f"a second pass still found work: {second}"
        assert second["repositioned"] == 0

    def test_the_preview_states_exactly_what_a_real_pass_writes(self):
        """The dry-run count excluded the shelf's current last hub wherever it appeared in the wanted
        order, but a real pass skips it only when it is FIRST. The comment above that line promises an
        owner is never shown a smaller number than the truth, so assert EQUALITY against a real pass
        on the same shelf rather than the inequality that let a one-off through."""
        hubs = [FakeHub("Kometa One", "k1"), FakeHub("Picked Sarah", "p1"), FakeHub("Kometa Two", "k2")]
        colls = [
            FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
            FakeColl("Kometa One", ["kometa"], 12),
            FakeColl("Kometa Two", ["kometa"], 13),
        ]
        preview = _client(colls).place_rows(
            FakeSection([FakeHub(h.title, h.identifier) for h in hubs]),
            label_prefix="shortlist",
            sequence=[("rows", {11})],
            dry_run=True,
        )
        real = _client(colls).place_rows(
            FakeSection([FakeHub(h.title, h.identifier) for h in hubs]),
            label_prefix="shortlist",
            sequence=[("rows", {11})],
        )

        assert preview["repositioned"] == real["repositioned"]

    def test_the_preview_is_right_when_the_wanted_top_hub_is_already_the_shelfs_last(self):
        """The other cell of the new formula. The real pass skips the move only when the hub it wants
        FIRST is already the shelf's last one — the single case where the count is one lower. The
        shelf above never produces it, so that branch went unexercised."""
        hubs = [FakeHub("Kometa One", "k1"), FakeHub("Kometa Two", "k2"), FakeHub("Picked Sarah", "p1")]
        colls = [
            FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
            FakeColl("Kometa One", ["kometa"], 12),
            FakeColl("Kometa Two", ["kometa"], 13),
        ]
        fresh = lambda: FakeSection([FakeHub(h.title, h.identifier) for h in hubs])  # noqa: E731
        preview = _client(colls).place_rows(fresh(), label_prefix="shortlist", sequence=[("rows", {11})], dry_run=True)
        real = _client(colls).place_rows(fresh(), label_prefix="shortlist", sequence=[("rows", {11})])

        assert preview["repositioned"] == real["repositioned"]
        assert real["repositioned"] == 2, "our row is already last, so only the two foreign hubs move"


class TestRetryAudit:
    """What a pass reports after it has ALREADY written, then stopped for another reason."""

    def test_a_retry_that_stops_early_still_reports_what_it_wrote(self):
        """The three early returns hardcoded `repositioned: 0`, which is only true on attempt 1.

        `place_rows` retries when the shelf does not come back as asked — the collapsed-float shape
        this whole change exists for. On the RE-READ, a hub can have changed under us (a co-managing
        tool, or one of our own jobs demoting a row), so the pass can reach "no rows in this library"
        or "anchor not found" having already moved hubs. Reporting 0 writes there loses them from the
        audit entirely, which is the same rule-10 gap one layer down.
        """
        row = FakeHub("Picked Sarah", "p1")
        foreign = FakeHub("Kometa Genre", "k1")
        colls = [FakeColl("Picked Sarah", ["shortlist_sarah"], 11), FakeColl("Kometa Genre", ["kometa"], 12)]

        class VanishingRow(FakeSection):
            """Accepts moves and applies them, but our row reads unpromoted from attempt 2 on."""

            def __init__(self, hubs):
                super().__init__(hubs)
                self.reads = 0

            def managedHubs(self):
                self.reads += 1
                if self.reads > 1:
                    row.promotedToSharedHome = False
                    row.promotedToOwnHome = False
                    row.promotedToRecommended = False
                return list(self._hubs)

            def apply(self, hub, after):  # never converges, so the retry is reached
                return None

        section = VanishingRow([foreign, row])
        result = _client(colls).place_rows(section, label_prefix="shortlist", sequence=[("rows", {11})])

        assert result["repositioned"] > 0, f"hub moves went to Plex and were reported as none: {result}"
        assert result["skipped"] is False, "a pass that wrote is not a pass that did nothing"

    def test_a_pass_that_wrote_and_could_not_place_is_audited_as_both(self):
        """An unplaceable anchor found on a RETRY has to report the writes as well as the reason —
        one record carrying both, not a choice between them."""
        import threading
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from shortlist.engine.models import EngineConfig
        from shortlist.engine.pipeline import RunReport, _apply_placement

        # The real shape: `place_rows` names the anchors it refused, so the audit can report each one
        # by name AND still report the hubs the pass moved before it stopped.
        result = {
            "anchor": "New Series",
            "moved": [],
            "repositioned": 4,
            "skipped": False,
            "reason": "anchor not found",
            "refused": ["New Series"],
        }
        ctx = SimpleNamespace(
            plex=SimpleNamespace(place_rows=lambda *a, **k: result),
            config=EngineConfig(dry_run=False),
            write_lock=threading.Lock(),
        )
        report = RunReport(started_at=datetime.now(UTC))

        _apply_placement(ctx, report, SimpleNamespace(title="Movies", key=1), [("anchor", "New Series")])

        assert len(report.hub_orderings) == 2, report.hub_orderings
        refused = next(e for e in report.hub_orderings if e.get("placed") is False)
        assert refused["anchor"] == "New Series", "the owner must be told WHICH anchor was unusable"
        wrote = next(e for e in report.hub_orderings if e.get("placed") is not False)
        assert wrote["repositioned"] == 4, "and the writes must not vanish from the audit"


class TestTopIsNotInheritedFromAnEarlierAnchor:
    """A row on Top must reach the top, whatever the row above it in Rows order asked for."""

    def test_a_top_row_after_an_anchored_row_is_not_dragged_under_that_anchor(self):
        """`seen_anchor` was never reset, so a block with no marker of its own fell into the PREVIOUS
        anchor's bucket. A row set to "Top" — the default, and documented as "the one position that
        always works" — therefore landed under another row's collection, and the pass then answered
        "already in place", so nothing warned.
        """
        foreign = FakeHub("Kometa One", "k1")
        target = FakeHub("New Series", "ns")
        anchored, top = FakeHub("Picked Sarah", "p1"), FakeHub("Because Sarah", "b1")
        client, section = (
            _client(
                [
                    FakeColl("Picked Sarah", ["shortlist_sarah"], 11),
                    FakeColl("Because Sarah", ["shortlist_sarah"], 12),
                    FakeColl("New Series", ["kometa"], 13),
                    FakeColl("Kometa One", ["kometa"], 14),
                ]
            ),
            FakeSection([foreign, target, anchored, top]),
        )

        client.place_rows(
            section,
            label_prefix="shortlist",
            sequence=[("anchor", "New Series"), ("rows", {11}), ("top", ""), ("rows", {12})],
        )

        assert section.titles() == ["Because Sarah", "Kometa One", "New Series", "Picked Sarah"]

    def test_a_second_row_on_the_same_anchor_still_reaches_it_after_a_top_row(self):
        """The reason the per-title dedupe had to go rather than just gain a Top marker: with the
        dedupe, a third row re-using an earlier anchor lost its marker and inherited whatever the Top
        row had left behind, landing at the top instead."""
        target = FakeHub("New Series", "ns")
        a, b, c = FakeHub("Row A", "a"), FakeHub("Row B", "b"), FakeHub("Row C", "c")
        client, section = (
            _client(
                [
                    FakeColl("Row A", ["shortlist_sarah"], 11),
                    FakeColl("Row B", ["shortlist_sarah"], 12),
                    FakeColl("Row C", ["shortlist_sarah"], 13),
                    FakeColl("New Series", ["kometa"], 14),
                ]
            ),
            FakeSection([target, a, b, c]),
        )

        client.place_rows(
            section,
            label_prefix="shortlist",
            sequence=[
                ("anchor", "New Series"),
                ("rows", {11}),
                ("top", ""),
                ("rows", {12}),
                ("anchor", "New Series"),
                ("rows", {13}),
            ],
        )

        assert section.titles() == ["Row B", "New Series", "Row A", "Row C"]


class TestRowChainsOnTheShelf:
    """The SHELF half of row-to-row placement: what `place_rows` does with a chain's markers.

    The marker half — which marker `_shelf_sequence` emits for a follower — is pinned in
    `test_pipeline.py::TestShelfSequence`, and `test_a_real_config_lands_the_whole_chain_on_the_shelf`
    below wires the two together, because both regressions in this area were sequence-shape
    assertions that got updated to match the broken behaviour.
    """

    def _shelf(self, hubs, colls):
        return _client(colls), FakeSection(hubs)

    def test_a_row_after_another_row_lands_next_to_it_not_at_the_top(self):
        landmark = FakeHub("Kometa Picks", "kp")
        other = FakeHub("Kometa Other", "ko")
        picked, because = FakeHub("Picked", "p"), FakeHub("Because", "b")
        client, section = self._shelf(
            [landmark, other, because, picked],
            [
                FakeColl("Picked", ["shortlist_sarah"], 10),
                FakeColl("Because", ["shortlist_sarah"], 20),
                FakeColl("Kometa Picks", ["kometa"], 30),
                FakeColl("Kometa Other", ["kometa"], 31),
            ],
        )

        result = client.place_rows(
            section,
            label_prefix="shortlist",
            sequence=[
                ("anchor", "Kometa Picks"),
                ("rows", {10}),
                ("anchor", "Kometa Picks"),
                ("rows", {20}),
            ],
        )

        assert section.titles() == ["Kometa Picks", "Picked", "Because", "Kometa Other"]
        assert result["verified"] is True
        assert section.halving_moves == 0

    def test_a_row_before_another_row_lands_directly_above_it(self):
        landmark = FakeHub("Kometa Picks", "kp")
        picked, because = FakeHub("Picked", "p"), FakeHub("Because", "b")
        client, section = self._shelf(
            [landmark, picked, because],
            [
                FakeColl("Picked", ["shortlist_sarah"], 10),
                FakeColl("Because", ["shortlist_sarah"], 20),
                FakeColl("Kometa Picks", ["kometa"], 30),
            ],
        )

        # `because` before `picked`, `picked` after the collection — so the row order settled
        # [because, picked] and both carry the head's marker.
        client.place_rows(
            section,
            label_prefix="shortlist",
            sequence=[
                ("anchor", "Kometa Picks"),
                ("rows", {20}),
                ("anchor", "Kometa Picks"),
                ("rows", {10}),
            ],
        )

        assert section.titles() == ["Kometa Picks", "Because", "Picked"]

    def test_a_chain_of_three_stays_in_order_under_its_landmark(self):
        landmark = FakeHub("Kometa Picks", "kp")
        hubs = [landmark, FakeHub("Popular", "pop"), FakeHub("Because", "b"), FakeHub("Picked", "p")]
        colls = [
            FakeColl("Picked", ["shortlist_sarah"], 10),
            FakeColl("Because", ["shortlist_sarah"], 20),
            FakeColl("Popular", ["shortlist_sarah"], 30),
            FakeColl("Kometa Picks", ["kometa"], 40),
        ]
        client, section = self._shelf(hubs, colls)

        client.place_rows(
            section,
            label_prefix="shortlist",
            sequence=[
                ("anchor", "Kometa Picks"),
                ("rows", {10}),
                ("anchor", "Kometa Picks"),
                ("rows", {20}),
                ("anchor", "Kometa Picks"),
                ("rows", {30}),
            ],
        )

        assert section.titles() == ["Kometa Picks", "Picked", "Because", "Popular"]

    def test_a_real_config_lands_the_whole_chain_on_the_shelf(self):
        """End to end over the module boundary: row CONFIG -> `_shelf_sequence` -> `place_rows` -> shelf.

        Every other test here hands `place_rows` a sequence written by hand, so a regression in which
        marker `_shelf_sequence` chooses cannot fail them — and that is exactly the regression this
        area has had twice. The config below is the maintainer's own: one row after a collection, the
        next after that row, the third after the second.
        """
        from datetime import UTC, datetime

        from shortlist.engine.models import HubAnchor, RowSpec, RunReport
        from shortlist.engine.pipeline import _shelf_sequence

        specs = [
            RowSpec(
                slug="picked",
                name_template="Picked",
                size=10,
                hub_anchors={"2": HubAnchor(anchor_title="Kometa Picks")},
            ),
            RowSpec(
                slug="because", name_template="Because", size=10, hub_anchors={"2": HubAnchor(anchor_row="picked")}
            ),
            RowSpec(
                slug="popular", name_template="Popular", size=10, hub_anchors={"2": HubAnchor(anchor_row="because")}
            ),
        ]
        keys = {"picked": {10}, "because": {20}, "popular": {30}}
        sequence = _shelf_sequence(specs, "2", keys, "TV Shows", {}, RunReport(started_at=datetime.now(UTC)))

        landmark = FakeHub("Kometa Picks", "kp")
        hubs = [landmark, FakeHub("Popular", "pop"), FakeHub("Because", "b"), FakeHub("Picked", "p")]
        client, section = self._shelf(
            hubs,
            [
                FakeColl("Picked", ["shortlist_sarah"], 10),
                FakeColl("Because", ["shortlist_sarah"], 20),
                FakeColl("Popular", ["shortlist_sarah"], 30),
                FakeColl("Kometa Picks", ["kometa"], 40),
            ],
        )

        result = client.place_rows(section, label_prefix="shortlist", sequence=sequence)

        assert section.titles() == ["Kometa Picks", "Picked", "Because", "Popular"]
        assert result["verified"] is True
        assert section.halving_moves == 0


class TestOneBadAnchorDoesNotBlockTheLibrary:
    """An unusable anchor costs its own row its placement, and nothing else."""

    def test_the_other_rows_are_still_placed(self):
        """The anchor-validation loop returned for the WHOLE library on the first unusable anchor.

        Tightening `can_anchor` to require promotion widened the trigger from "a foreign collection
        promoted nowhere" to "any built-in the owner switched off in Manage Recommendations" — and the
        recorded capture shows two of those on the maintainer's own server. So one row pointed at a
        switched-off hub stopped every other row in that library from being placed, silently.
        """
        good = FakeHub("Kometa Picks", "kp")
        off = FakeHub("By Genre", "movie.genre", collection=False, promoted=False)
        r_ok, r_bad = FakeHub("Picked", "p"), FakeHub("Because", "b")
        client, section = (
            _client(
                [
                    FakeColl("Picked", ["shortlist_sarah"], 10),
                    FakeColl("Because", ["shortlist_sarah"], 20),
                    FakeColl("Kometa Picks", ["kometa"], 30),
                ]
            ),
            FakeSection([good, off, r_bad, r_ok]),
        )

        result = client.place_rows(
            section,
            label_prefix="shortlist",
            sequence=[
                ("anchor", "Kometa Picks"),
                ("rows", {10}),
                ("anchor", "By Genre"),
                ("rows", {20}),
            ],
        )

        assert result["refused"] == ["By Genre"], result
        # The placed row lands under its anchor, and the refused one keeps its place among the hubs
        # we do not move — it is not dropped, and it does not sit between an anchor and its row.
        visible = [t for t in section.titles() if t != "By Genre"]
        assert visible == ["Kometa Picks", "Picked", "Because"], section.titles()

    def test_the_preview_does_not_credit_a_row_it_cannot_place(self):
        """The same accounting, in the branch an owner reads BEFORE trusting a real pass. The live
        path keys `moved` on the rows it actually placed; the preview kept keying it on ownership, so
        it promised to move a row while a sibling record said that row could not be placed."""
        good = FakeHub("Kometa Picks", "kp")
        off = FakeHub("By Genre", "movie.genre", collection=False, promoted=False)
        r_ok, r_bad = FakeHub("Picked", "p"), FakeHub("Because", "b")
        colls = [
            FakeColl("Picked", ["shortlist_sarah"], 10),
            FakeColl("Because", ["shortlist_sarah"], 20),
            FakeColl("Kometa Picks", ["kometa"], 30),
        ]
        sequence = [
            ("anchor", "Kometa Picks"),
            ("rows", {10}),
            ("anchor", "By Genre"),
            ("rows", {20}),
        ]
        hubs = [good, off, r_bad, r_ok]
        preview = _client(colls).place_rows(
            FakeSection(
                [
                    FakeHub(
                        h.title,
                        h.identifier,
                        collection=h.identifier.startswith(("kp", "p", "b")),
                        promoted=h.promotedToSharedHome,
                    )
                    for h in hubs
                ]
            ),
            label_prefix="shortlist",
            sequence=sequence,
            dry_run=True,
        )

        assert preview["moved"] == ["Picked"], preview
        assert preview["refused"] == ["By Genre"]

    def test_each_refused_anchor_is_audited_by_name(self):
        """The audit used to join every anchor into one string with a bare reason, so an owner could
        not tell WHICH of their settings was doing nothing."""
        import threading
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from shortlist.engine.models import EngineConfig
        from shortlist.engine.pipeline import RunReport, _apply_placement

        result = {
            "anchor": "Kometa Picks",
            "moved": ["Picked"],
            "repositioned": 3,
            "skipped": False,
            "verified": True,
            "refused": ["By Genre", "Archive 2019"],
        }
        ctx = SimpleNamespace(
            plex=SimpleNamespace(place_rows=lambda *a, **k: result),
            config=EngineConfig(dry_run=False),
            write_lock=threading.Lock(),
        )
        report = RunReport(started_at=datetime.now(UTC))

        _apply_placement(ctx, report, SimpleNamespace(title="Movies", key=1), [("rows", {10})])

        refused = [e for e in report.hub_orderings if e.get("placed") is False]
        assert [e["anchor"] for e in refused] == ["By Genre", "Archive 2019"]
        wrote = [e for e in report.hub_orderings if e.get("placed") is not False]
        assert len(wrote) == 1 and wrote[0]["repositioned"] == 3, "the successful writes are still audited"
