"""Unit tests for PlexClient.order_owned_hubs — the Recommended-shelf placement of Shortlist rows.

The Plex move mechanic itself is verified live on a real server; these pin the DECISION logic: only
our hubs move, the anchor is read-only (Kometa coexistence), it's idempotent, and dry-run is inert.
"""

from shortlist.engine.clients.plex_pms import PlexClient

_UNSET = "UNSET"  # sentinel: move() was never called on this hub


class FakeHub:
    def __init__(self, title: str, ident: str):
        self.title = title
        self.identifier = ident
        self.moved_after = _UNSET

    def reload(self):
        return self

    def move(self, after=None):
        self.moved_after = after


class FakeLabel:
    def __init__(self, tag: str):
        self.tag = tag


class FakeColl:
    def __init__(self, title: str, tags: list[str]):
        self.title = title
        self.labels = [FakeLabel(t) for t in tags]


class FakeSection:
    def __init__(self, hubs: list[FakeHub], title: str = "TV Shows", key: int = 2):
        self._hubs = hubs
        self.title = title
        self.key = key

    def managedHubs(self):
        return list(self._hubs)


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


def test_only_titles_moves_just_that_subset_and_never_a_sibling_or_foreign_hub():
    anchor = FakeHub("New Series", "a")
    sibling = FakeHub("Picked for You", "o1")  # ours, but NOT in the requested subset
    target_row = FakeHub("Hidden Gems", "o2")  # ours, IN the subset
    foreign = FakeHub("Kometa Genre", "g")
    section = FakeSection([anchor, sibling, foreign, target_row])
    client = _client(
        [
            FakeColl("Picked for You", ["shortlist_sarah"]),
            FakeColl("Hidden Gems", ["shortlist_sarah"]),
            FakeColl("Kometa Genre", ["kometa"]),
        ]
    )

    result = client.order_owned_hubs(
        section, label_prefix="shortlist", anchor_title="New Series", only_titles={"Hidden Gems"}
    )

    assert result["moved"] == ["Hidden Gems"]
    assert target_row.moved_after is anchor  # only the requested subset moves
    assert sibling.moved_after == _UNSET  # a sibling Shortlist row outside the subset is untouched
    assert foreign.moved_after == _UNSET  # a foreign (Kometa) hub is never touched


def test_only_titles_is_idempotent_when_the_subset_already_sits_after_the_anchor():
    anchor = FakeHub("New Series", "a")
    target_row = FakeHub("Hidden Gems", "o2")  # already directly after the anchor
    sibling = FakeHub("Picked for You", "o1")
    section = FakeSection([anchor, target_row, sibling])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"]), FakeColl("Hidden Gems", ["shortlist_sarah"])])

    result = client.order_owned_hubs(
        section, label_prefix="shortlist", anchor_title="New Series", only_titles={"Hidden Gems"}
    )

    assert result["skipped"] is True and result["reason"] == "already in place"
    assert target_row.moved_after == _UNSET


def test_a_row_can_never_be_anchored_to_a_sibling_shortlist_hub():
    sibling = FakeHub("Picked for You", "o1")
    target_row = FakeHub("Hidden Gems", "o2")
    section = FakeSection([sibling, target_row])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"]), FakeColl("Hidden Gems", ["shortlist_sarah"])])

    # Naming our OWN sibling row as the anchor is refused (it's excluded from anchor candidates).
    result = client.order_owned_hubs(
        section, label_prefix="shortlist", anchor_title="Picked for You", only_titles={"Hidden Gems"}
    )

    assert result["skipped"] is True and result["reason"] == "anchor not found"
    assert target_row.moved_after == _UNSET


def _order_ctx(cfg, plex):
    import threading
    from types import SimpleNamespace

    section = SimpleNamespace(key=2, title="TV Shows")
    return SimpleNamespace(config=cfg, delivery_sections=[section], plex=plex, write_lock=threading.Lock())


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

    # One call, whole library, no title subset (the robust global path).
    plex.order_owned_hubs.assert_called_once()
    assert plex.order_owned_hubs.call_args.kwargs["only_titles"] is None
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

    # Two groups: the default-anchored 'picked' rows and the overridden 'gems' rows, each its own subset.
    groups = {
        frozenset(c.kwargs["only_titles"]): c.kwargs["anchor_title"] for c in plex.order_owned_hubs.call_args_list
    }
    assert groups == {
        frozenset({"Picked A", "Picked B"}): "Default Anchor",
        frozenset({"Gems A", "Gems B"}): "Gems Anchor",
    }


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
    assert set(kwargs["only_titles"]) == {"Gems A", "Gems B"}


def test_order_phase_skips_an_overridden_row_with_no_delivered_titles():
    from unittest.mock import MagicMock

    from shortlist.engine.models import EngineConfig, HubAnchor, RowSpec
    from shortlist.engine.pipeline import _order_phase

    plex = MagicMock()
    plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["x"]}
    # 'ghost' overrides but delivered no titles this run (absent from placement_titles) -> no move.
    cfg = EngineConfig(
        hub_anchors={"2": HubAnchor("Default", False)},
        rows=[
            RowSpec(slug="ghost", name_template="Ghost", size=10, hub_anchors={"2": HubAnchor("Ghost Anchor", False)})
        ],
    )
    _order_phase(_order_ctx(cfg, plex), _report_with_titles())

    plex.order_owned_hubs.assert_not_called()  # empty title set -> nothing to move


def test_dry_run_reports_the_move_without_writing():
    anchor = FakeHub("New Series", "a")
    r1 = FakeHub("Picked for You", "o1")
    section = FakeSection([anchor, FakeHub("Genre", "g"), r1])
    client = _client([FakeColl("Picked for You", ["shortlist_sarah"])])

    result = client.order_owned_hubs(section, label_prefix="shortlist", anchor_title="New Series", dry_run=True)

    assert result["dry_run"] is True
    assert result["moved"] == ["Picked for You"]
    assert r1.moved_after == _UNSET  # dry-run never actually moves a hub
