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
from tests.db_helpers import disposing_engine


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    with disposing_engine(make_engine(tmp_path)) as engine:
        factory = make_session_factory(engine)
        yield factory


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

    def test_a_rating_key_another_row_also_claims_for_that_person_is_dropped(self, sessions):
        """Ambiguous means unusable, exactly as `pipeline.identity_map` rules for a run. A leftover of
        this row that a same-titled row in that library adopted is recorded under BOTH rows — and this
        key is a delete handle, so trusting it removed the other row's live collection (issue #121)."""
        with sessions() as session:
            session.add_all(
                [
                    Delivery(collection_slug="friday", user_slug="sarah", library_key="1", rating_key=11),
                    Delivery(collection_slug="friday", user_slug="sarah", library_key="2", rating_key=22),
                    Delivery(collection_slug="friday_tv", user_slug="sarah", library_key="2", rating_key=22),
                    Delivery(collection_slug="friday_tv", user_slug="mike", library_key="2", rating_key=11),
                ]
            )
            session.commit()
            assert rec._ledger_keys(session, "friday") == {"sarah": {11}}

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

        dry_run = rec.reconcile_row_removal(
            _state(sessions, plex), slug="movienight", build="shared", dry_run=False, removed=removed
        )

        assert dry_run is False
        assert removed == ["Movie Night"]
        plex.delete_owned_collection.assert_called_once_with(collection, LABEL_PREFIX)
        label_used = plex.find_owned_collections.call_args.args[1]
        assert label_used == f"{SHARED_LABEL_PREFIX}movienight" == "shortlist__shared_movienight"

    @pytest.mark.parametrize(("dry_run", "left"), [(False, {"sarah"}), (True, {"sarah", "shared_movienight"})])
    def test_shared_build_forgets_its_own_ledger_rows_after_a_real_removal(self, sessions, dry_run: bool, left: set):
        """Deleting a shared row kept `shared_<slug>`'s ledger entry for a collection that was gone (seen live,
        2026-09-27), so a row reusing the slug would be handed a dead ratingKey. A per-person entry under the
        same slug is not the shared row's to forget."""
        with sessions() as session:
            for user_slug, key in (("shared_movienight", 900), ("sarah", 901)):
                session.add(
                    Delivery(
                        collection_slug="movienight", user_slug=user_slug, library_key="1", rating_key=key, title="x"
                    )
                )
            session.commit()
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            [_collection("Movie Night")] if label == "shortlist__shared_movienight" else []
        )

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="movienight", build="shared", dry_run=dry_run, removed=[]
        )

        with sessions() as session:
            assert {d.user_slug for d in session.query(Delivery).filter_by(collection_slug="movienight")} == left

    def test_a_shared_row_narrowed_out_of_a_library_forgets_only_that_library(self, sessions):
        with sessions() as session:
            for library_key, key in (("1", 900), ("2", 902)):
                session.add(
                    Delivery(
                        collection_slug="movienight",
                        user_slug="shared_movienight",
                        library_key=library_key,
                        rating_key=key,
                        title="x",
                    )
                )
            session.commit()
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies", "1"), _section("Classics", "2")]
        plex.find_owned_collections.return_value = []

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="movienight", build="shared", dry_run=False, removed=[], in_sections={"2"}
        )

        with sessions() as session:
            assert [d.library_key for d in session.query(Delivery).filter_by(collection_slug="movienight")] == ["1"]

    @pytest.mark.parametrize(("dry_run", "left"), [(False, ["2"]), (True, ["1", "2"])])
    def test_a_shared_walk_that_fails_partway_has_already_forgotten_the_libraries_it_finished(
        self, sessions, dry_run: bool, left: list[str]
    ):
        """The shared twin of the per-person case below: forgetting only after every library meant a failure in
        the second kept the first library's entry for a collection already deleted, until a retry succeeded —
        and Plex reuses ratingKeys. A dry run deleted nothing, so it forgets nothing either."""
        with sessions() as session:
            for library_key, key in (("1", 900), ("2", 902)):
                session.add(
                    Delivery(
                        collection_slug="movienight",
                        user_slug="shared_movienight",
                        library_key=library_key,
                        rating_key=key,
                        title="x",
                    )
                )
            session.commit()
        movies = _collection("Movie Night")

        def owned(section, label):
            if section.key == "2":
                raise RuntimeError("PMS timed out")
            return [movies]

        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies", "1"), _section("TV", "2")]
        plex.find_owned_collections.side_effect = owned
        removed: list[str] = []

        with pytest.raises(RuntimeError):
            rec.reconcile_row_removal(
                _state(sessions, plex), slug="movienight", build="shared", dry_run=dry_run, removed=removed
            )

        with sessions() as session:
            keys = sorted(d.library_key for d in session.query(Delivery).filter_by(collection_slug="movienight"))
        assert keys == left
        assert removed == ["Movie Night"], "the library that finished is still reported for the audit"

    def test_a_walk_that_fails_partway_has_already_forgotten_the_people_it_finished(self, sessions):
        """The ledger records collections that EXIST. Forgetting only after the whole walk meant a failure on the
        second person kept the first person's entries for collections already deleted — and the nightly sweep
        of a shared row's per-person copies tries every such key, every night, while Plex reuses ratingKeys."""
        _add_user(sessions, slug="sarah", account_id=100)
        _add_user(sessions, slug="mike", account_id=200)
        with sessions() as session:
            session.add(Collection(slug="friday", name="Friday", media="movie"))
            for user_slug, key in (("sarah", 71), ("mike", 72)):
                session.add(
                    Delivery(collection_slug="friday", user_slug=user_slug, library_key="1", rating_key=key, title="x")
                )
            session.commit()
        sarahs = _collection("Friday" + row_marker(100))
        sarahs.ratingKey = 71

        def owned(section, label):
            if label == "shortlist_mike":
                raise RuntimeError("PMS timed out")
            return [sarahs] if label == "shortlist_sarah" else []

        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = owned

        with pytest.raises(RuntimeError):
            rec.reconcile_row_removal(
                _state(sessions, plex), slug="friday", build="per_person", dry_run=False, removed=[]
            )

        plex.delete_owned_collection.assert_called_once_with(sarahs, LABEL_PREFIX)
        with sessions() as session:
            left = {d.user_slug for d in session.query(Delivery).filter_by(collection_slug="friday")}
        assert left == {"mike"}, "sarah's copy is gone, so is her entry; mike's walk failed, so his stays"

    def test_shared_build_with_only_user_ids_does_nothing(self, sessions):
        """Who SEES a shared row is a share-filter concern, not a per-user Plex removal — an
        audience-shrink cleanup on a shared row must never touch the one collection everyone shares."""
        plex = MagicMock(spec=PlexClient)
        removed: list[str] = []

        dry_run = rec.reconcile_row_removal(
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

        dry_run = rec.reconcile_row_removal(
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

        rec.reconcile_row_removal(
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

        rec.reconcile_row_removal(
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

        rec.reconcile_row_removal(
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

        dry_run = rec.reconcile_row_removal(
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

        dry_run = rec.reconcile_row_removal(
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

        rec.reconcile_row_removal(
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

        rec.reconcile_row_removal(
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
        assert events == [{"done": True, "total": 0, "dry_run": False}]

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
        assert events == [{"done": True, "total": 0, "dry_run": False}]

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
        assert events[-1] == {"done": True, "total": 1, "dry_run": False}

    def test_a_top_seed_row_given_a_plain_name_is_renamed_now_from_its_recorded_title(self, sessions):
        """A `{top_seed}` title renders to nothing without picks, so the screen matched nothing and said
        "renamed 0". The ledger records what each person's copy was last delivered as — that is the old title.
        A seeded collection the ledger does not name is left alone: title alone is all that tells a person's
        rows apart, and guessing would rename a different row."""
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(
                Delivery(
                    collection_slug="comedy",
                    user_slug="sarah",
                    library_key="1",
                    rating_key=771,
                    title="Because you watched Dune",
                )
            )
            session.commit()
        this_row = _collection("Because you watched Dune" + row_marker(100))
        this_row.ratingKey = 771
        another_row = _collection("Because you watched Heat" + row_marker(100))
        another_row.ratingKey = 772
        # Wears the recorded title but is not the collection the ledger names: a static rename writes no ledger
        # entry, so a recorded title can be worn by something else by now.
        same_title = _collection("Because you watched Dune" + row_marker(100))
        same_title.ratingKey = 773
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            [same_title, this_row, another_row] if label == "shortlist_sarah" else []
        )

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="comedy",
                new_template="Comedy Picks",
                old_template="Because you watched {top_seed}",
            )
        )

        this_row.editTitle.assert_called_once_with("Comedy Picks" + row_marker(100))
        another_row.editTitle.assert_not_called()
        same_title.editTitle.assert_not_called()
        assert {
            "user": "sarah",
            "display_name": "sarah",
            "old": "Because you watched Dune",
            "new": "Comedy Picks",
            "libraries": ["Movies"],
        } in events
        assert events[-1] == {"done": True, "total": 1, "dry_run": False}

    @pytest.mark.parametrize(
        ("old_template", "worn"),
        [
            pytest.param("Old Name", "Old Name", id="plain-to-top-seed"),
            pytest.param("Because you watched {top_seed}", "Because you watched Dune", id="top-seed-to-top-seed"),
        ],
    )
    def test_a_row_given_a_top_seed_name_is_reported_as_taking_it_at_the_next_run(
        self, sessions, old_template: str, worn: str
    ):
        """Its new title depends on picks only a run has, so nothing is renamed now — but each copy is
        reported as taking the name at the next run, instead of "renamed 0" as if nothing needed doing."""
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(
                Delivery(collection_slug="comedy", user_slug="sarah", library_key="1", rating_key=771, title=worn)
            )
            session.commit()
        collection = _collection(worn + row_marker(100))
        collection.ratingKey = 771
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="comedy",
                new_template="More like {top_seed}",
                old_template=old_template,
            )
        )

        collection.editTitle.assert_not_called()
        assert events == [
            {
                "user": "sarah",
                "display_name": "sarah",
                "old": worn,
                "new": "More like {top_seed}",
                "libraries": ["Movies"],
                "next_run": True,
            },
            {"done": True, "total": 0, "dry_run": False},
        ]

    CONFLICT = "(409) conflict; http://pms:32400/library/sections/1/all?id=771&title.value=X&type=18"

    def _refusing(self, title: str, *, then=None) -> MagicMock:
        from plexapi.exceptions import BadRequest

        collection = _collection(title)
        collection.ratingKey = 771
        collection.editTitle.side_effect = [BadRequest(self.CONFLICT), then]
        collection.items.return_value = [MagicMock(ratingKey=5)]
        return collection

    def test_a_name_a_deleted_collection_left_behind_is_freed_and_the_rename_goes_through(self, sessions):
        """The same refusal delivery handles (tests/fixtures/pms_collection_title_tags.json): renaming a row
        back to a name it once had used to fail here with a raw 409 until the next run."""
        _add_user(sessions, slug="sarah", account_id=100)
        collection = self._refusing("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        plex.collections_titled.return_value = []
        plex.create_collection.return_value = MagicMock(ratingKey=9)

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template="Old Name"
            )
        )

        assert collection.editTitle.call_args_list[-1].args == ("New Name" + row_marker(100),)
        assert plex.create_collection.call_args.args[2] == collection.items.return_value[:1]
        assert not any(e.get("error") for e in events), events
        assert events[-1] == {"done": True, "total": 1, "dry_run": False}

    def test_a_name_their_row_in_another_library_has_is_left_for_the_next_run_and_says_so(self, sessions):
        """Only a new collection can share that name, and a rename has no titles to build one from."""
        _add_user(sessions, slug="sarah", account_id=100)
        collection = self._refusing("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies", key="1")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        twin = MagicMock(ratingKey=42, title="New Name" + row_marker(100), librarySectionID="2")
        plex.collections_titled.return_value = [twin]

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template="Old Name"
            )
        )

        (pending,) = [e for e in events if e.get("user")]
        assert pending["next_run"] is True and pending["new"] == "New Name" and "error" not in pending
        plex.create_collection.assert_not_called()
        assert events[-1] == {"done": True, "total": 0, "dry_run": False}

    def test_a_name_something_in_that_library_really_has_is_reported_and_the_others_still_rename(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        _add_user(sessions, slug="mike", account_id=200)
        sarahs = self._refusing("Old Name" + row_marker(100))
        mikes = _collection("Old Name" + row_marker(200))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies", key="1")]
        plex.find_owned_collections.side_effect = lambda sec, label: {
            "shortlist_sarah": [sarahs],
            "shortlist_mike": [mikes],
        }.get(label, [])
        plex.collections_titled.side_effect = lambda title: (
            [MagicMock(ratingKey=42, title=title, librarySectionID="1")] if title.endswith(row_marker(100)) else []
        )

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template="Old Name"
            )
        )

        (refused,) = [e for e in events if e.get("error")]
        assert refused["user"] == "sarah" and "already has" in refused["error"] and "(409)" not in refused["error"]
        assert refused["display_name"] == "sarah" and not refused["error"].startswith("sarah")
        mikes.editTitle.assert_called_once_with("New Name" + row_marker(200))
        assert events[-1] == {"done": True, "total": 1, "dry_run": False}

    def test_a_name_that_could_not_be_freed_is_not_blamed_on_something_holding_it(self, sessions):
        """Review 2026-09-14: every KEPT said "something else there already has that name", including a
        failed label write while freeing it, which sent the owner looking for a collection that did not exist."""
        from plexapi.exceptions import BadRequest

        _add_user(sessions, slug="sarah", account_id=100)
        collection = self._refusing("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        plex.collections_titled.return_value = []
        plex.create_collection.return_value = MagicMock(ratingKey=9)
        plex.stored_label.side_effect = BadRequest("(500) internal_server_error; http://pms/x")

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template="Old Name"
            )
        )

        (refused,) = [e for e in events if e.get("error")]
        assert "already has" not in refused["error"] and "next run" in refused["error"]

    def test_a_name_is_not_freed_while_a_run_or_writer_job_is_writing(self, sessions, monkeypatch):
        from shortlist.server.services import jobs

        monkeypatch.setattr(jobs, "plex_writer_busy", lambda state: True)
        _add_user(sessions, slug="sarah", account_id=100)
        collection = self._refusing("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        plex.collections_titled.return_value = []

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="New Name", old_template="Old Name"
            )
        )

        plex.create_collection.assert_not_called()
        (pending,) = [e for e in events if e.get("user")]
        assert pending["next_run"] is True

    def test_a_rename_that_already_holds_the_writer_lock_still_frees_the_name(self, sessions, monkeypatch):
        """The roster sync renames after a nickname change while it holds the writer lock itself: seeing that
        lock as "busy" postponed every such rename to the next run (review 2026-09-14)."""
        from shortlist.server.services import jobs

        monkeypatch.setattr(jobs, "plex_writer_busy", lambda state: True)
        _add_user(sessions, slug="sarah", account_id=100)
        collection = self._refusing("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        plex.collections_titled.return_value = []
        plex.create_collection.return_value = MagicMock(ratingKey=9)

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="comedy",
                new_template="New Name",
                old_template="Old Name",
                holds_writer_lock=True,
            )
        )

        plex.create_collection.assert_called_once()
        assert events[-1] == {"done": True, "total": 1, "dry_run": False}

    def test_a_shared_rows_refused_rename_is_said_plainly(self, sessions):
        collection = self._refusing("Old Shared Name" + row_marker(0))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies", key="1")]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            [collection] if label.startswith("shortlist__shared_") else []
        )
        plex.collections_titled.side_effect = lambda title: [MagicMock(ratingKey=42, title=title, librarySectionID="1")]

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="popular",
                new_template="New Shared Name",
                old_template="Old Shared Name",
                build="shared",
            )
        )

        (refused,) = [e for e in events if e.get("error")]
        assert "already has that name" in refused["error"] and "(409)" not in refused["error"]
        assert events[-1] == {"done": True, "total": 0, "dry_run": False}

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

    def test_shared_build_renames_by_label_alone_keeping_the_shared_marker(self, sessions):
        """A shared row needs no old title (its label is its own), but it does carry a marker: delivery
        writes `row_marker(0)` on it and finds it again only by the marked title, or by that marker. A
        rename that dropped the marker left the next run unable to find it, so it built a second one."""
        collection = _collection("Old Shared Name" + row_marker(0))
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

        collection.editTitle.assert_called_once_with("New Shared Name" + row_marker(0))
        assert events[-1] == {"done": True, "total": 1, "dry_run": False}

    # ---- seasonal names (discussion #124) ----------------------------------------------------------

    def _one_collection(self, sessions, title, label="shortlist_sarah"):
        _add_user(sessions, slug="sarah", account_id=100)
        collection = _collection(title)
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, found_by: [collection] if found_by == label else []
        return collection, plex

    def test_a_seasonal_name_is_renamed_in_the_season_the_collection_wears(self, sessions):
        """Without a season the templates render nothing, so the rename matched nothing and reported success
        while Plex kept the old name until the next run."""
        collection, plex = self._one_collection(sessions, "🎄 Christmas picks" + row_marker(100))

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="seasonal",
                new_template="{season} for {user}",
                old_template="{season_emoji} {season} picks",
            )
        )

        collection.editTitle.assert_called_once_with("Christmas for sarah" + row_marker(100))
        assert events[-1] == {"done": True, "total": 1, "dry_run": False}

    def test_out_of_season_it_keeps_the_season_it_last_wore(self, sessions):
        collection, plex = self._one_collection(sessions, "🎃 Halloween picks" + row_marker(100))

        list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="seasonal",
                new_template="{season} for {user}",
                old_template="{season_emoji} {season} picks",
            )
        )

        collection.editTitle.assert_called_once_with("Halloween for sarah" + row_marker(100))

    def test_a_plain_name_becoming_seasonal_waits_for_the_run_that_knows_the_season(self, sessions):
        """Nothing says which season a plain "Holiday picks" collection should become; guessing one could put
        Valentine's Day on it in December."""
        collection, plex = self._one_collection(sessions, "Holiday picks" + row_marker(100))

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="seasonal", new_template="{season} picks", old_template="Holiday picks"
            )
        )

        collection.editTitle.assert_not_called()
        assert events == [{"done": True, "total": 0, "dry_run": False}]

    def test_a_nickname_change_renames_a_seasonal_row_named_after_them(self, sessions):
        collection, plex = self._one_collection(sessions, "Christmas for Sal" + row_marker(100))

        list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="seasonal",
                new_template="{season} for {user}",
                old_template="{season} for {user}",
                old_display_names={"sarah": "Sal"},
            )
        )

        collection.editTitle.assert_called_once_with("Christmas for sarah" + row_marker(100))

    def test_a_shared_seasonal_row_is_renamed_in_the_season_it_wears(self, sessions):
        collection, plex = self._one_collection(
            sessions, "🎄 Christmas picks" + row_marker(0), label="shortlist__shared_seasonal"
        )

        list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="seasonal",
                new_template="{season} favourites",
                old_template="{season_emoji} {season} picks",
                build="shared",
            )
        )

        collection.editTitle.assert_called_once_with("Christmas favourites" + row_marker(0))

    def test_a_shared_seasonal_row_given_a_top_seed_name_is_left_alone(self, sessions):
        """A shared row has no seed to be named after, so nothing can name it — and the pair's missing new title
        must not reach the shared walk as a title."""
        collection, plex = self._one_collection(
            sessions, "🎄 Christmas picks" + row_marker(0), label="shortlist__shared_seasonal"
        )

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex),
                slug="seasonal",
                new_template="Because you watched {top_seed}",
                old_template="{season_emoji} {season} picks",
                build="shared",
            )
        )

        collection.editTitle.assert_not_called()
        assert events == [{"done": True, "total": 0, "dry_run": False}]

    def test_a_shared_rename_leaves_a_helper_and_an_unmarked_copy_alone(self, sessions):
        """Everything under a shared row's label used to be renamed, in listing order: a helper a killed run left
        behind, or the unmarked copy an older rename left, would take the row's new name first, the real row
        would be refused, and the next run would adopt the copy."""
        from shortlist.engine.delivery import FREED_NAME_PREFIX

        helper = _collection(FREED_NAME_PREFIX + "0123456789ab" + row_marker(0))
        unmarked_copy = _collection("Old Shared Name")
        the_row = _collection("Old Shared Name" + row_marker(0))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            [helper, unmarked_copy, the_row] if label == "shortlist__shared_movienight" else []
        )

        list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="movienight", new_template="New Shared Name", build="shared"
            )
        )

        helper.editTitle.assert_not_called()
        unmarked_copy.editTitle.assert_not_called()
        the_row.editTitle.assert_called_once_with("New Shared Name" + row_marker(0))

    def test_a_lone_unmarked_shared_collection_is_still_renamed_and_given_its_marker(self, sessions):
        """The copy an older rename stripped: with nothing marked beside it, it IS the row, and taking the marker
        back is what lets the next run find it again."""
        collection, plex = self._one_collection(sessions, "Old Shared Name", label="shortlist__shared_movienight")

        list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="movienight", new_template="New Shared Name", build="shared"
            )
        )

        collection.editTitle.assert_called_once_with("New Shared Name" + row_marker(0))

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
        assert events[-1] == {"done": True, "total": 1, "dry_run": True}  # still counted as a would-be rename

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

    def test_a_top_seed_name_that_did_not_change_reports_nothing(self, sessions):
        """A roster sync re-runs the rename for every person when the global template or a nickname changes. A
        person whose own `{top_seed}` name renders the same as before is not taking a new name at the next run."""
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(
                Delivery(collection_slug="comedy", user_slug="sarah", library_key="1", rating_key=771, title="x Dune")
            )
            session.commit()
        collection = _collection("x Dune" + row_marker(100))
        collection.ratingKey = 771
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []

        events = list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="comedy", new_template="x {top_seed}", old_template="x {top_seed}"
            )
        )

        assert events == [{"done": True, "total": 0, "dry_run": False}]

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
        # Nothing renamed now; reported as taking the name at the next run instead of a bare "renamed 0"
        # (backlog 2026-09-27) — see test_a_row_given_a_top_seed_name_is_reported_as_taking_it_at_the_next_run.
        assert events[-1] == {"done": True, "total": 0, "dry_run": False}
        assert all(e.get("next_run") for e in events[:-1])

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
        assert events[-1] == {"done": True, "total": 1, "dry_run": False}


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

    @pytest.mark.parametrize("effective_dry_run", [True, False])
    def test_the_audit_records_the_effective_dry_run(self, sessions, effective_dry_run):
        """Rules 8 and 10: under a forced preview the audit must not read as renames that happened."""
        _add_user(sessions, slug="sarah", account_id=100)
        collection = _collection("Old Name" + row_marker(100))
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [_section("Movies")]
        plex.find_owned_collections.side_effect = lambda sec, label: [collection] if label == "shortlist_sarah" else []
        state = _state(sessions, plex, dry_run=effective_dry_run)

        asyncio.run(
            rec.run_row_rename_from_plex(
                state, slug="comedy", new_template="New Name", old_template="Old Name", scope="row.rename"
            )
        )

        with sessions() as session:
            event = session.query(Event).filter_by(scope="row.rename").one()
        assert event.message["dry_run"] is effective_dry_run

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


