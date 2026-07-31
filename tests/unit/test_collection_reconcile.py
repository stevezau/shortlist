"""shortlist/server/services/collection_reconcile.py: the on-demand Plex reconciles that run outside
the nightly pipeline (row delete/rename/build-flip/audience-shrink).

Modeled on `tests/unit/test_delivery.py` — a `MagicMock(spec=PlexClient)` stands in for the server,
and assertions land on the exact label/title arguments the SUT hands it, not just "was it called".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.delivery import row_marker
from shortlist.engine.models import LABEL_PREFIX, SHARED_LABEL_PREFIX
from shortlist.server.db.models import DEFAULT_SLUG, Collection, Delivery, Event, Run, RunUser, User
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services import collection_reconcile as rec
from shortlist.server.settings_store import SettingsStore


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


def _state(sessions, plex: MagicMock, *, dry_run: bool = False) -> SimpleNamespace:
    """Stands in for app.state: a real DB plus a run_service whose `build_context` hands back `plex`
    and the EFFECTIVE dry_run — the value `ctx.config.dry_run` carries after the safe-mode chokepoint
    has already run, independent of whatever `dry_run` a caller passes to `build_context` itself."""
    ctx = SimpleNamespace(plex=plex, config=SimpleNamespace(dry_run=dry_run))
    return SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(build_context=lambda **kw: ctx), secrets=None)


def _section(title: str, key: str = "1") -> MagicMock:
    section = MagicMock()
    section.title = title
    section.key = key
    return section


def _collection(title: str) -> MagicMock:
    collection = MagicMock()
    collection.title = title
    return collection


def _add_user(sessions, *, slug: str, account_id: int, username: str | None = None, nickname: str = "") -> int:
    with sessions() as session:
        user = User(
            plex_account_id=account_id,
            username=username or slug,
            slug=slug,
            nickname=nickname,
            user_type="shared",
            enabled=True,
        )
        session.add(user)
        session.commit()
        return user.id


class TestRowTemplate:
    """The name template a row's collections are titled from — read differently for the DEFAULT row
    (a global setting) than for any other (its own `name_template`, else its plain `name`)."""

    def test_default_row_reads_the_global_setting(self, sessions):
        with sessions() as session:
            SettingsStore(session).set("row.name_template", "✨ Custom Global")
            assert rec.row_template(session, DEFAULT_SLUG) == "✨ Custom Global"

    def test_default_row_falls_back_to_the_setting_default_when_unset(self, sessions):
        with sessions() as session:
            assert rec.row_template(session, DEFAULT_SLUG) == "✨ {library_name} Picked for You"

    def test_other_rows_prefer_their_own_name_template(self, sessions):
        with sessions() as session:
            session.add(Collection(slug="comedy", name="Comedy Night", name_template="{user}'s Comedy"))
            session.commit()
            assert rec.row_template(session, "comedy") == "{user}'s Comedy"

    def test_other_rows_fall_back_to_the_plain_name_with_no_template_set(self, sessions):
        with sessions() as session:
            session.add(Collection(slug="comedy", name="Comedy Night", name_template=""))
            session.commit()
            assert rec.row_template(session, "comedy") == "Comedy Night"

    def test_a_deleted_row_has_no_template_left_to_read(self, sessions):
        with sessions() as session:
            assert rec.row_template(session, "gone") == ""


class TestLedgerKeys:
    """The primary way a per-person collection is found: the delivery ledger's ratingKeys, scoped to
    one row, since a title match cannot survive a `{top_seed}` row's every-run-different title."""

    def test_maps_user_slug_to_the_set_of_rating_keys_recorded_for_this_row(self, sessions):
        with sessions() as session:
            session.add_all(
                [
                    Delivery(collection_slug="comedy", user_slug="sarah", library_key="1", rating_key=111),
                    Delivery(collection_slug="comedy", user_slug="sarah", library_key="2", rating_key=222),
                    Delivery(collection_slug="comedy", user_slug="mike", library_key="1", rating_key=333),
                    Delivery(collection_slug="other-row", user_slug="sarah", library_key="1", rating_key=999),
                ]
            )
            session.commit()
            assert rec._ledger_keys(session, "comedy") == {"sarah": {111, 222}, "mike": {333}}

    def test_a_falsy_rating_key_is_never_recorded(self, sessions):
        with sessions() as session:
            session.add(Delivery(collection_slug="comedy", user_slug="sarah", library_key="1", rating_key=0))
            session.commit()
            assert rec._ledger_keys(session, "comedy") == {}


