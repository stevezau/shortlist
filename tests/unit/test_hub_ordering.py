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