class TestASeasonalSiblingClaimsEverySeasonsTitle:
    """A seasonal row wears whichever season it was last built for, so it claims the title of EVERY catalogue
    season (#124) — and the reconciles have to hand the claim the catalogue that says what those are (#137).
    Without it the claim is empty, and removing or renaming a plain row can take the seasonal row's collection."""

    MARK = row_marker(100)

    def _rows(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(Collection(slug="plain", name="Halloween picks", media="movie"))
            # Follows Christmas alone now, but its collection may still wear the Halloween it was built for.
            session.add(Collection(slug="seasonal", name="{season} picks", media="movie", seasons=["christmas"]))
            session.commit()

    def _plex(self, worn: MagicMock) -> MagicMock:
        movies = _section("Movies", key="1")
        movies.type = "movie"
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies]
        plex.find_owned_collections.side_effect = lambda sec, label: [worn] if label == "shortlist_sarah" else []
        return plex

    def test_the_claim_covers_every_catalogue_season(self, sessions):
        self._rows(sessions)
        with sessions() as session:
            other_rows = rec._other_rows(session, None, "plain")
            sarah = next(u for u in rec._users_data(session) if u["slug"] == "sarah")
        ctx = SimpleNamespace(plex=self._plex(_collection("anything")))

        claimed = rec._claimed_titles(ctx, sarah, other_rows)

        assert {("1", "Valentine's Day picks"), ("1", "Halloween picks"), ("1", "Christmas picks")} <= claimed

    def test_removing_a_plain_row_leaves_the_seasonal_rows_collection_alone(self, sessions):
        self._rows(sessions)
        plex = self._plex(_collection("Halloween picks" + self.MARK))

        rec.reconcile_row_removal(_state(sessions, plex), slug="plain", build="per_person", dry_run=False, removed=[])

        plex.delete_owned_collection.assert_not_called()