class TestDeliveredTitlesByUser:
    """The secondary, fallback source of candidate titles: the latest completed run's breakdown."""

    def test_reads_the_latest_completed_runs_breakdown_for_this_row(self, sessions):
        sarah_id = _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=sarah_id,
                    breakdown=[
                        {"row_slug": "comedy", "row_title": "Comedy Night", "library_title": "Movies"},
                        {"row_slug": "picked", "row_title": "Other Row", "library_title": "Movies"},
                    ],
                )
            )
            session.commit()
        with sessions() as session:
            assert rec._delivered_titles_by_user(session, "comedy") == {sarah_id: {"Comedy Night": "Movies"}}

    def test_no_completed_runs_yields_nothing(self, sessions):
        with sessions() as session:
            assert rec._delivered_titles_by_user(session, "comedy") == {}

    def test_the_latest_run_scoped_to_a_different_row_reports_nothing_for_this_one(self, sessions):
        """Rows have their own crons: the morning after row A ran, row B's slug is absent from the
        latest breakdown entirely — this source is a fallback for exactly this reason."""
        sarah_id = _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=sarah_id,
                    breakdown=[{"row_slug": "picked", "row_title": "Other Row", "library_title": "Movies"}],
                )
            )
            session.commit()
        with sessions() as session:
            assert rec._delivered_titles_by_user(session, "comedy") == {}

    def test_a_still_running_run_is_ignored_in_favour_of_the_last_completed_one(self, sessions):
        sarah_id = _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            done = Run(trigger="manual", status="ok")
            session.add(done)
            session.flush()
            session.add(
                RunUser(
                    run_id=done.id,
                    user_id=sarah_id,
                    breakdown=[{"row_slug": "comedy", "row_title": "Comedy Night", "library_title": "Movies"}],
                )
            )
            session.add(Run(trigger="manual", status="running"))
            session.commit()
        with sessions() as session:
            assert rec._delivered_titles_by_user(session, "comedy") == {sarah_id: {"Comedy Night": "Movies"}}


class TestForgetDeliveries:
    def test_scoped_to_the_users_and_sections_the_sweep_actually_covered(self, sessions):
        """A NARROWED row (dropped a library) must forget only the libraries it left — the entry for
        one it still uses is the only way a `{top_seed}` title could ever be addressed again."""
        with sessions() as session:
            session.add_all(
                [
                    Delivery(collection_slug="comedy", user_slug="sarah", library_key="1", rating_key=1),
                    Delivery(collection_slug="comedy", user_slug="sarah", library_key="2", rating_key=2),
                    Delivery(collection_slug="comedy", user_slug="mike", library_key="1", rating_key=3),
                ]
            )
            session.commit()
            rec._forget_deliveries(session, "comedy", user_slugs={"sarah"}, in_sections={"1"})
            session.commit()
        with sessions() as session:
            remaining = {(d.user_slug, d.library_key) for d in session.query(Delivery).all()}
        assert remaining == {("sarah", "2"), ("mike", "1")}

    def test_forget_user_deliveries_drops_every_row_for_that_person_regardless_of_which_row(self, sessions):
        with sessions() as session:
            session.add_all(
                [
                    Delivery(collection_slug="comedy", user_slug="sarah", library_key="1", rating_key=1),
                    Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=2),
                    Delivery(collection_slug="picked", user_slug="mike", library_key="1", rating_key=3),
                ]
            )
            session.commit()
            rec.forget_user_deliveries(session, "sarah")
            session.commit()
        with sessions() as session:
            remaining = {d.user_slug for d in session.query(Delivery).all()}
        assert remaining == {"mike"}