class TestAnExploreSiblingClaimsEachPersonsOwnThemeTitle:
    """An explore row (#138) titles each person's collection from THAT person's current theme, not the row's own.
    Claimed from the row's base theme, the title a person's collection actually wears is unclaimed, and removing
    a plain row that shares it takes the explore row's collection (the #121 class)."""

    MARK = row_marker(100)

    def _rows(self, sessions):
        from datetime import datetime

        from shortlist.server.db.models import Theme, ThemeHistory

        user_id = _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            starter = Theme(slug="starter", name="Starter", emoji="", media=["movie"], genres=["Drama"])
            scary = Theme(slug="scary", name="Scary nights", emoji="", media=["movie"], genres=["Horror"])
            session.add_all([starter, scary])
            session.flush()
            session.add(Collection(slug="plain", name="Scary nights", media="movie"))
            ai = Collection(
                slug="ai", name="{theme}", media="movie", theme_id=starter.id, theme_mode="explore", enabled=True
            )
            session.add(ai)
            session.flush()
            session.add(
                ThemeHistory(
                    collection_id=ai.id,
                    user_id=user_id,
                    theme_id=scary.id,
                    theme_name="Scary nights",
                    state="current",
                    started_at=datetime(2026, 10, 1),
                )
            )
            session.commit()

    def _plex(self, worn: MagicMock) -> MagicMock:
        movies = _section("Movies", key="1")
        movies.type = "movie"
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies]
        plex.find_owned_collections.side_effect = lambda sec, label: [worn] if label == "shortlist_sarah" else []
        return plex

    def test_the_claim_is_the_title_the_persons_own_theme_gives(self, sessions):
        self._rows(sessions)
        with sessions() as session:
            other_rows = rec._other_rows(session, None, "plain")
            sarah = next(u for u in rec._users_data(session) if u["slug"] == "sarah")
        ctx = SimpleNamespace(plex=self._plex(_collection("anything")))

        claimed = rec._claimed_titles(ctx, sarah, other_rows)

        assert ("1", "Scary nights") in claimed
        assert ("1", "Starter") not in claimed, "the row's own theme is not what she wears"

    def test_removing_a_plain_row_leaves_the_explore_rows_collection_alone(self, sessions):
        self._rows(sessions)
        plex = self._plex(_collection("Scary nights" + self.MARK))

        rec.reconcile_row_removal(_state(sessions, plex), slug="plain", build="per_person", dry_run=False, removed=[])

        plex.delete_owned_collection.assert_not_called()