def _udata(uid: int, slug: str, account_id: int, prefs: dict | None = None) -> dict:
    return {
        "id": uid,
        "slug": slug,
        "username": slug,
        "nickname": "",
        "plex_account_id": account_id,
        "user_type": "shared",
        "prefs": prefs or {},
    }


class TestWalkRowCollections:
    """The one place the "which collection is this row's?" question is answered for a per-person row —
    shared by the removal and poster-reset passes."""

    def _ctx(self, *sections: MagicMock) -> SimpleNamespace:
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = list(sections)
        return SimpleNamespace(plex=plex)

    def test_displays_are_the_union_of_rendered_and_recorded_titles(self):
        ctx = self._ctx(_section("Movies"))
        seen: dict[str, set[str]] = {}

        rec._walk_row_collections(
            ctx,
            [_udata(1, "sarah", 100)],
            slug="comedy",
            template="My Row",
            titles_by_user={1: {"Recorded Title": "Movies"}},
            action=lambda user, displays: seen.setdefault(user["slug"], displays),
        )

        assert seen["sarah"] == {"My Row", "Recorded Title"}

    def test_the_default_row_lets_a_users_own_template_override_win(self):
        ctx = self._ctx(_section("Movies"))
        seen: dict[str, set[str]] = {}

        rec._walk_row_collections(
            ctx,
            [_udata(1, "sarah", 100, prefs={"row_name_tpl": "Sarah's Own Title"})],
            slug=DEFAULT_SLUG,
            template="Global Default",
            titles_by_user={},
            action=lambda user, displays: seen.setdefault(user["slug"], displays),
        )

        assert seen["sarah"] == {"Sarah's Own Title"}

    def test_a_non_default_row_ignores_the_per_user_override(self):
        ctx = self._ctx(_section("Movies"))
        seen: dict[str, set[str]] = {}

        rec._walk_row_collections(
            ctx,
            [_udata(1, "sarah", 100, prefs={"row_name_tpl": "Sarah's Own Title"})],
            slug="comedy",
            template="Row Template",
            titles_by_user={},
            action=lambda user, displays: seen.setdefault(user["slug"], displays),
        )

        assert seen["sarah"] == {"Row Template"}

    def test_only_user_ids_skips_everyone_else(self):
        ctx = self._ctx(_section("Movies"))
        visited: list[str] = []

        rec._walk_row_collections(
            ctx,
            [_udata(1, "sarah", 100), _udata(2, "mike", 200)],
            slug="comedy",
            template="Row",
            titles_by_user={},
            action=lambda user, displays: visited.append(user["slug"]),
            only_user_ids={2},
        )

        assert visited == ["mike"]


class TestReconcileRowRemoval:
    """`build == "shared"` vs `"per_person"` walk entirely different label spaces: a shared row is
    `shortlist__shared_<slug>` (double underscore — unreachable from any user slug, a privacy
    invariant); per-person is `shortlist_<user_slug>`, one label per PERSON shared by all their rows."""

    def test_shared_build_removes_everything_under_the_double_underscore_shared_label(self, sessions):
        section = _section("Movies")
        collection = _collection("Movie Night")
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            [collection] if label == "shortlist__shared_movienight" else []
        )
        removed: list[str] = []

        dry_run = rec._reconcile_row_removal(
            _state(sessions, plex), slug="movienight", build="shared", dry_run=False, removed=removed
        )

        assert dry_run is False
        assert removed == ["Movie Night"]
        plex.delete_owned_collection.assert_called_once_with(collection, LABEL_PREFIX)
        label_used = plex.find_owned_collections.call_args.args[1]
        assert label_used == f"{SHARED_LABEL_PREFIX}movienight" == "shortlist__shared_movienight"

    def test_shared_build_with_only_user_ids_does_nothing(self, sessions):
        """Who SEES a shared row is a share-filter concern, not a per-user Plex removal — an
        audience-shrink cleanup on a shared row must never touch the one collection everyone shares."""
        plex = MagicMock(spec=PlexClient)
        removed: list[str] = []

        dry_run = rec._reconcile_row_removal(
            _state(sessions, plex), slug="movienight", build="shared", dry_run=False, removed=removed, only_user_ids={1}
        )

        assert removed == []
        assert dry_run is False
        plex.find_owned_collections.assert_not_called()

    def test_per_person_build_removes_one_users_collection_by_rendered_title(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        section = _section("Movies")
        collection = _collection("My Row" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        removed: list[str] = []

        dry_run = rec._reconcile_row_removal(
            _state(sessions, plex),
            slug="picked",
            build="per_person",
            dry_run=False,
            removed=removed,
            template="My Row",
        )

        assert dry_run is False
        assert removed == ["My Row"]
        plex.delete_owned_collection.assert_called_once_with(collection, LABEL_PREFIX)
        label_used = plex.find_owned_collections.call_args.args[1]
        assert label_used == f"{LABEL_PREFIX}_sarah" == "shortlist_sarah"

    def test_a_user_with_nothing_matching_is_left_untouched(self, sessions):
        _add_user(sessions, slug="mike", account_id=200)
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        removed: list[str] = []

        rec._reconcile_row_removal(
            _state(sessions, plex),
            slug="picked",
            build="per_person",
            dry_run=False,
            removed=removed,
            template="",  # unfillable/blank -> renders to nothing, and there is no ledger entry either
        )

        assert removed == []
        plex.find_owned_collections.assert_not_called()

    def test_a_top_seed_row_is_found_by_ledger_identity_not_by_title(self, sessions):
        """`{top_seed}` renders a different title every run, so no computed display can ever match
        it — the delivery ledger's ratingKey is the only way to find it again."""
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(Delivery(collection_slug="comedy", user_slug="sarah", library_key="1", rating_key=777))
            session.commit()
        section = _section("Movies", key="1")
        collection = _collection("Because you watched Fargo" + row_marker(100))
        collection.ratingKey = 777
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        removed: list[str] = []

        rec._reconcile_row_removal(
            _state(sessions, plex),
            slug="comedy",
            build="per_person",
            dry_run=False,
            removed=removed,
            template="Because you watched {top_seed}",
        )

        plex.delete_owned_collection.assert_called_once_with(collection, LABEL_PREFIX)

    def test_the_ledger_is_forgotten_only_after_a_real_removal(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=555))
            session.commit()
        section = _section("Movies", key="1")
        collection = _collection("My Row" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        removed: list[str] = []

        rec._reconcile_row_removal(
            _state(sessions, plex), slug="picked", build="per_person", dry_run=False, removed=removed, template="My Row"
        )

        with sessions() as session:
            assert session.query(Delivery).filter_by(user_slug="sarah").count() == 0

    def test_a_dry_run_removal_leaves_the_ledger_and_plex_untouched(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=555))
            session.commit()
        section = _section("Movies", key="1")
        collection = _collection("My Row" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        removed: list[str] = []

        dry_run = rec._reconcile_row_removal(
            _state(sessions, plex), slug="picked", build="per_person", dry_run=True, removed=removed, template="My Row"
        )

        assert dry_run is True
        assert removed == ["My Row"]  # still reported as WOULD remove
        plex.delete_owned_collection.assert_not_called()
        with sessions() as session:
            assert session.query(Delivery).filter_by(user_slug="sarah").count() == 1  # ledger untouched

    def test_safe_mode_forces_dry_run_on_even_when_the_caller_asked_for_a_real_removal(self, sessions):
        """`ctx.config.dry_run or dry_run` is a FLOOR: it may force a preview ON, never off."""
        _add_user(sessions, slug="sarah", account_id=100)
        section = _section("Movies")
        collection = _collection("My Row" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        removed: list[str] = []

        dry_run = rec._reconcile_row_removal(
            _state(sessions, plex, dry_run=True),
            slug="picked",
            build="per_person",
            dry_run=False,  # the caller asked for a REAL removal
            removed=removed,
            template="My Row",
        )

        assert dry_run is True
        plex.delete_owned_collection.assert_not_called()

    def test_in_sections_narrows_the_sweep_to_the_libraries_the_row_still_left(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        movies, shows = _section("Movies", key="1"), _section("TV Shows", key="2")
        movies_c, shows_c = _collection("My Row" + row_marker(100)), _collection("My Row" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies, shows]

        def find(sec, label):
            if label != "shortlist_sarah":
                return []
            return [movies_c] if sec is movies else [shows_c]

        plex.find_owned_collections.side_effect = find
        removed: list[str] = []

        rec._reconcile_row_removal(
            _state(sessions, plex),
            slug="picked",
            build="per_person",
            dry_run=False,
            removed=removed,
            template="My Row",
            in_sections={"2"},
        )

        plex.delete_owned_collection.assert_called_once_with(shows_c, LABEL_PREFIX)

    def test_only_user_ids_narrows_the_per_person_sweep_to_specific_people(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        mike_id = _add_user(sessions, slug="mike", account_id=200)
        section = _section("Movies")
        sarah_c, mike_c = _collection("My Row" + row_marker(100)), _collection("My Row" + row_marker(200))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda sec, label: {
            "shortlist_sarah": [sarah_c],
            "shortlist_mike": [mike_c],
        }.get(label, [])
        removed: list[str] = []

        rec._reconcile_row_removal(
            _state(sessions, plex),
            slug="picked",
            build="per_person",
            dry_run=False,
            removed=removed,
            template="My Row",
            only_user_ids={mike_id},
        )

        plex.delete_owned_collection.assert_called_once_with(mike_c, LABEL_PREFIX)
        assert removed == ["My Row"]


class TestReconcilePosterReset:
    """Cosmetic and privacy-neutral — same label spaces as removal, but driven purely by safe mode
    (no caller-supplied `dry_run` at all)."""

    def test_shared_build_resets_every_collection_under_the_double_underscore_label(self, sessions):
        section = _section("Movies")
        collection = _collection("Movie Night")
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            [collection] if label == "shortlist__shared_movienight" else []
        )
        reset: list[str] = []

        dry_run = rec._reconcile_poster_reset(_state(sessions, plex), slug="movienight", build="shared", reset=reset)

        assert dry_run is False
        assert reset == ["Movies"]
        plex.reset_poster.assert_called_once_with(collection)

    def test_per_person_build_resets_a_users_matching_collection(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(Collection(slug="comedy", name="Comedy", name_template="Comedy Nights"))
            session.commit()
        section = _section("Movies")
        collection = _collection("Comedy Nights" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        reset: list[str] = []

        dry_run = rec._reconcile_poster_reset(_state(sessions, plex), slug="comedy", build="per_person", reset=reset)

        assert dry_run is False
        assert reset == ["Movies"]
        plex.reset_poster.assert_called_once_with(collection)

    def test_per_person_build_skips_a_user_with_nothing_to_reset(self, sessions):
        _add_user(sessions, slug="mike", account_id=200)
        with sessions() as session:
            session.add(Collection(slug="comedy", name="Comedy", name_template=""))
            session.commit()
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = []  # nothing renders, no recorded titles either -> empty displays
        reset: list[str] = []

        rec._reconcile_poster_reset(_state(sessions, plex), slug="comedy", build="per_person", reset=reset)

        assert reset == []
        plex.reset_poster.assert_not_called()

    def test_effective_dry_run_comes_entirely_from_the_context_not_a_caller_argument(self, sessions):
        with sessions() as session:
            session.add(Collection(slug="comedy", name="Comedy", name_template=""))
            session.commit()
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = []
        reset: list[str] = []

        dry_run = rec._reconcile_poster_reset(
            _state(sessions, plex, dry_run=True), slug="comedy", build="per_person", reset=reset
        )

        assert dry_run is True


class TestReconcileRowRenameIter:
    """Finds collections directly from Plex by label, identifying THIS row's by its OLD rendered
    title — the one thing that must never be skipped, on pain of retitling a different row."""

    def test_refuses_to_rename_when_there_is_no_previous_title_to_match_on(self, sessions):
        """The bug this guards: renaming "whatever we find" under a shared label can retitle a
        DIFFERENT row's collection, stranding it — addressable by nothing, duplicated next run."""
        _add_user(sessions, slug="sarah", account_id=100)
        collection = _collection("Anything" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.return_value = [collection]

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template=None
            )
        )

        collection.editTitle.assert_not_called()
        assert events == [{"done": True, "total": 0}]

    def test_an_empty_old_template_also_refuses_since_falsy_is_falsy(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        collection = _collection("Anything" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.return_value = [collection]

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template=""
            )
        )

        collection.editTitle.assert_not_called()
        assert events == [{"done": True, "total": 0}]

    def test_renames_the_collection_matching_the_old_rendered_title(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        collection = _collection("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template="Old Name"
            )
        )

        collection.editTitle.assert_called_once_with("New Name" + row_marker(100))
        assert {
            "user": "sarah",
            "display_name": "sarah",
            "old": "Old Name",
            "new": "New Name",
            "libraries": ["Movies"],
        } in (events)
        assert events[-1] == {"done": True, "total": 1}

    def test_does_not_touch_a_different_row_sharing_the_same_label(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        this_row = _collection("Old Name" + row_marker(100))
        other_row = _collection("Some Other Row" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            [this_row, other_row] if label == "shortlist_sarah" else []
        )

        list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template="Old Name"
            )
        )

        this_row.editTitle.assert_called_once_with("New Name" + row_marker(100))
        other_row.editTitle.assert_not_called()

    def test_shared_build_renames_by_label_alone_no_marker_no_title_match_needed(self, sessions):
        collection = _collection("Old Shared Name")  # no marker: ONE collection server-wide
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            [collection] if label == "shortlist__shared_movienight" else []
        )

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="movienight",
                new_template="New Shared Name",
                old_template=None,  # not required for shared: one label, one membership
                build="shared",
            )
        )

        collection.editTitle.assert_called_once_with("New Shared Name")
        assert events[-1] == {"done": True, "total": 1}

    def test_dry_run_reports_without_writing(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        collection = _collection("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="comedy",
                new_template="New Name",
                old_template="Old Name",
                dry_run=True,
            )
        )

        collection.editTitle.assert_not_called()
        assert events[-1] == {"done": True, "total": 1}  # still counted as a would-be rename

    def test_a_pms_failure_for_one_user_is_yielded_and_redacted_without_losing_anothers_success(self, sessions):
        _add_user(sessions, slug="ann", account_id=100)
        _add_user(sessions, slug="bob", account_id=200)
        ann_c = _collection("Old Name" + row_marker(100))
        bob_c = _collection("Old Name" + row_marker(200))
        bob_c.editTitle.side_effect = RuntimeError("PMS error at http://pms/x?X-Plex-Token=SEKRETVALUE")
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: {
            "shortlist_ann": [ann_c],
            "shortlist_bob": [bob_c],
        }.get(label, [])

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template="Old Name"
            )
        )

        errors = [e for e in events if "error" in e]
        assert len(errors) == 1
        assert errors[0]["user"] == "bob"
        assert "SEKRETVALUE" not in errors[0]["error"]
        assert any(e.get("user") == "ann" and "old" in e for e in events)
        assert events[-1]["total"] == 1  # only ann's success is counted

    def test_an_unfillable_top_seed_template_skips_rather_than_retitling_to_the_blank_default(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        collection = _collection("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.return_value = [collection]

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="comedy",
                new_template="Because you watched {top_seed}",
                old_template="Old Name",
            )
        )

        collection.editTitle.assert_not_called()
        assert events == [{"done": True, "total": 0}]

    def test_old_display_names_covers_a_nickname_change_with_an_unchanged_template(self, sessions):
        """`{user}` renders the NEW nickname on both sides without this — matching nothing and
        leaving the old-titled collection on Plex for the next run to duplicate."""
        _add_user(sessions, slug="sarah", account_id=100, nickname="Sarah J")
        collection = _collection("For Old Nick" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="comedy",
                new_template="For {user}",
                old_template="For {user}",
                old_display_names={"sarah": "Old Nick"},
            )
        )

        collection.editTitle.assert_called_once_with("For Sarah J" + row_marker(100))
        assert events[-1] == {"done": True, "total": 1}