class TestATitleAnotherRowBuildsUnderIsNeverThisRows:
    """Issue #121: a Movies-only row and a TV-only row of one person may share a title. Every
    on-demand reconcile below used to match that title in EVERY library, so deleting, switching off,
    renaming or resetting the Movies row did the same to the TV row's collection."""

    MARK = row_marker(100)

    def _plex(self):
        movies, shows = _section("Movies", key="1"), _section("TV Shows", key="2")
        movies.type, shows.type = "movie", "show"
        movies_c, shows_c = _collection("Friday" + self.MARK), _collection("Friday" + self.MARK)
        movies_c.ratingKey, shows_c.ratingKey = 11, 22
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.side_effect = lambda sec, label: (
            ([movies_c] if sec is movies else [shows_c]) if label == "shortlist_sarah" else []
        )
        return plex, movies_c, shows_c

    def _movies_row(self, sessions):
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(Collection(slug="friday", name="Friday", media="movie"))
            session.add(Collection(slug="friday_tv", name="Friday", media="show"))
            session.commit()

    def test_removing_a_row_leaves_another_rows_same_title_in_a_different_library(self, sessions):
        self._movies_row(sessions)
        plex, movies_c, _shows_c = self._plex()
        removed: list[str] = []

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="friday", build="per_person", dry_run=False, removed=removed
        )

        plex.delete_owned_collection.assert_called_once_with(movies_c, LABEL_PREFIX)

    def test_a_deleted_row_still_leaves_the_other_rows_collection_alone(self, sessions):
        """The DELETE path runs after the row is gone — what protects the TV row is the TV row itself,
        which is still in the database."""
        self._movies_row(sessions)
        with sessions() as session:
            session.query(Collection).filter_by(slug="friday").delete()
            session.commit()
        plex, movies_c, _shows_c = self._plex()
        removed: list[str] = []

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="friday", build="per_person", dry_run=False, removed=removed, template="Friday"
        )

        plex.delete_owned_collection.assert_called_once_with(movies_c, LABEL_PREFIX)

    def test_a_ledger_key_the_other_row_now_holds_does_not_delete_its_collection(self, sessions):
        """The review's reproduction. Row A once built in TV too; its TV copy and ledger entry survived a
        narrowing Plex was down for. Row B (TV "Friday") then adopted that collection by title and
        recorded the same ratingKey. Deleting A must leave B's live collection alone."""
        self._movies_row(sessions)
        with sessions() as session:
            session.add_all(
                [
                    Delivery(collection_slug="friday", user_slug="sarah", library_key="1", rating_key=11),
                    Delivery(collection_slug="friday", user_slug="sarah", library_key="2", rating_key=22),
                    Delivery(collection_slug="friday_tv", user_slug="sarah", library_key="2", rating_key=22),
                ]
            )
            session.commit()
        plex, movies_c, _shows_c = self._plex()
        removed: list[str] = []

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="friday", build="per_person", dry_run=False, removed=removed
        )

        plex.delete_owned_collection.assert_called_once_with(movies_c, LABEL_PREFIX)

    def test_a_leftover_copy_no_other_row_claims_is_still_removed(self, sessions):
        """Every library is scanned so a copy left behind in a library the row stopped using goes too —
        it would otherwise sit on that person's Home. Only another row's title is off limits."""
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(Collection(slug="friday", name="Friday", media="movie"))
            session.commit()
        plex, movies_c, shows_c = self._plex()
        removed: list[str] = []

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="friday", build="per_person", dry_run=False, removed=removed
        )

        assert [c.args[0] for c in plex.delete_owned_collection.call_args_list] == [movies_c, shows_c]

    def test_a_row_whose_audience_leaves_this_person_out_claims_nothing_for_them(self, sessions):
        from shortlist.server.db.models import CollectionAudience

        self._movies_row(sessions)
        mike = _add_user(sessions, slug="mike", account_id=200)
        with sessions() as session:
            tv = session.query(Collection).filter_by(slug="friday_tv").one()
            tv.audience = "subset"
            session.add(CollectionAudience(collection_id=tv.id, user_id=mike))
            session.commit()
        plex, _movies_c, _shows_c = self._plex()
        removed: list[str] = []

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="friday", build="per_person", dry_run=False, removed=removed
        )

        assert plex.delete_owned_collection.call_count == 2, "sarah is not in friday_tv's audience"

    def test_what_another_top_seed_row_was_delivered_as_is_claimed_in_that_library(self, sessions):
        """Two `{top_seed}` rows — Movies and TV — both seeded by one watch wear the same title, which no
        template can predict. The ledger recorded where each was delivered as what; deleting the Movies
        row must not match the TV row's collection by that title."""
        _add_user(sessions, slug="sarah", account_id=100)
        seeded = "Because you watched Dune"
        with sessions() as session:
            session.add(Collection(slug="friday", name="Because you watched {top_seed}", media="movie"))
            session.add(Collection(slug="friday_tv", name="Because you watched {top_seed}", media="show"))
            # No ratingKey match for the TV copy, so only a title could select it.
            session.add(
                Delivery(collection_slug="friday_tv", user_slug="sarah", library_key="2", rating_key=99, title=seeded)
            )
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            user = session.query(User).filter_by(slug="sarah").one()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user.id,
                    status="ok",
                    breakdown=[{"row_slug": "friday", "row_title": seeded, "library_key": "1"}],
                )
            )
            session.commit()
        plex, movies_c, shows_c = self._plex()
        movies_c.title = shows_c.title = seeded + self.MARK
        removed: list[str] = []

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="friday", build="per_person", dry_run=False, removed=removed
        )

        plex.delete_owned_collection.assert_called_once_with(movies_c, LABEL_PREFIX)

    def test_what_another_seasonal_row_was_delivered_as_is_claimed_in_that_library(self, sessions):
        """The seasonal twin of the `{top_seed}` case above: a seasonal title cannot be predicted without the
        season the collection last wore, so it is claimed from the ledger in the same way."""
        _add_user(sessions, slug="sarah", account_id=100)
        worn = "Christmas picks"
        with sessions() as session:
            session.add(Collection(slug="friday", name="{season} picks", media="movie", seasons=["christmas"]))
            session.add(Collection(slug="friday_tv", name="{season} picks", media="show", seasons=["christmas"]))
            session.add(
                Delivery(collection_slug="friday_tv", user_slug="sarah", library_key="2", rating_key=99, title=worn)
            )
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            user = session.query(User).filter_by(slug="sarah").one()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user.id,
                    status="ok",
                    breakdown=[{"row_slug": "friday", "row_title": worn, "library_key": "1"}],
                )
            )
            session.commit()
        plex, movies_c, shows_c = self._plex()
        movies_c.title = shows_c.title = worn + self.MARK
        removed: list[str] = []

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="friday", build="per_person", dry_run=False, removed=removed
        )

        plex.delete_owned_collection.assert_called_once_with(movies_c, LABEL_PREFIX)

    def test_a_static_rows_ledger_title_claims_nothing_since_a_rename_leaves_it_stale(self, sessions):
        """A rename edits Plex and writes no ledger entry, so a static row's recorded title can be one it
        no longer wears — and this row may have just been renamed onto it."""
        self._movies_row(sessions)
        with sessions() as session:
            other = session.query(Collection).filter_by(slug="friday_tv").one()
            other.name, other.media = "Sunday", "movie"
            session.add(
                Delivery(collection_slug="friday_tv", user_slug="sarah", library_key="1", rating_key=99, title="Friday")
            )
            session.commit()
        plex, movies_c, _shows_c = self._plex()
        reset: list[str] = []

        rec._reconcile_poster_reset(_state(sessions, plex), slug="friday", build="per_person", reset=reset)

        assert movies_c in [c.args[0] for c in plex.reset_poster.call_args_list]

    def test_a_switched_off_row_claims_nothing(self, sessions):
        """A disabled row builds nothing, and its own collections are on their way out anyway."""
        self._movies_row(sessions)
        with sessions() as session:
            session.query(Collection).filter_by(slug="friday_tv").one().enabled = False
            session.commit()
        plex, _movies_c, _shows_c = self._plex()
        removed: list[str] = []

        rec.reconcile_row_removal(
            _state(sessions, plex), slug="friday", build="per_person", dry_run=False, removed=removed
        )

        assert plex.delete_owned_collection.call_count == 2

    def test_a_narrowing_removes_by_title_in_the_library_the_row_left(self, sessions):
        """`in_sections` names libraries the row NO LONGER builds in, so nothing there is the row's by
        where it builds — the title still identifies it as long as no other row builds that title there."""
        _add_user(sessions, slug="sarah", account_id=100)
        with sessions() as session:
            session.add(Collection(slug="friday", name="Friday", media="movie"))
            session.commit()
        plex, _movies_c, shows_c = self._plex()
        removed: list[str] = []

        rec.reconcile_row_removal(
            _state(sessions, plex),
            slug="friday",
            build="per_person",
            dry_run=False,
            removed=removed,
            in_sections={"2"},
        )

        plex.delete_owned_collection.assert_called_once_with(shows_c, LABEL_PREFIX)

    def test_a_poster_reset_leaves_another_rows_same_title_in_a_different_library(self, sessions):
        self._movies_row(sessions)
        plex, movies_c, _shows_c = self._plex()
        reset: list[str] = []

        rec._reconcile_poster_reset(_state(sessions, plex), slug="friday", build="per_person", reset=reset)

        plex.reset_poster.assert_called_once_with(movies_c)

    def test_a_rename_leaves_another_rows_same_title_in_a_different_library(self, sessions):
        self._movies_row(sessions)
        plex, movies_c, shows_c = self._plex()

        list(
            rec.reconcile_row_rename_iter(
                _state(sessions, plex), slug="friday", new_template="Saturday", old_template="Friday"
            )
        )

        movies_c.editTitle.assert_called_once_with("Saturday" + self.MARK)
        shows_c.editTitle.assert_not_called()


class TestSeasonalTitlesOutsideARun:
    """A seasonal row's collection wears whichever season it was last built for (discussion #124), so a
    title rendered without a run is never its title: like a `{top_seed}` row, it is found by what the
    ledger and the run history recorded."""

    def test_its_template_renders_no_titles_to_match_on(self):
        from types import SimpleNamespace

        from shortlist.server.services import collection_reconcile as reconcile

        ctx = SimpleNamespace(plex=SimpleNamespace(sections=lambda: [SimpleNamespace(title="Movies", key="1")]))
        udata = {
            "slug": "sarah",
            "username": "sarah",
            "nickname": "",
            "plex_account_id": 100,
            "user_type": "shared",
            "prefs": {},
        }
        assert reconcile._rendered_titles(ctx, udata, "{season_emoji} {season} picks", "seasonal") == set()

    def test_two_different_seasonal_names_have_different_title_keys(self):
        from shortlist.server.services.collection_reconcile import title_key

        assert title_key("{season} picks") != title_key("{season} for you")
        assert title_key("{season} picks") != ""

    def test_the_same_seasonal_name_has_the_same_title_key(self):
        from shortlist.server.services.collection_reconcile import title_key

        assert title_key("{season} picks") == title_key("{season} picks")