class TestRunReconcileAudit:
    """`run_reconcile` runs the removal in an executor and writes the audit event (rule 10) — even a
    mid-loop failure must record what was already removed, and the error must be redacted (rule 9)."""

    def test_a_successful_removal_is_audited_with_the_effective_dry_run_value(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(Collection(slug="comedy", name="Comedy", name_template="My Row"))
            session.commit()
        collection = _collection("My Row" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        state = _state(sessions, plex)

        removed, error = asyncio.run(
            rec.run_reconcile(state, slug="comedy", build="per_person", dry_run=False, scope="row.delete")
        )

        assert removed == ["My Row"]
        assert error is None
        with sessions() as session:
            event = session.query(Event).filter_by(scope="row.delete").one()
        assert event.level == "warning"
        assert event.message["removed"] == ["My Row"]
        assert event.message["dry_run"] is False

    def test_a_mid_loop_pms_failure_still_audits_the_partial_removal_with_a_redacted_error(self, sessions):
        _add_user(sessions, slug="ann", account_id=100)
        _add_user(sessions, slug="bob", account_id=200)
        with sessions() as session:
            session.add(Collection(slug="comedy", name="Comedy", name_template="My Row"))
            session.commit()
        ann_c = _collection("My Row" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]

        def find(sec, label):
            if label == "shortlist_ann":
                return [ann_c]
            if label == "shortlist_bob":
                raise RuntimeError("PMS error at http://pms/x?X-Plex-Token=SEKRETVALUE")
            return []

        plex.find_owned_collections.side_effect = find
        state = _state(sessions, plex)

        removed, error = asyncio.run(
            rec.run_reconcile(state, slug="comedy", build="per_person", dry_run=False, scope="row.delete")
        )

        assert removed == ["My Row"]  # ann's removal survives bob's failure
        assert error is not None
        assert "SEKRETVALUE" not in error
        with sessions() as session:
            event = session.query(Event).filter_by(scope="row.delete").one()
        assert "SEKRETVALUE" not in str(event.message)
        assert event.message["removed"] == ["My Row"]


class TestRunPosterResetAudit:
    def test_a_successful_reset_is_audited(self, sessions):
        with sessions() as session:
            session.add(Collection(slug="movienight", name="Movie Night", name_template=""))
            session.commit()
        collection = _collection("Movie Night")
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            [collection] if label == "shortlist__shared_movienight" else []
        )
        state = _state(sessions, plex)

        reset, error = asyncio.run(
            rec.run_poster_reset(state, slug="movienight", build="shared", scope="row.reset_poster")
        )

        assert reset == ["Movies"]
        assert error is None
        with sessions() as session:
            event = session.query(Event).filter_by(scope="row.reset_poster").one()
        assert event.message["poster_reset"] == ["Movies"]
        assert event.message["dry_run"] is False


class TestRunRowRenameFromPlexAudit:
    def test_a_successful_rename_is_audited_with_its_entries(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        collection = _collection("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        state = _state(sessions, plex)

        entries, error = asyncio.run(
            rec.run_row_rename_from_plex(
                state, slug="comedy", new_template="New Name", old_template="Old Name", scope="row.rename"
            )
        )

        assert error is None
        assert len(entries) == 1
        assert entries[0]["old"] == "Old Name"
        assert entries[0]["new"] == "New Name"
        with sessions() as session:
            event = session.query(Event).filter_by(scope="row.rename").one()
        assert event.message["new_template"] == "New Name"
        assert len(event.message["renames"]) == 1

    def test_a_per_user_failure_is_joined_into_the_audited_error_and_redacted(self, sessions):
        _add_user(sessions, slug="bob", account_id=200)
        collection = _collection("Old Name" + row_marker(200))
        collection.editTitle.side_effect = RuntimeError("boom X-Plex-Token=SEKRETVALUE")
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.return_value = [collection]
        state = _state(sessions, plex)

        entries, error = asyncio.run(
            rec.run_row_rename_from_plex(
                state, slug="comedy", new_template="New Name", old_template="Old Name", scope="row.rename"
            )
        )

        assert entries == []
        assert error is not None
        assert "bob" in error
        assert "SEKRETVALUE" not in error
        with sessions() as session:
            event = session.query(Event).filter_by(scope="row.rename").one()
        assert "SEKRETVALUE" not in str(event.message)
