"""API contract tests: collections/rows CRUD, seeding, row edits reaching Plex, narrowing a row's
libraries, blocked seeds, clearing deleted rows, and deleting poster images."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shortlist.engine.rows import ROW_ORDERS
from shortlist.server.auth import SESSION_COOKIE
from shortlist.server.db.models import User
from shortlist.server.settings_store import SettingsStore

pytestmark = pytest.mark.integration

# Response KEY SETS, spelled out — see the same note in test_api_users.py. A Pydantic response model
# FILTERS the payload, so every model here sets `extra="allow"`; naming every key is what fails if
# that config is ever stripped, or if a default starts inventing a key the handler never sent.

#: Every key `collections._serialize` renders — `GET`, `POST` and `PATCH /api/collections` alike.
COLLECTION_KEYS = {
    "id",
    "slug",
    "name",
    "last_run_id",
    "build",
    "audience",
    "audience_user_ids",
    "enabled",
    "schedule",
    "size",
    "media",
    "sort_order",
    "name_template",
    "min_watchers",
    "request_tag",
    "candidate_sources",
    "watched_pct",
    "rewatch",
    "unstarted_only",
    "refresh_days",
    "recency",
    "recent_count",
    "max_seeds",
    "cold_start",
    "seed_window",
    "pick_order",
    "placement",
    "placement_friends",
    "pin_top",
    "hub_anchor",
    "library_keys",
    "poster",
}

#: Every key `collections._poster_view` renders, nested under `poster`.
POSTER_KEYS = {"mode", "title", "subtitle", "style", "has_image"}


class TestCollectionsSeed:
    def test_migration_seeds_the_default_picked_row(self, client: TestClient):
        """Upgrade must be behaviour-neutral: exactly one per-person 'picked' row for everyone."""
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            rows = session.query(Collection).all()
            assert len(rows) == 1
            row = rows[0]
            assert (row.slug, row.build, row.audience, row.enabled) == (
                "picked",
                "per_person",
                "everyone",
                True,
            )

    def test_collection_reports_its_last_run(self, client: TestClient):
        """The Rows UI links a row to its last run — last_run_id is the newest run that delivered it."""
        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            run_id = run.id
            session.add(
                PickRow(
                    run_id=run_id,
                    user_id=uid,
                    tmdb_id=9,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    collection_slug="picked",
                    title="X",
                )
            )
            session.commit()

        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        assert picked["last_run_id"] == run_id

    def test_default_rows_serialized_name_is_the_global_template_not_the_stale_column(self, client: TestClient):
        """The Rows UI must show the ACTUAL default title (the global template it delivers), not the
        seeded 'name' column — so the {library_name} default is visible in the editor and list."""
        client.put("/api/settings", json={"values": {"row.name_template": "✨ {library_name} Picked for You"}})
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        assert picked["name"] == "✨ {library_name} Picked for You"

    def test_saving_the_default_row_never_overwrites_its_name_column(self, client: TestClient):
        """The editor sends the serialized name (now the template) back on save; the default row's name
        column must NOT be clobbered by it — it follows Settings, not this PATCH."""
        client.put("/api/settings", json={"values": {"row.name_template": "✨ {library_name} Picked for You"}})
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        r = client.patch(f"/api/collections/{picked['id']}", json={"name": "✨ {library_name} Picked for You"})
        assert r.status_code == 200
        with client.app.state.sessions() as session:
            from shortlist.server.db.models import Collection

            assert session.query(Collection).filter_by(slug="picked").one().name == "✨ Picked for You"

    def test_default_row_never_stores_its_own_name_template(self, client: TestClient):
        """The rename screen sends `name` AND `name_template` — right for every other row, wrong here.

        The default row's title is the global `row.name_template`. The engine already forces this
        column empty when it builds specs, so a stored value never reaches Plex — but the report
        service used to prefer it, so a row carrying one showed a stale name for ever once Settings →
        Defaults moved on. The guard is server-side rather than in the caller: any client sending the
        field would otherwise put the row back into that state.
        """
        client.put("/api/settings", json={"values": {"row.name_template": "✨ {library_name} Picked for You"}})
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")

        r = client.patch(
            f"/api/collections/{picked['id']}",
            json={"name": "✨ {library_name} Handpicked", "name_template": "✨ {library_name} Handpicked"},
        )
        assert r.status_code == 200

        # The global moved; the row's own column did not.
        assert client.get("/api/settings").json()["row.name_template"] == "✨ {library_name} Handpicked"
        with client.app.state.sessions() as session:
            from shortlist.server.db.models import Collection

            assert session.query(Collection).filter_by(slug="picked").one().name_template == ""

        # The rename SCREEN does not stop at the PATCH — it immediately POSTs /rename with the same
        # template. Guarding only the PATCH left the column cleared for exactly one request.
        client.post(
            f"/api/collections/{picked['id']}/rename",
            json={"name_template": "✨ {library_name} Handpicked", "old_template": ""},
        )
        with client.app.state.sessions() as session:
            from shortlist.server.db.models import Collection

            assert session.query(Collection).filter_by(slug="picked").one().name_template == ""

    def test_default_row_never_serves_a_stale_name_template(self, client: TestClient):
        """A database written before the guard still carries a value; the API must not ship it.

        The SPA reads `name_template || name` in three places, so a stale column would show a title
        Plex no longer uses — and the rename screen would send it as `old_template`, match nothing,
        report "renamed 0 collections", and leave the next run to build a second collection beside
        the old one.
        """
        from shortlist.server.db.models import Collection

        client.put("/api/settings", json={"values": {"row.name_template": "✨ {library_name} Picked for You"}})
        with client.app.state.sessions() as session:
            session.query(Collection).filter_by(slug="picked").update({Collection.name_template: "✨ Stale Name"})
            session.commit()

        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        assert picked["name_template"] == ""
        # `name` for this row IS the global template (delivery renders it per library), so the SPA's
        # `name_template || name` now lands on the live value instead of the stale column.
        assert picked["name"] == "✨ {library_name} Picked for You"

    def test_editing_the_default_rows_name_writes_the_global_template_and_reconciles(
        self, client: TestClient, monkeypatch
    ):
        """The default row's editable name IS the global `row.name_template` (a per-collection value
        would beat each user's own `row_name_tpl` override). Editing it writes that setting and renames
        the collections already on Plex in place — the same reconcile a nickname change fires."""
        from shortlist.server.services import collection_reconcile as rec

        calls: list[tuple[str, str, str, str]] = []

        async def fake_rename(state, *, slug, new_template, old_template, scope):
            calls.append((slug, new_template, old_template, scope))
            return [], None

        monkeypatch.setattr(rec, "run_row_rename_from_plex", fake_rename)

        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        before = client.get("/api/settings").json()["row.name_template"]
        r = client.patch(f"/api/collections/{picked['id']}", json={"name": "✨ {library_name} Handpicked"})
        assert r.status_code == 200
        # The edit is surfaced as the row's name (read back from the global template) …
        assert r.json()["name"] == "✨ {library_name} Handpicked"
        # … persisted to the shared setting …
        assert client.get("/api/settings").json()["row.name_template"] == "✨ {library_name} Handpicked"
        # … and reconciled onto Plex for the default slug. The PREVIOUS template goes with it: it is
        # what the collections on the server are titled with, and therefore the only way to tell which
        # of a multi-row user's collections belongs to this row.
        assert calls == [("picked", "✨ {library_name} Handpicked", before, "collection.rename")]

    def test_saving_the_default_row_with_an_unchanged_name_does_no_plex_work(self, client: TestClient, monkeypatch):
        """A save that doesn't move the name (e.g. an enable toggle carrying the current name) must not
        touch Plex — the rename reconcile does real I/O and only fires on a real change."""
        from shortlist.server.services import collection_reconcile as rec

        calls: list = []

        async def fake_rename(state, **kwargs):
            calls.append(kwargs)
            return [], None

        monkeypatch.setattr(rec, "run_row_rename_from_plex", fake_rename)

        client.put("/api/settings", json={"values": {"row.name_template": "✨ {library_name} Picked for You"}})
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        # The editor round-trips the current template as the name — an unchanged value, so no reconcile.
        r = client.patch(
            f"/api/collections/{picked['id']}",
            json={"name": "✨ {library_name} Picked for You", "enabled": True},
        )
        assert r.status_code == 200
        assert calls == [], "an unchanged default name must not reconcile onto Plex"

    def test_default_row_size_and_name_follow_the_global_setting(self, client: TestClient, tmp_path):
        """The wizard/Settings set row.size and row.name_template; the default 'picked' row must
        deliver at those values, not a size frozen into the collection at migration time."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        client.put("/api/settings", json={"values": {"row.size": 10}})
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        picked = next(spec for spec in specs if spec.slug == "picked")
        assert picked.size == 10  # follows the setting, not the collection's seeded 15
        assert picked.name_template == ""  # falls through to the global row name

    def test_per_row_watched_pct_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Rewatch Row", "watched_pct": 0.5})
        assert created.status_code == 201 and created.json()["watched_pct"] == 0.5
        # Out of the 0..1 range is rejected.
        assert client.post("/api/collections", json={"name": "X", "watched_pct": 2.0}).status_code == 422

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "rewatch_row").watched_pct == 0.5

    def test_per_row_cadence_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Fresh Row", "refresh_days": 3})
        assert created.status_code == 201 and created.json()["refresh_days"] == 3
        # Out of range is rejected, at both ends.
        assert client.post("/api/collections", json={"name": "X", "refresh_days": -1}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "refresh_days": 400}).status_code == 422
        # And the global cadence setting is range-checked too.
        assert client.put("/api/settings", json={"values": {"recommendations.refresh_days": 400}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.refresh_days": 30}}).status_code == 200
        # 0 is a CHOICE ("frozen"), not out of range — the one value a bounds check must let through.
        assert client.put("/api/settings", json={"values": {"recommendations.refresh_days": 0}}).status_code == 200

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "fresh_row").refresh_days == 3

    def test_per_row_recency_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "New Row", "recency": 0.8})
        assert created.status_code == 201 and created.json()["recency"] == 0.8
        assert client.post("/api/collections", json={"name": "X", "recency": 1.5}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "recency": -0.1}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.recency": 2.0}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.recency": 0.4}}).status_code == 200

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            specs = builder._build_rows(session, store)
            assert builder._engine_config(session, store).recency == 0.4
        assert next(s for s in specs if s.slug == "new_row").recency == 0.8

    def test_a_row_left_at_the_default_inherits_rather_than_pinning_zero(self, client: TestClient):
        """NULL, not 0.0. A row created before this setting existed — or one the owner never touched
        — must follow the global. Storing 0.0 for "unset" would freeze every existing row at "ignore
        release date" and make raising the global do nothing, which is indistinguishable from the
        feature being broken."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Plain Row"})
        assert created.status_code == 201 and created.json()["recency"] is None

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "plain_row").recency is None

    def test_an_explicit_zero_is_stored_and_not_swallowed_as_unset(self, client: TestClient):
        """The falsy-vs-None trap this codebase has hit before: `body.recency or None` would turn a
        deliberate "ignore release date on THIS row" into "inherit the global", so a Hidden Gems row
        on a modern-leaning server would quietly stop being one."""
        created = client.post("/api/collections", json={"name": "Gems Row", "recency": 0.0})
        assert created.status_code == 201 and created.json()["recency"] == 0.0

    def test_per_row_max_seeds_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Because Row", "max_seeds": 1})
        assert created.status_code == 201 and created.json()["max_seeds"] == 1
        assert client.post("/api/collections", json={"name": "X", "max_seeds": 0}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "max_seeds": 101}).status_code == 422

        # PATCHable, and clearable back to "inherit the engine default". (`name` rides along because
        # CollectionIn requires it; only the fields actually sent are written.)
        cid = created.json()["id"]
        patch = {"name": "Because Row"}
        assert client.patch(f"/api/collections/{cid}", json={**patch, "max_seeds": 5}).json()["max_seeds"] == 5
        assert client.patch(f"/api/collections/{cid}", json={**patch, "max_seeds": None}).json()["max_seeds"] is None
        client.patch(f"/api/collections/{cid}", json={**patch, "max_seeds": 1})

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "because_row").max_seeds == 1
        # A row that never set one keeps None, so the engine falls back to its own budget.
        assert next(s for s in specs if s.slug == "picked").max_seeds is None

    def test_per_row_seed_window_round_trips_and_reaches_the_spec(self, client: TestClient):
        """How many recent watches a row cycles between. Unlike max_seeds it is NOT nullable — there
        is no global to inherit, because whether a row rotates belongs to what that row is."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Cycling Row", "seed_window": 3})
        assert created.status_code == 201 and created.json()["seed_window"] == 3
        assert client.post("/api/collections", json={"name": "X", "seed_window": 0}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "seed_window": 21}).status_code == 422

        cid = created.json()["id"]
        patch = {"name": "Cycling Row"}
        assert client.patch(f"/api/collections/{cid}", json={**patch, "seed_window": 5}).json()["seed_window"] == 5
        client.patch(f"/api/collections/{cid}", json={**patch, "seed_window": 3})

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "cycling_row").seed_window == 3
        # A row that never set one takes their most recent watch — the behaviour before cycling existed.
        assert next(s for s in specs if s.slug == "picked").seed_window == 1

    def test_global_max_seeds_is_bounded_and_defaults_to_the_engines_own(self, client: TestClient):
        """The server-wide seed budget: bounds, round-trip, and a default that matches the engine's.

        The one link NOT asserted here is `store.get(...)` -> `EngineConfig(max_seeds=...)` inside
        `ContextBuilder.build`, which needs a live PMS to reach and is stubbed out in every test that
        goes near it — the same untested seam its three neighbours (watched_pct, refresh_days,
        recent_count) already sit on. The engine half IS covered: test_pipeline's
        `test_two_rows_differing_only_in_max_seeds_do_not_share_seeds` asserts a row with no override
        falls back to `cfg.max_seeds`.
        """
        from shortlist.engine.models import EngineConfig

        # Floored at 5, not 1: this applies to EVERY row on the server, and seeds are shared across
        # the media types a row covers — so a server-wide 1 or 2 would leave every movies-and-TV row
        # with one of its halves unseeded. A deliberately narrow value belongs on the row (1..100).
        assert client.put("/api/settings", json={"values": {"recommendations.max_seeds": 1}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.max_seeds": 101}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.max_seeds": 12}}).status_code == 200
        assert client.get("/api/settings").json()["recommendations.max_seeds"] == 12

        # A fresh install must behave exactly as it did before this setting existed.
        from shortlist.server.settings_store import DEFAULTS

        assert DEFAULTS["recommendations.max_seeds"] == EngineConfig().max_seeds

    def test_cold_start_settings_are_bounded_and_default_to_todays_behaviour(self, client: TestClient):
        """The global half of issue #66. Defaults must match the engine's, so upgrading an existing
        install changes nothing until the owner says so — this setting can REMOVE somebody's row."""
        from shortlist.engine.models import EngineConfig
        from shortlist.server.settings_store import DEFAULTS

        assert client.put("/api/settings", json={"values": {"recommendations.cold_start": "skip"}}).status_code == 200
        assert client.get("/api/settings").json()["recommendations.cold_start"] == "skip"
        assert client.put("/api/settings", json={"values": {"recommendations.cold_start": "off"}}).status_code == 422

        # Floored at 1: at 0 nobody is ever cold, which silently disables the whole path.
        assert client.put("/api/settings", json={"values": {"recommendations.min_history": 0}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.min_history": 101}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.min_history": 4}}).status_code == 200
        assert client.get("/api/settings").json()["recommendations.min_history"] == 4

        assert DEFAULTS["recommendations.cold_start"] == EngineConfig().cold_start
        assert DEFAULTS["recommendations.min_history"] == EngineConfig().min_history

    def test_per_row_cold_start_round_trips_and_reaches_the_spec(self, client: TestClient):
        """The per-row half. `null` must stay null all the way to the spec — that is what "inherit"
        IS, and a column that quietly materialised "popular" would pin every row to today's global."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Because Row", "cold_start": "skip"})
        assert created.status_code == 201
        assert created.json()["cold_start"] == "skip"
        assert client.post("/api/collections", json={"name": "X", "cold_start": "bogus"}).status_code == 422

        inherits = client.post("/api/collections", json={"name": "Plain Row"})
        assert inherits.json()["cold_start"] is None

        # And a PATCH can hand it back to the global. (`name` rides along because CollectionIn
        # requires it on every request — the same shape the max_seeds patch test uses.)
        patched = client.patch(
            f"/api/collections/{created.json()['id']}", json={"name": "Because Row", "cold_start": None}
        )
        assert patched.status_code == 200 and patched.json()["cold_start"] is None

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            from shortlist.server.settings_store import SettingsStore

            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        by_slug = {s.slug: s for s in specs}
        assert by_slug[inherits.json()["slug"]].cold_start is None
        assert by_slug[created.json()["slug"]].cold_start is None  # the PATCH above handed it back

    def test_per_row_placement_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Top Row", "placement": "library", "pin_top": True})
        assert created.status_code == 201
        assert created.json()["placement"] == "library" and created.json()["pin_top"] is True
        # An unknown placement is rejected.
        assert client.post("/api/collections", json={"name": "X", "placement": "bogus"}).status_code == 422

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        spec = next(s for s in specs if s.slug == "top_row")
        assert spec.placement == "library" and spec.pin_top is True
        assert spec.show_library and not spec.show_home  # library-only

    @pytest.mark.parametrize("order", ROW_ORDERS)
    def test_every_pick_order_round_trips_and_reaches_the_spec(self, order, client: TestClient):
        """Each order the engine implements must survive the whole path: POST -> DB -> RowSpec.

        Parametrized over `ROW_ORDERS` — the engine's own tuple — rather than a list written out
        here, so adding a seventh order without widening the API's `ORDERS` set fails this test
        instead of shipping a value the engine honours but the API rejects with a 422.
        """
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": f"Order {order}", "pick_order": order})
        assert created.status_code == 201, f"the API rejected {order!r}: {created.json()}"
        assert created.json()["pick_order"] == order

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        spec = next(s for s in specs if s.slug == created.json()["slug"])
        assert spec.pick_order == order, f"{order!r} did not reach the engine spec"

    def test_an_unknown_pick_order_is_rejected(self, client: TestClient):
        """The closed set is what stops a typo silently delivering in rank order for ever — the
        engine's `_apply_order` falls back to the ranking rather than raising, so nothing downstream
        would ever report it."""
        assert client.post("/api/collections", json={"name": "X", "pick_order": "bogus"}).status_code == 422

    def test_an_all_surfaces_off_placement_round_trips(self, client: TestClient):
        """ "off" must survive the API — the UI's all-switches-off state has nowhere else to go, and
        collapsing it back to a default is exactly the bug in issue #6."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post(
            "/api/collections",
            json={"name": "Quiet Row", "placement": "off", "placement_friends": "off"},
        )
        assert created.status_code == 201
        assert created.json()["placement"] == "off"
        assert created.json()["placement_friends"] == "off"

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        spec = next(s for s in specs if s.slug == "quiet_row")
        assert not spec.show_home and not spec.show_friends_home
        assert not spec.show_owner_library and not spec.show_friends_library

    def test_the_two_placement_sides_reach_the_spec_independently(self, client: TestClient):
        """Owner keeps the Recommended shelf; friends' rows only reach Friends' Home."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post(
            "/api/collections",
            json={"name": "Split Row", "placement": "both", "placement_friends": "home"},
        )
        assert created.status_code == 201
        assert client.post("/api/collections", json={"name": "Y", "placement_friends": "bogus"}).status_code == 422

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        spec = next(s for s in specs if s.slug == "split_row")
        assert spec.show_owner_library and not spec.show_friends_library
        assert spec.show_home and spec.show_friends_home

    def test_a_row_can_be_anchored_to_another_row_and_bad_ones_are_refused(self, client: TestClient):
        """Issue #81. The anchor is a row SLUG, because a per-person row is one Plex collection per
        person and a title only ever names one account's copy.

        Every refusal here is refused at SAVE time on purpose. The engine's only sane response to a
        cycle is to leave those rows where they are — silently, once a night, in a log nobody reads.
        The moment to say "these two point at each other" is while someone is looking at the screen
        that created it.
        """
        first = client.post("/api/collections", json={"name": "Picked Row"})
        assert first.status_code == 201
        picked = first.json()["slug"]

        ok = client.post(
            "/api/collections",
            json={"name": "Because Row", "hub_anchor": {"2": {"row": picked}}},
        )
        assert ok.status_code == 201
        assert ok.json()["hub_anchor"]["2"] == {"anchor": "", "row": picked, "before": False, "top": False}
        because = ok.json()["slug"]

        missing = client.post(
            "/api/collections", json={"name": "Ghost Row", "hub_anchor": {"2": {"row": "no-such-row"}}}
        )
        assert missing.status_code == 422 and "no row called" in missing.json()["detail"]

        both = client.post(
            "/api/collections",
            json={"name": "Both Row", "hub_anchor": {"2": {"row": picked, "anchor": "New Series"}}},
        )
        assert both.status_code == 422, "row wins in the engine, so accepting both would hide one of them"

        itself = client.patch(
            f"/api/collections/{first.json()['id']}", json={"name": "Picked Row", "hub_anchor": {"2": {"row": picked}}}
        )
        assert itself.status_code == 422 and "after itself" in itself.json()["detail"]

        # 'because' already follows 'picked'; pointing 'picked' at 'because' closes the loop.
        loop = client.patch(
            f"/api/collections/{first.json()['id']}",
            json={"name": "Picked Row", "hub_anchor": {"2": {"row": because}}},
        )
        assert loop.status_code == 422 and "loop" in loop.json()["detail"]

        # The same pair in a DIFFERENT library is not a loop — anchors are per library.
        other_library = client.patch(
            f"/api/collections/{first.json()['id']}",
            json={"name": "Picked Row", "hub_anchor": {"3": {"row": because}}},
        )
        assert other_library.status_code == 200

    def test_a_loop_further_down_the_chain_does_not_block_an_unrelated_edit(self, client: TestClient):
        """Only a loop THIS edit closes is the editor's problem.

        The chain walk revisits nodes, so a tangle that already exists further along (a direct DB
        edit, a restore, an import) would otherwise 422 an innocent save with "that would make a
        loop" — untrue of their edit, and nothing they can act on. The engine already declines to
        place a cycle; this save is genuinely fine.
        """
        a = client.post("/api/collections", json={"name": "Row A"}).json()
        b = client.post("/api/collections", json={"name": "Row B"}).json()
        c = client.post("/api/collections", json={"name": "Row C"}).json()

        # Plant B <-> C directly, the way a restore or a hand-edited DB would.
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            session.get(Collection, b["id"]).hub_anchor = {"2": {"row": c["slug"], "before": False}}
            session.get(Collection, c["id"]).hub_anchor = {"2": {"row": b["slug"], "before": False}}
            session.commit()

        # A, which is in no loop, points at B. That edit creates nothing and must be allowed.
        saved = client.patch(
            f"/api/collections/{a['id']}",
            json={"name": "Row A", "hub_anchor": {"2": {"row": b["slug"]}}},
        )
        assert saved.status_code == 200, saved.json()

    def test_deleting_a_row_clears_the_anchors_that_pointed_at_it(self, client: TestClient):
        """Otherwise the rows that followed it point at nothing forever.

        The engine skips an anchor row it cannot resolve — right for a row that simply has not
        delivered into that library yet, wrong for one that no longer exists — and from inside a run
        those two look identical. So the reference is cleared here, and those rows fall back to the
        library default, which is where a row with no placement of its own belongs.
        """
        picked = client.post("/api/collections", json={"name": "Anchor Row"})
        assert picked.status_code == 201
        follower = client.post(
            "/api/collections",
            json={"name": "Follower Row", "hub_anchor": {"2": {"row": picked.json()["slug"]}}},
        )
        assert follower.status_code == 201

        assert client.delete(f"/api/collections/{picked.json()['id']}").status_code in (200, 204)

        after = client.get("/api/collections").json()
        still = next(r for r in after if r["id"] == follower.json()["id"])
        assert still["hub_anchor"] == {}, "the dangling anchor must be gone, not left pointing at a ghost"

    def test_per_row_hub_anchor_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.engine.models import HubAnchor
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        body = {"name": "Gems Row", "hub_anchor": {"2": {"anchor": "New Series", "before": True}}}
        created = client.post("/api/collections", json=body)
        assert created.status_code == 201
        assert created.json()["hub_anchor"] == {"2": {"anchor": "New Series", "row": "", "before": True, "top": False}}
        # A blank anchor with no top is rejected by the shape.
        blank = client.post("/api/collections", json={"name": "X", "hub_anchor": {"2": {"anchor": ""}}})
        assert blank.status_code == 422
        # A 'top' entry needs no anchor.
        top = client.post("/api/collections", json={"name": "Top Gems", "hub_anchor": {"2": {"top": True}}})
        assert top.status_code == 201 and top.json()["hub_anchor"]["2"]["top"] is True

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "gems_row").hub_anchors == {
            "2": HubAnchor(anchor_title="New Series", before=True)
        }
        assert next(s for s in specs if s.slug == "top_gems").hub_anchors == {"2": HubAnchor(to_top=True)}

    def test_a_row_anchor_survives_the_save_and_reaches_the_engine_as_a_slug(self, client: TestClient):
        """The link between the two halves of issue #81: the API stores it and the engine receives it.

        Both ends are covered elsewhere — the API refuses bad anchors, and the engine places a row
        after another row's block — but nothing proved the middle. `_parse_hub_anchors` reads `row`
        BEFORE `anchor`, and a parse that dropped it would leave every save looking correct while the
        engine silently fell back to the library default.
        """
        from shortlist.engine.models import HubAnchor
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        first = client.post("/api/collections", json={"name": "Anchor Target"})
        target = first.json()["slug"]
        follower = client.post(
            "/api/collections",
            json={"name": "Follows It", "hub_anchor": {"2": {"row": target, "before": True}}},
        )
        assert follower.status_code == 201

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))

        assert next(s for s in specs if s.slug == follower.json()["slug"]).hub_anchors == {
            "2": HubAnchor(anchor_row=target, before=True)
        }

    def test_a_disabled_row_becomes_a_retired_row_for_cleanup(self, client: TestClient):
        """A row switched off is not delivered (dropped from _build_rows) AND handed to the engine as
        a retired row, so its lingering collection is removed from its owner's Home on the next run."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid = created.json()["id"]
        client.patch(f"/api/collections/{cid}", json={"name": "Hidden Gems", "enabled": False})

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            retired = builder._retired_rows(session, store)
            built = builder._build_rows(session, store)

        assert "hidden_gems" not in {s.slug for s in built}  # not delivered
        assert "hidden_gems" in {s.slug for s in retired}  # but queued for removal
        assert next(s for s in retired if s.slug == "hidden_gems").name_template == "Hidden Gems"

    def test_a_disabled_dynamic_title_row_is_not_retired(self, client: TestClient):
        """A {top_seed} title renders to the DEFAULT row's title when there are no picks, and all of a
        user's per-person rows share one label (told apart by title only). Retiring such a row would
        match and DELETE the user's live default row — so it must be skipped, not queued for removal."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Because You Watched"})
        cid = created.json()["id"]
        # Give it a dynamic title, then disable it.
        client.patch(
            f"/api/collections/{cid}",
            json={"name": "Because You Watched", "name_template": "Because you watched {top_seed}", "enabled": False},
        )

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            retired = builder._retired_rows(session, SettingsStore(session, client.app.state.secrets))

        assert "because_you_watched" not in {s.slug for s in retired}, "a dynamic-title row must not be auto-removed"

    def test_a_disabled_whitespace_title_row_is_not_retired(self, client: TestClient):
        """A whitespace-only template also renders to the DEFAULT title (strip -> empty), so it would
        collide with the live default row just like {top_seed}. The guard tests the RENDERED title,
        not a substring, so this must be skipped too."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Blankish"})
        cid = created.json()["id"]
        client.patch(f"/api/collections/{cid}", json={"name": "Blankish", "name_template": "   ", "enabled": False})

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            retired = builder._retired_rows(session, SettingsStore(session, client.app.state.secrets))

        assert "blankish" not in {s.slug for s in retired}, "a whitespace-title row must not be auto-removed"

    def test_poster_config_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        body = {"name": "Poster Row", "poster": {"mode": "generate", "title": "{user}'s Picks", "style": "neon"}}
        created = client.post("/api/collections", json=body)
        assert created.status_code == 201
        poster = created.json()["poster"]
        assert poster["mode"] == "generate" and poster["title"] == "{user}'s Picks" and poster["has_image"] is False
        # An unknown mode is rejected.
        assert client.post("/api/collections", json={"name": "X", "poster": {"mode": "bogus"}}).status_code == 422

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        spec = next(s for s in specs if s.slug == "poster_row")
        assert spec.poster is not None and spec.poster.mode == "generate" and spec.poster.style == "neon"

    def test_an_oversized_poster_is_refused_on_its_declared_length(self, client: TestClient):
        """The size check used to run AFTER `await file.read()` — so a 500 MB post was fully received
        and spilled to a temp file before being told it was too big. Content-Length is checked first;
        the read-side check stays as the real guard for a request that lies or omits it."""
        from shortlist.server.services import poster_service

        cid = client.post("/api/collections", json={"name": "Too Big"}).json()["id"]
        oversized = b"\0" * (poster_service.MAX_UPLOAD_BYTES + 8192)

        r = client.post(f"/api/collections/{cid}/poster/upload", files={"file": ("big.png", oversized, "image/png")})

        assert r.status_code == 413

    def test_poster_upload_stores_switches_mode_and_serves_the_image(self, client: TestClient):
        import base64

        # A genuine 1x1 PNG — normalize_upload (when Pillow is present) rejects non-images.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        )
        created = client.post("/api/collections", json={"name": "Uploaded Poster"})
        cid = created.json()["id"]
        # No image yet.
        assert client.get(f"/api/collections/{cid}/poster/image").status_code == 404

        upload = client.post(
            f"/api/collections/{cid}/poster/upload",
            files={"file": ("poster.png", png, "image/png")},
        )
        assert upload.status_code == 200 and upload.json()["mode"] == "upload"
        assert set(upload.json()) == {"ok", "mode"}

        # The row is now in upload mode and reports an image; the image endpoint serves it.
        got = next(c for c in client.get("/api/collections").json() if c["id"] == cid)
        assert set(got["poster"]) == POSTER_KEYS
        assert got["poster"]["mode"] == "upload" and got["poster"]["has_image"] is True
        image = client.get(f"/api/collections/{cid}/poster/image")
        assert image.status_code == 200 and image.headers["content-type"].startswith("image/") and image.content

        # A non-image upload is rejected (only when Pillow can tell).
        bad = client.post(
            f"/api/collections/{cid}/poster/upload", files={"file": ("x.png", b"not an image", "image/png")}
        )
        assert bad.status_code in (200, 422)  # 422 with Pillow, 200 (stored as-is) without

        # Deleting the image removes it (mode stays "upload", so nothing is served afterwards).
        assert client.delete(f"/api/collections/{cid}/poster/image").status_code == 204
        assert client.get(f"/api/collections/{cid}/poster/image").status_code == 404

    def test_dropping_a_custom_poster_triggers_a_reset(self, client: TestClient, monkeypatch):
        from shortlist.server.services import collection_reconcile as rec

        calls: list[tuple[str, str, str]] = []

        async def fake_reset(state, *, slug, build, scope):
            calls.append((slug, build, scope))
            return [], None

        monkeypatch.setattr(rec, "run_poster_reset", fake_reset)
        created = client.post("/api/collections", json={"name": "Art Row", "poster": {"mode": "text", "title": "Hi"}})
        cid = created.json()["id"]
        # Switching back to Plex default must reconcile a revert onto Plex.
        client.patch(
            f"/api/collections/{cid}",
            json={"name": "Art Row", "poster": {"mode": "", "title": "", "subtitle": "", "style": ""}},
        )
        assert calls and calls[0][2] == "collection.poster"
        # A no-op poster save (still default) does NOT trigger a reset.
        calls.clear()
        client.patch(
            f"/api/collections/{cid}",
            json={"name": "Art Row", "poster": {"mode": "", "title": "", "subtitle": "", "style": ""}},
        )
        assert calls == []

    def test_image_provider_status_reports_incapable_without_an_image_provider(self, client: TestClient):
        status = client.get("/api/system/image-provider")
        assert status.status_code == 200
        # The test config has no OpenAI/Google curator, so generation is not available.
        assert status.json()["capable"] is False


class TestCollectionsApi:
    def test_list_starts_with_the_seeded_default(self, client: TestClient):
        cols = client.get("/api/collections").json()
        assert [c["slug"] for c in cols] == ["picked"]
        assert set(cols[0]) == COLLECTION_KEYS
        assert set(cols[0]["poster"]) == POSTER_KEYS

    def test_an_audience_naming_a_user_who_does_not_exist_is_refused(self, client: TestClient):
        """`CollectionAudience.user_id` is a foreign key with `PRAGMA foreign_keys=ON`, so an unknown
        id used to reach the DB and come back as an unhandled `IntegrityError` — a 500 with a SQL
        string in it, where every other bad input on this router is a 422. On a SHARED row this list
        decides who is excluded from the share filter, so it must not be guessed at either.

        Both entry points, POST and PATCH, and the row must be left exactly as it was.
        """
        real = next(u["id"] for u in client.get("/api/users").json())

        created = client.post(
            "/api/collections",
            json={"name": "Ghosts", "audience": "subset", "audience_user_ids": [real, 999_999]},
        )
        assert created.status_code == 422
        assert "999999" in created.json()["detail"]
        assert "Ghosts" not in {c["name"] for c in client.get("/api/collections").json()}

        ok = client.post(
            "/api/collections",
            json={"name": "Ghosts", "audience": "subset", "audience_user_ids": [real], "size": 10},
        )
        assert ok.status_code == 201
        cid = ok.json()["id"]

        patched = client.patch(f"/api/collections/{cid}", json={"audience_user_ids": [999_999]})
        assert patched.status_code == 422
        # The refusal rolled back the whole PATCH, so the row still has the audience it started with.
        assert client.get("/api/collections").json()[-1]["audience_user_ids"] == [real]

    def test_create_update_delete_per_person(self, client: TestClient):
        created = client.post(
            "/api/collections",
            json={"name": "Hidden Gems", "size": 10},
        )
        assert created.status_code == 201
        cid = created.json()["id"]
        assert set(created.json()) == COLLECTION_KEYS
        assert created.json()["slug"] == "hidden_gems"
        assert created.json()["build"] == "per_person"

        updated = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Hidden Gems", "size": 20, "enabled": False},
        )
        assert updated.status_code == 200
        assert set(updated.json()) == COLLECTION_KEYS, "the PATCH renders the same row the POST did"
        assert updated.json()["size"] == 20 and updated.json()["enabled"] is False

        assert client.delete(f"/api/collections/{cid}").status_code == 204
        assert [c["slug"] for c in client.get("/api/collections").json()] == ["picked"]

    def test_rewatch_and_unstarted_only_round_trip(self, client: TestClient):
        created = client.post(
            "/api/collections",
            json={"name": "Again", "size": 10, "rewatch": True, "watched_pct": 1.0},
        )
        assert created.status_code == 201
        assert created.json()["rewatch"] is True
        assert created.json()["unstarted_only"] is False

        cid = created.json()["id"]
        patched = client.patch(f"/api/collections/{cid}", json={"name": "Again", "rewatch": False})
        assert patched.status_code == 200 and patched.json()["rewatch"] is False

    def test_a_rewatch_row_cannot_also_exclude_everything_started(self, client: TestClient):
        """They ask for opposite things, and the failure is SILENT: `unstarted_only` leaves only
        never-opened series in the pool, so the rewatch ordering finds nothing finished to lead with
        and the row fills with unseen titles under a "you've already seen" name."""
        r = client.post(
            "/api/collections",
            json={"name": "Nonsense", "media": "show", "rewatch": True, "unstarted_only": True},
        )
        assert r.status_code == 422
        assert "opposite" in r.json()["detail"]

    def test_unstarted_only_is_refused_on_a_movies_row(self, client: TestClient):
        """A movie is finished the moment it is watched, so there is no "started" state. The flag is
        structurally inert there (`_started_shows` yields only SHOW keys) and the editor hides it — so
        storing it would leave a row behaving unlike what its settings say."""
        r = client.post(
            "/api/collections",
            json={"name": "Films", "media": "movie", "unstarted_only": True},
        )
        assert r.status_code == 422
        assert "shows" in r.json()["detail"]

        # A shows row and a both-media row are both fine.
        for media in ("show", "both"):
            ok = client.post(
                "/api/collections",
                json={"name": f"Start {media}", "media": media, "unstarted_only": True},
            )
            assert ok.status_code == 201, f"{media}: {ok.text}"

    def test_patching_into_the_contradiction_is_refused_too(self, client: TestClient):
        """The PATCH path merges onto stored values, so validating only the POST body would let the
        same invalid pair in one field at a time."""
        cid = client.post(
            "/api/collections",
            json={"name": "Again", "media": "show", "rewatch": True, "watched_pct": 1.0},
        ).json()["id"]

        # `name` is required on PATCH, so it must be sent — without it the request 422s on the missing
        # field and the test would pass without ever exercising the contradiction check.
        ok_shape = client.patch(f"/api/collections/{cid}", json={"name": "Again", "size": 12})
        assert ok_shape.status_code == 200, f"the patch shape itself must be valid: {ok_shape.text}"

        r = client.patch(f"/api/collections/{cid}", json={"name": "Again", "unstarted_only": True})
        assert r.status_code == 422, "a row must not be able to reach the contradiction in two steps"
        assert "opposite" in r.json()["detail"]

    def test_narrowing_a_row_to_movies_cannot_strand_unstarted_only(self, client: TestClient):
        """The other one-field-at-a-time route into an invalid row."""
        cid = client.post(
            "/api/collections",
            json={"name": "To start", "media": "show", "unstarted_only": True},
        ).json()["id"]

        r = client.patch(f"/api/collections/{cid}", json={"name": "To start", "media": "movie"})
        assert r.status_code == 422
        assert "shows" in r.json()["detail"]

    def _fake_plex_ctx(self, monkeypatch, client, *, collections):
        """Point run_service.build_context at a fake Plex that records deletions."""
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        deleted: list[str] = []
        section = SimpleNamespace(title="Movies")
        plex = MagicMock()
        plex.sections.return_value = [section]
        # Return objects with a .title for each (title, label) pair whose label matches.
        plex.find_owned_collections.side_effect = lambda s, label: [
            SimpleNamespace(title=title) for (title, lbl) in collections if lbl == label
        ]
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append(c.title)
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)
        return deleted

    def test_cleanup_removes_a_shared_rows_collection_by_its_label(self, client: TestClient, monkeypatch):
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Popular", "build": "shared"})
        cid, slug = created.json()["id"], created.json()["slug"]
        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[("🔥 Popular" + row_marker(0), f"shortlist__shared_{slug}")],
        )

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})
        assert r.status_code == 200
        assert r.json()["removed"] == ["🔥 Popular"]  # marker stripped for the audit
        assert len(deleted) == 1

    def test_cleanup_dry_run_reports_without_deleting(self, client: TestClient, monkeypatch):
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Popular", "build": "shared"})
        cid, slug = created.json()["id"], created.json()["slug"]
        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("🔥 Popular" + row_marker(0), f"shortlist__shared_{slug}")]
        )

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": True})
        assert r.status_code == 200
        assert set(r.json()) == {"removed", "dry_run", "message"}
        assert r.json()["removed"] == ["🔥 Popular"] and r.json()["dry_run"] is True
        assert deleted == []  # nothing actually removed

    def test_cleanup_removes_a_per_person_row_for_each_user_in_the_breakdown(self, client: TestClient, monkeypatch):
        """The complex branch: pin each user's collection by the exact title the last run delivered,
        under that user's own label — and skip a user whose breakdown has no entry for this row."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser

        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]

        with client.app.state.sessions() as session:
            users = session.query(User).order_by(User.id).all()
            assert len(users) >= 2, "fixture must seed at least two users"
            u1, u2 = users[0], users[1]
            u1_slug, u1_acct = u1.slug, u1.plex_account_id
            u2_slug, u2_acct = u2.slug, u2.plex_account_id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            # Both users got this row last run (only u2's breakdown lacks it below stays skipped);
            # here BOTH have it, and any third user has none.
            for uid in (u1.id, u2.id):
                session.add(
                    RunUser(
                        run_id=run.id,
                        user_id=uid,
                        status="ok",
                        breakdown=[{"row_slug": slug, "row_title": "Gems", "library_key": "1"}],
                    )
                )
            session.commit()

        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[
                ("Gems" + row_marker(u1_acct), f"shortlist_{u1_slug}"),
                ("Gems" + row_marker(u2_acct), f"shortlist_{u2_slug}"),
            ],
        )

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})
        assert r.status_code == 200
        assert set(r.json()["removed"]) == {"Gems"}  # marker stripped; both users' collections
        assert len(deleted) == 2  # one per user WITH a breakdown entry for this row

    def test_a_per_person_row_is_removed_with_no_run_history_at_all(self, client: TestClient, monkeypatch):
        """Addressing a collection by "the title the LATEST completed run recorded" is why deleting a
        row could remove nothing and audit it as "removed 0" — and for a deleted row there is no second
        chance. Rows have their own crons, so the latest run is routinely scoped to a different row;
        `DELETE /api/runs` empties the record outright.

        Rendering the row's own template covers it: computed from config, so it holds whatever history
        says. NO run is set up here, deliberately."""
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid = created.json()["id"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id

        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("Hidden Gems" + row_marker(acct), f"shortlist_{uslug}")]
        )

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})

        assert r.status_code == 200
        assert r.json()["removed"] == ["Hidden Gems"]
        assert len(deleted) == 1

    def test_removal_leaves_another_row_of_the_same_user_alone(self, client: TestClient, monkeypatch):
        """All of one user's rows share ONE label, so the title is the only thing separating them.
        Rendering a template that matched loosely would delete somebody's other row — the failure that
        matters far more than missing one."""
        from shortlist.engine.delivery import row_marker

        keep = client.post("/api/collections", json={"name": "Keep Me"})
        drop = client.post("/api/collections", json={"name": "Drop Me"})
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id

        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[
                ("Keep Me" + row_marker(acct), f"shortlist_{uslug}"),
                ("Drop Me" + row_marker(acct), f"shortlist_{uslug}"),
            ],
        )

        r = client.post(f"/api/collections/{drop.json()['id']}/cleanup", json={"dry_run": False})

        assert r.json()["removed"] == ["Drop Me"]
        assert deleted == ["Drop Me" + row_marker(acct)]
        assert keep.status_code == 201

    def test_deleting_a_row_also_removes_its_plex_collection(self, client: TestClient, monkeypatch):
        """Delete now cleans Plex first (while the slug still exists), THEN drops the DB row.

        Shared build only: delete adds a build-agnostic reconcile STEP; the per-person branch itself
        is covered by test_cleanup_removes_a_per_person_row_for_each_user_in_the_breakdown.
        """
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Popular", "build": "shared"})
        cid, slug = created.json()["id"], created.json()["slug"]
        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("🔥 Popular" + row_marker(0), f"shortlist__shared_{slug}")]
        )

        assert client.delete(f"/api/collections/{cid}").status_code == 204
        assert len(deleted) == 1  # its Plex collection was removed
        assert slug not in {c["slug"] for c in client.get("/api/collections").json()}  # and the DB row is gone

    def test_shrinking_a_rows_audience_removes_only_the_dropped_users_collection(self, client: TestClient, monkeypatch):
        """Dropping a user from a subset audience removes THAT user's collection; the kept user's is
        left untouched (only_user_ids scopes the sweep). Adding a user is a create → left for a run."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser

        u_ids = [u["id"] for u in client.get("/api/users").json()]
        created = client.post(
            "/api/collections", json={"name": "Gems", "audience": "subset", "audience_user_ids": u_ids}
        )
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            by_id = {u.id: u for u in session.query(User).all()}
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            for uid in u_ids:
                session.add(
                    RunUser(
                        run_id=run.id, user_id=uid, status="ok", breakdown=[{"row_slug": slug, "row_title": "Gems"}]
                    )
                )
            session.commit()
            slugs = {uid: by_id[uid].slug for uid in u_ids}
            accts = {uid: by_id[uid].plex_account_id for uid in u_ids}

        keep, drop = u_ids[0], u_ids[1]
        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[
                ("Gems" + row_marker(accts[keep]), f"shortlist_{slugs[keep]}"),
                ("Gems" + row_marker(accts[drop]), f"shortlist_{slugs[drop]}"),
            ],
        )

        r = client.patch(
            f"/api/collections/{cid}", json={"name": "Gems", "audience": "subset", "audience_user_ids": [keep]}
        )
        assert r.status_code == 200
        # Exactly the DROPPED user's collection (its account marker), never the kept user's.
        assert deleted == ["Gems" + row_marker(accts[drop])]

    def test_widening_from_everyone_to_a_subset_removes_the_complement(self, client: TestClient, monkeypatch):
        """everyone → subset: the audience state flips from the 'everyone' branch (old = all ids) to a
        subset, so every user NOT in the new subset is dropped and their row removed — the largest
        removal in the matrix, and the one where old_users resolves via 'everyone', not CollectionAudience."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser

        u_ids = [u["id"] for u in client.get("/api/users").json()]
        assert len(u_ids) >= 2, "fixture must seed at least two users"
        created = client.post("/api/collections", json={"name": "Gems", "audience": "everyone"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            by_id = {u.id: u for u in session.query(User).all()}
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            for uid in u_ids:
                session.add(
                    RunUser(
                        run_id=run.id, user_id=uid, status="ok", breakdown=[{"row_slug": slug, "row_title": "Gems"}]
                    )
                )
            session.commit()
            slugs = {uid: by_id[uid].slug for uid in u_ids}
            accts = {uid: by_id[uid].plex_account_id for uid in u_ids}

        keep, dropped = u_ids[0], u_ids[1:]
        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[("Gems" + row_marker(accts[uid]), f"shortlist_{slugs[uid]}") for uid in u_ids],
        )

        r = client.patch(
            f"/api/collections/{cid}", json={"name": "Gems", "audience": "subset", "audience_user_ids": [keep]}
        )
        assert r.status_code == 200
        assert set(deleted) == {"Gems" + row_marker(accts[uid]) for uid in dropped}
        assert "Gems" + row_marker(accts[keep]) not in deleted  # the kept user's row is untouched

    def test_widening_a_subset_to_everyone_removes_nothing(self, client: TestClient, monkeypatch):
        """subset → everyone: the audience only grew (old ⊆ new), so dropped = ∅ and nothing is removed.
        A newly included user's row is a create, left for the next gated run — never removed here."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser

        u_ids = [u["id"] for u in client.get("/api/users").json()]
        keep = u_ids[0]
        created = client.post(
            "/api/collections", json={"name": "Gems", "audience": "subset", "audience_user_ids": [keep]}
        )
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            by_id = {u.id: u for u in session.query(User).all()}
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(run_id=run.id, user_id=keep, status="ok", breakdown=[{"row_slug": slug, "row_title": "Gems"}])
            )
            session.commit()
            slugs = {uid: by_id[uid].slug for uid in u_ids}
            accts = {uid: by_id[uid].plex_account_id for uid in u_ids}

        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("Gems" + row_marker(accts[keep]), f"shortlist_{slugs[keep]}")]
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "audience": "everyone"})
        assert r.status_code == 200
        assert deleted == []  # audience only widened → no reconcile removal

    def test_patching_a_non_audience_field_never_touches_plex(self, client: TestClient, monkeypatch):
        """A size-only PATCH on a per-person row must NOT enter the audience reconcile at all — no Plex
        round-trip. build_context is the sole entry to Plex here, so a spy that must-not-be-called guards
        the touching_audience gate directly (asserting deleted==[] alone couldn't tell a skip from a
        run-that-found-nothing)."""
        from unittest.mock import MagicMock

        created = client.post("/api/collections", json={"name": "Gems", "audience": "everyone"})
        cid = created.json()["id"]
        spy = MagicMock()
        monkeypatch.setattr(client.app.state.run_service, "build_context", spy)

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "size": 15})
        assert r.status_code == 200 and r.json()["size"] == 15
        spy.assert_not_called()

    def _fake_rename_ctx(self, monkeypatch, client, *, titles_by_label, fail=False):
        """Point build_context at a fake Plex whose collections record editTitle() renames.

        titles_by_label: {label -> current title}. Returns the `renames` list of (old, new) titles.
        When `fail`, editTitle raises — to exercise the best-effort/audit failure path (rule 5/9)."""
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        renames: list[tuple[str, str]] = []
        cols = {}
        for label, title in titles_by_label.items():
            col = MagicMock(title=title)
            if fail:
                col.editTitle.side_effect = RuntimeError("PMS 500 at http://pms:32400/library?X-Plex-Token=SEKRET")
            else:
                col.editTitle.side_effect = lambda new, c=col: renames.append((c.title, new))
            cols[label] = col
        section = SimpleNamespace(title="Movies")
        plex = MagicMock()
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda s, label: [cols[label]] if label in cols else []
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)
        return renames

    def test_renaming_a_row_retitles_each_users_collection_in_place(self, client: TestClient, monkeypatch):
        """Rename → every user who has the row gets their collection retitled in place (multi-row users
        would otherwise keep the old-named copy). New human title, same per-account marker.

        NO run history is set up, deliberately. The reconcile enumerates collections from PLEX by label
        and identifies this row's by what the OLD template renders to. It used to read the latest
        completed run's breakdown instead, which meant a row renamed the morning after a DIFFERENT row
        ran silently renamed nothing at all."""
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Old Gems"})
        cid = created.json()["id"]
        with client.app.state.sessions() as session:
            info = [(u.slug, u.plex_account_id) for u in session.query(User).order_by(User.id).all()[:2]]

        renames = self._fake_rename_ctx(
            monkeypatch,
            client,
            titles_by_label={f"shortlist_{uslug}": "Old Gems" + row_marker(acct) for uslug, acct in info},
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Buried Treasure"})
        assert r.status_code == 200
        expected = {("Old Gems" + row_marker(acct), "Buried Treasure" + row_marker(acct)) for _, acct in info}
        assert set(renames) == expected  # each account's row retitled, marker preserved

        # The audit records WHOSE row went from what to what, in which libraries (rule 10).
        from shortlist.server.db.models import Event

        with client.app.state.sessions() as session:
            audit = session.query(Event).filter_by(scope="collection.rename").order_by(Event.id.desc()).first()
        by_user = {e["user"]: e for e in audit.message["renames"]}
        assert set(by_user) == {uslug for uslug, _ in info}
        for uslug, _ in info:
            assert by_user[uslug]["old"] == "Old Gems" and by_user[uslug]["new"] == "Buried Treasure"
            assert by_user[uslug]["libraries"] == ["Movies"]

    def test_a_rename_still_works_after_the_run_history_is_cleared(self, client: TestClient, monkeypatch):
        """`DELETE /api/runs` says it "changes nothing on Plex" — and that was true only because it
        silently disarmed every reconcile. Addressing a collection by "the title the latest completed
        run recorded" meant clearing history left nothing able to find it, and so did the far more
        common case: rows have their own crons, so the latest run is routinely scoped to ONE row."""
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Old Gems"})
        cid = created.json()["id"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
        assert client.delete("/api/runs").status_code in (200, 204)

        renames = self._fake_rename_ctx(
            monkeypatch, client, titles_by_label={f"shortlist_{uslug}": "Old Gems" + row_marker(acct)}
        )
        r = client.patch(f"/api/collections/{cid}", json={"name": "Buried Treasure"})

        assert r.status_code == 200
        assert renames == [("Old Gems" + row_marker(acct), "Buried Treasure" + row_marker(acct))]

    def test_renaming_to_a_library_name_template_retitles_per_library(self, client: TestClient, monkeypatch):
        """A {library_name} rename renders per library, in the SAME library the collection is in — the
        name comes from the Plex section being walked, so the Movies collection gets the Movies title
        and not some other library's."""
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Old Gems"})
        cid = created.json()["id"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id

        renames = self._fake_rename_ctx(
            monkeypatch, client, titles_by_label={f"shortlist_{uslug}": "Old Gems" + row_marker(acct)}
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "✨ {library_name} Fresh"})
        assert r.status_code == 200
        # The Movies library's name fills {library_name}, so the old row is retitled to its Movies form.
        assert renames == [("Old Gems" + row_marker(acct), "✨ Movies Fresh" + row_marker(acct))]

    def test_renaming_via_a_static_name_template_also_reconciles(self, client: TestClient, monkeypatch):
        """A name_template-only change (name untouched) is a rename too — the effective title is the
        template, so changing it must retitle the collection in place."""
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Gems"})
        cid = created.json()["id"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id

        renames = self._fake_rename_ctx(
            monkeypatch, client, titles_by_label={f"shortlist_{uslug}": "Gems" + row_marker(acct)}
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "name_template": "Buried Treasure"})
        assert r.status_code == 200
        assert renames == [("Gems" + row_marker(acct), "Buried Treasure" + row_marker(acct))]

    def test_rename_reconcile_survives_a_plex_error(self, client: TestClient, monkeypatch):
        """A PMS failure mid-rename is best-effort: the PATCH still returns 200, and the failure is
        audited with the token redacted (rules 5 + 9) — never surfaced raw or fatal.

        The failure is per-collection, so it must not stop the walk NOR vanish from the audit: a
        swallowed error records "renamed 0 collections", which reads exactly like "nothing needed
        renaming" — the one distinction an operator has to be able to make."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Event

        created = client.post("/api/collections", json={"name": "Old Gems"})
        cid = created.json()["id"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id

        self._fake_rename_ctx(
            monkeypatch, client, titles_by_label={f"shortlist_{uslug}": "Old Gems" + row_marker(acct)}, fail=True
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Buried Treasure"})
        assert r.status_code == 200  # best-effort: the rename failure never fails the PATCH
        with client.app.state.sessions() as session:
            audit = session.query(Event).filter_by(scope="collection.rename").order_by(Event.id.desc()).first()
        assert audit.message["error"] is not None
        assert "SEKRET" not in str(audit.message) and "REDACTED" in audit.message["error"]  # rule 9

    def test_renaming_to_a_dynamic_template_is_left_for_the_next_run(self, client: TestClient, monkeypatch):
        """A {top_seed} template renders to the default title with no picks, so the reconcile skips it
        rather than retitle to the wrong name — the next run's delivery renames the sole-row case."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser

        created = client.post("/api/collections", json={"name": "Old Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id, user_id=user.id, status="ok", breakdown=[{"row_slug": slug, "row_title": "Old Gems"}]
                )
            )
            session.commit()

        renames = self._fake_rename_ctx(
            monkeypatch, client, titles_by_label={f"shortlist_{uslug}": "Old Gems" + row_marker(acct)}
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Old Gems", "name_template": "{top_seed} Picks"})
        assert r.status_code == 200
        assert renames == []  # dynamic new title → skipped, not retitled to the default name

    def test_renaming_the_default_row_leaves_a_users_own_name_override_untouched(self, client: TestClient, monkeypatch):
        """The default row resolves each user's title as their own `row_name_tpl` or the global template.
        Renaming the global template must retitle a user on the default, but NOT one who set a personal
        name — the reconcile re-renders the override user from THEIR template, sees no change, skips them."""
        from shortlist.engine.delivery import row_marker

        # Two users on the default row: one on the global template, one with a personal name override.
        with client.app.state.sessions() as session:
            plain, custom = session.query(User).order_by(User.id).all()[:2]
            custom.prefs = {"row_name_tpl": "🌟 My Own Picks"}
            plain_info = (plain.slug, plain.plex_account_id)
            custom_info = (custom.slug, custom.plex_account_id)
            session.commit()

        # Each user's collection carries the title THEIR template renders to in the Movies library —
        # which is how the reconcile identifies them, with no run history involved.
        renames = self._fake_rename_ctx(
            monkeypatch,
            client,
            titles_by_label={
                f"shortlist_{plain_info[0]}": "✨ Movies Picked for You" + row_marker(plain_info[1]),
                f"shortlist_{custom_info[0]}": "🌟 My Own Picks" + row_marker(custom_info[1]),
            },
        )

        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        r = client.patch(f"/api/collections/{picked['id']}", json={"name": "✨ Handpicked"})
        assert r.status_code == 200
        # Only the plain user is retitled; the override user's collection is left exactly as it was.
        plain_marker = row_marker(plain_info[1])
        assert renames == [("✨ Movies Picked for You" + plain_marker, "✨ Handpicked" + plain_marker)]

    def test_changing_a_rows_build_removes_the_old_builds_collections(self, client: TestClient, monkeypatch):
        """Flipping per-person → shared removes the old per-person per-user collections, so both builds
        don't live on Home at once. A removal, so gate-exempt."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser

        created = client.post("/api/collections", json={"name": "Gems"})  # per_person by default
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            users = session.query(User).order_by(User.id).all()[:2]
            info = [(u.slug, u.plex_account_id) for u in users]
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            for u in users:
                session.add(
                    RunUser(
                        run_id=run.id, user_id=u.id, status="ok", breakdown=[{"row_slug": slug, "row_title": "Gems"}]
                    )
                )
            session.commit()

        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[("Gems" + row_marker(acct), f"shortlist_{uslug}") for uslug, acct in info],
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "build": "shared"})
        assert r.status_code == 200 and r.json()["build"] == "shared"
        # Every user's OLD per-person collection was removed (the new shared row builds on the next run).
        assert set(deleted) == {"Gems" + row_marker(acct) for _, acct in info}

    def test_changing_a_shared_row_to_per_person_removes_the_shared_collection(self, client: TestClient, monkeypatch):
        """The other direction of the flip: shared → per-person removes the OLD shared collection (found
        by its own shared label), so it doesn't linger while the new per-person rows build."""
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Popular", "build": "shared"})
        cid, slug = created.json()["id"], created.json()["slug"]
        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("🔥 Popular" + row_marker(0), f"shortlist__shared_{slug}")]
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Popular", "build": "per_person"})
        assert r.status_code == 200 and r.json()["build"] == "per_person"
        assert deleted == ["🔥 Popular" + row_marker(0)]  # the old shared collection removed by its label

    def test_shared_collection_with_subset_audience(self, client: TestClient):
        users = client.get("/api/users").json()
        ids = [u["id"] for u in users]
        created = client.post(
            "/api/collections",
            json={
                "name": "Staff Picks",
                "build": "shared",
                "audience": "subset",
                "audience_user_ids": ids,
                "min_watchers": 3,
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["build"] == "shared"
        assert sorted(body["audience_user_ids"]) == sorted(ids)
        assert body["min_watchers"] == 3

    def test_the_default_row_can_be_deleted_like_any_other(self, client: TestClient):
        """It used to 422 with "disable it instead". Rows are user-created now and an empty row list
        means "everything is off" rather than "resurrect the default", so the special case only made
        one row in the list inexplicably lack a Delete button."""
        from shortlist.server.db.models import Collection

        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")

        assert client.delete(f"/api/collections/{picked['id']}").status_code == 204
        with client.app.state.sessions() as session:
            assert session.query(Collection).filter_by(slug="picked").one_or_none() is None

    def test_deleting_a_row_returns_before_the_plex_cleanup_finishes(self, client: TestClient):
        """The Plex side is a per-user walk over every library — and for a shared row, a privacy pass
        across every account. Awaiting it held the request open for all of it, so the page sat on a
        spinner. The DB row is gone when this returns; the jobs are durable and visible meanwhile."""
        from shortlist.server.db.models import Job

        created = client.post("/api/collections", json={"name": "Temp Row"})
        assert created.status_code in (200, 201)

        assert client.delete(f"/api/collections/{created.json()['id']}").status_code == 204

        with client.app.state.sessions() as session:
            queued = session.query(Job).filter(Job.kind == "row.reconcile").all()
        assert queued, "the Plex removal must be QUEUED, not done inline"

    def test_validation_rejects_bad_enums(self, client: TestClient):
        assert client.post("/api/collections", json={"name": "X", "build": "nonsense"}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "media": "vinyl"}).status_code == 422

    def test_candidate_sources_round_trip_and_reject_unknown(self, client: TestClient):
        # Empty by default (inherit the global setting).
        created = client.post("/api/collections", json={"name": "Trakt Row"})
        assert created.status_code == 201 and created.json()["candidate_sources"] == []
        cid = created.json()["id"]
        # A per-row override round-trips through PATCH and GET (the client sends the full body).
        patched = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Trakt Row", "candidate_sources": ["trakt", "tmdb_discover"]},
        )
        assert patched.status_code == 200
        assert patched.json()["candidate_sources"] == ["trakt", "tmdb_discover"]
        # An unknown source id is rejected with a helpful 422, not silently stored.
        bad = client.post("/api/collections", json={"name": "Bad Row", "candidate_sources": ["imdb_magic"]})
        assert bad.status_code == 422

    def test_library_keys_round_trip(self, client: TestClient):
        # Empty by default (every library); a per-row selection round-trips as strings.
        created = client.post("/api/collections", json={"name": "4K Only"})
        assert created.status_code == 201 and created.json()["library_keys"] == []
        cid = created.json()["id"]
        patched = client.patch(f"/api/collections/{cid}", json={"name": "4K Only", "library_keys": ["3", "5"]})
        assert patched.status_code == 200
        assert patched.json()["library_keys"] == ["3", "5"]

    def test_slug_collision_gets_suffixed(self, client: TestClient):
        # Different names (duplicates are rejected) that slugify to the same base collide on slug.
        first = client.post("/api/collections", json={"name": "Date Night"}).json()
        second = client.post("/api/collections", json={"name": "Date-Night!"}).json()
        assert first["slug"] == "date_night"
        assert second["slug"] == "date_night_2"

    def test_duplicate_names_are_rejected(self, client: TestClient):
        assert client.post("/api/collections", json={"name": "Movie Night"}).status_code == 201
        assert client.post("/api/collections", json={"name": "Movie Night"}).status_code == 422


class TestRowEditsReachPlexDurably:
    """Editing a row is a Plex write, not just a config change — and it has to survive Plex being down.

    Every one of these used to be a bare `run_in_executor`: no retry, no record, and no check that a
    run wasn't writing to the same server at that moment. A Plex outage at the instant of the edit lost
    the work permanently, and nothing revisits a deleted or switched-off row.
    """

    def _fake_plex(self, monkeypatch, client, *, collections, explode=False):
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        deleted: list[str] = []
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(title="Movies")]
        plex.find_owned_collections.side_effect = lambda s, label: [
            SimpleNamespace(title=title) for (title, lbl) in collections if lbl == label
        ]
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append(c.title)
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())

        def build(**kw):
            if explode:
                raise RuntimeError("Plex is down")
            return ctx

        monkeypatch.setattr(client.app.state.run_service, "build_context", build)
        return deleted

    def _jobs(self, client: TestClient) -> list[dict]:
        return client.get("/api/system/jobs").json()

    def test_switching_a_row_off_takes_its_collections_down(self, client: TestClient, monkeypatch):
        """Nothing used to fire here. The next run removes a disabled row only for the users it
        PROCESSES, so anyone paused, disabled or restricted kept it indefinitely — and a row whose
        schedule is blank has no next run at all."""
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid = created.json()["id"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
        deleted = self._fake_plex(
            monkeypatch, client, collections=[("Hidden Gems" + row_marker(acct), f"shortlist_{uslug}")]
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Hidden Gems", "enabled": False})

        assert r.status_code == 200
        assert deleted == ["Hidden Gems" + row_marker(acct)]

    def test_switching_a_row_back_on_removes_nothing(self, client: TestClient, monkeypatch):
        """The reverse must not fire — it would delete the row it is meant to bring back."""
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Hidden Gems", "enabled": False})
        cid = created.json()["id"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
        deleted = self._fake_plex(
            monkeypatch, client, collections=[("Hidden Gems" + row_marker(acct), f"shortlist_{uslug}")]
        )

        client.patch(f"/api/collections/{cid}", json={"name": "Hidden Gems", "enabled": True})

        assert deleted == []

    def test_a_row_edited_twice_off_does_not_re_queue_the_removal(self, client: TestClient, monkeypatch):
        """A row that is already off has nothing to take down, and the row editor re-sends every field
        on each save."""
        created = client.post("/api/collections", json={"name": "Hidden Gems", "enabled": False})
        cid = created.json()["id"]
        self._fake_plex(monkeypatch, client, collections=[])

        client.patch(f"/api/collections/{cid}", json={"name": "Hidden Gems", "enabled": False})

        assert [j["kind"] for j in self._jobs(client)] == []

    def test_a_plex_outage_during_a_delete_leaves_a_retryable_job(self, client: TestClient, monkeypatch):
        """The whole reason these became jobs. Before, this work was simply lost: nothing revisits a
        deleted row, so its collections stayed on the server for ever."""
        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid = created.json()["id"]
        self._fake_plex(monkeypatch, client, collections=[], explode=True)

        assert client.delete(f"/api/collections/{cid}").status_code == 204

        job = next(j for j in self._jobs(client) if j["kind"] == "row.reconcile")
        assert job["status"] == "queued", "still queued means the worker will retry it"
        assert job["attempts"] == 1 and "Plex is down" in (job["error"] or "")

    def test_a_deletes_reconcile_can_still_find_the_row_after_the_row_is_gone(self, client: TestClient, monkeypatch):
        """A retry runs after the DB row has been deleted, so the title its collections were built under
        can no longer be looked up. It travels in the job payload for exactly that reason."""
        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid = created.json()["id"]
        self._fake_plex(monkeypatch, client, collections=[], explode=True)
        client.delete(f"/api/collections/{cid}")

        job = next(j for j in self._jobs(client) if j["kind"] == "row.reconcile")

        assert job["payload"]["template"] == "Hidden Gems"
        assert job["payload"]["slug"] == "hidden_gems"


class TestNarrowingARowsLibraries:
    """Narrowing a row is not the same as removing it — but it strands just as much.

    A row whose media goes "both" → movies, or that drops a library from its list, keeps whatever it
    already built in the libraries it walked away from. Delivery no longer targets them, so those
    collections are never refreshed, never removed, and re-promoted every run by promotion's no-spec
    fallback: a row switched to "movies only" kept a stale TV shelf up indefinitely.
    """

    def _plex(self, monkeypatch, client, *, collections):
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        deleted: list[tuple[str, str]] = []
        movies = SimpleNamespace(title="Movies", key="1", type="movie")
        shows = SimpleNamespace(title="TV", key="2", type="show")
        by_key = {"1": movies, "2": shows}
        plex = MagicMock()
        plex.sections.return_value = [movies, shows]
        plex.find_owned_collections.side_effect = lambda s, label: [
            SimpleNamespace(title=title, _section=str(s.key))
            for (title, lbl, key) in collections
            if lbl == label and key == str(s.key)
        ]
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append((c.title, c._section))
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)
        assert by_key  # both libraries exist, so the difference below is a real one
        return deleted

    def _row(self, client: TestClient):
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Gems", "media": "both"})
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            return created.json()["id"], user.slug, row_marker(user.plex_account_id)

    def test_narrowing_media_removes_only_the_library_it_left(self, client: TestClient, monkeypatch):
        cid, uslug, marker = self._row(client)
        deleted = self._plex(
            monkeypatch,
            client,
            collections=[("Gems" + marker, f"shortlist_{uslug}", "1"), ("Gems" + marker, f"shortlist_{uslug}", "2")],
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "media": "movie"})

        assert r.status_code == 200
        # The TV copy goes; the Movies one is still live and must survive. Removing both would delete
        # the row the owner just said they wanted.
        assert deleted == [("Gems" + marker, "2")]

    def test_narrowing_to_named_libraries_removes_the_dropped_one(self, client: TestClient, monkeypatch):
        cid, uslug, marker = self._row(client)
        deleted = self._plex(
            monkeypatch,
            client,
            collections=[("Gems" + marker, f"shortlist_{uslug}", "1"), ("Gems" + marker, f"shortlist_{uslug}", "2")],
        )

        client.patch(f"/api/collections/{cid}", json={"name": "Gems", "library_keys": ["1"]})

        assert deleted == [("Gems" + marker, "2")]

    def test_widening_a_row_removes_nothing(self, client: TestClient, monkeypatch):
        """The opposite direction adds libraries, which is a build — left to the next run's gated
        delivery. Removing anything here would delete a row the owner just asked to expand."""
        cid, uslug, marker = self._row(client)
        client.patch(f"/api/collections/{cid}", json={"name": "Gems", "media": "movie"})
        deleted = self._plex(monkeypatch, client, collections=[("Gems" + marker, f"shortlist_{uslug}", "1")])

        client.patch(f"/api/collections/{cid}", json={"name": "Gems", "media": "both"})

        assert deleted == []

    def test_an_unreadable_plex_removes_nothing(self, client: TestClient, monkeypatch):
        """Not knowing which libraries exist must mean "delete nothing", never "delete everything" —
        this is the one irreversible action on the path."""

        def explode(**kw):
            raise RuntimeError("Plex is down")

        cid, _uslug, _marker = self._row(client)
        monkeypatch.setattr(client.app.state.run_service, "build_context", explode)

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "media": "movie"})

        assert r.status_code == 200
        assert [j["kind"] for j in client.get("/api/system/jobs").json()] == []


class TestDeletingAPosterImage:
    """ "Delete the image" has to mean gone from Plex too, not just from Shortlist's store.

    Clearing the stored bytes used to be all this did, leaving `mode` as "upload" with nothing to
    upload — so the row kept the artwork already pushed to Plex, for ever, with nothing able to reach
    it: the row-editor path only reverts when a row that HAD a mode drops to none, and the mode never
    dropped.
    """

    def _reset_spy(self, monkeypatch):
        from shortlist.server.services import collection_reconcile as rec

        calls: list[tuple[str, str]] = []

        async def spy(state, *, slug, build, scope):
            calls.append((slug, scope))
            return [], None

        monkeypatch.setattr(rec, "run_poster_reset", spy)
        return calls

    def test_it_clears_the_mode_and_reverts_the_artwork_on_plex(self, client: TestClient, monkeypatch):
        created = client.post("/api/collections", json={"name": "Gems", "poster": {"mode": "text", "title": "Gems"}})
        cid, slug = created.json()["id"], created.json()["slug"]
        calls = self._reset_spy(monkeypatch)

        assert client.delete(f"/api/collections/{cid}/poster/image").status_code == 204

        assert client.get("/api/collections").json()
        row = next(c for c in client.get("/api/collections").json() if c["id"] == cid)
        assert row["poster"]["mode"] == "", "the mode must drop, or nothing can ever revert the artwork"
        assert calls == [(slug, "collection.poster")]

    def test_a_row_that_never_had_a_custom_poster_touches_plex_at_all(self, client: TestClient, monkeypatch):
        """Nothing was ever pushed, so there is nothing to revert — and a PMS round-trip per delete
        would be pure cost."""
        created = client.post("/api/collections", json={"name": "Gems"})
        calls = self._reset_spy(monkeypatch)

        assert client.delete(f"/api/collections/{created.json()['id']}/poster/image").status_code == 204

        assert calls == []


class TestBlockedSeedsApi:
    """The feature was half-built: the API existed, the frontend wrapper existed and was never
    called, and the list rendered bare TMDB ids. The empty state even pointed at a button that
    didn't exist."""

    def _uid(self, client: TestClient) -> int:

        with client.app.state.sessions() as session:
            return session.query(User).order_by(User.id).first().id

    def test_blocking_a_title_keeps_its_name(self, client: TestClient):
        """ "tmdb 346648" is a number nobody recognises — most of why nobody used this."""
        uid = self._uid(client)

        r = client.post(
            f"/api/users/{uid}/blocked-seeds",
            json={"tmdb_id": 346648, "title": "Paddington 2", "media_type": "movie", "year": 2017},
        )

        assert r.status_code == 200
        assert set(r.json()) == {"blocked_seeds"}
        assert r.json()["blocked_seeds"] == [
            {"tmdb_id": 346648, "title": "Paddington 2", "media_type": "movie", "year": 2017}
        ]

    def test_blocking_the_same_title_twice_does_not_duplicate_it(self, client: TestClient):
        uid = self._uid(client)
        client.post(f"/api/users/{uid}/blocked-seeds", json={"tmdb_id": 1, "title": "A"})
        body = client.post(f"/api/users/{uid}/blocked-seeds", json={"tmdb_id": 1, "title": "A (better name)"}).json()

        assert len(body["blocked_seeds"]) == 1
        assert body["blocked_seeds"][0]["title"] == "A (better name)", "a re-block should refresh the name"

    def test_an_existing_bare_int_list_still_works(self, client: TestClient):
        """An install that blocked titles before the shape changed must not need a migration."""

        uid = self._uid(client)
        with client.app.state.sessions() as session:
            user = session.get(User, uid)
            user.prefs = {**(user.prefs or {}), "blocked_seeds": [111, 222]}
            session.commit()

        # Reading: the old ids come back as records with no name rather than being dropped.
        listed = client.post(f"/api/users/{uid}/blocked-seeds", json={"tmdb_id": 333, "title": "New"}).json()
        assert {e["tmdb_id"] for e in listed["blocked_seeds"]} == {111, 222, 333}
        # A record built from a bare int is still a WHOLE record on the way out — the blank name and
        # the null year have to survive, or the picker can't tell "no title recorded" from a dropped field.
        legacy = next(e for e in listed["blocked_seeds"] if e["tmdb_id"] == 111)
        assert legacy == {"tmdb_id": 111, "title": "", "media_type": "", "year": None}

        # Removing one of the OLD ids works too.
        after = client.delete(f"/api/users/{uid}/blocked-seeds/111").json()
        assert set(after) == {"blocked_seeds"}
        assert {e["tmdb_id"] for e in after["blocked_seeds"]} == {222, 333}

    def test_unknown_user_404s_rather_than_writing_nothing_silently(self, client: TestClient):
        assert client.post("/api/users/9999/blocked-seeds", json={"tmdb_id": 1}).status_code == 404
        assert client.delete("/api/users/9999/blocked-seeds/1").status_code == 404

    def test_title_search_rejects_a_media_type_it_cannot_search(self, client: TestClient):
        assert client.get("/api/users/search/titles?q=dune&media_type=album").status_code == 422

    def test_title_search_of_nothing_is_an_empty_list_not_an_error(self, client: TestClient):
        assert client.get("/api/users/search/titles?q=%20").json() == []

    @pytest.mark.parametrize(
        ("media_type", "found", "expected"),
        [
            ("movie", {"id": 346648, "title": "Paddington 2", "release_date": "2017-11-10"}, 2017),
            # A show's date field has a different name, and a blank one must read as "no year" rather
            # than dropping the field the picker renders.
            ("show", {"id": 95396, "name": "Severance", "first_air_date": ""}, None),
        ],
    )
    def test_title_search_returns_what_the_block_picker_needs(
        self, client: TestClient, monkeypatch, media_type, found, expected
    ):
        tmdb = SimpleNamespace(search=lambda query, mt: found)
        monkeypatch.setattr(client.app.state.run_service, "build_requests_context", lambda: (None, tmdb))

        body = client.get(f"/api/users/search/titles?q=x&media_type={media_type}").json()

        assert set(body[0]) == {"tmdb_id", "title", "media_type", "year"}
        assert body[0] == {
            "tmdb_id": found["id"],
            "title": found.get("title") or found.get("name"),
            "media_type": media_type,
            "year": expected,
        }


class TestClearDeletedRows:
    """Removing the pick history of rows that no longer exist.

    Hiding them was the default (their numbers still count in the totals), but there was no way to
    actually be rid of them — so a throwaway test row lingered in the dashboard for ever.
    """

    def _seed(self, client: TestClient, slug: str, n: int = 3) -> int:
        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            for i in range(n):
                session.add(
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=1000 + i,
                        media_type="movie",
                        rating_key=1000 + i,
                        rank=i + 1,
                        collection_slug=slug,
                        title=f"T{i}",
                    )
                )
            session.commit()
            return uid

    def _live_row(self, client: TestClient, slug: str) -> None:
        """Create a row that genuinely EXISTS, so the Collection lookup is what protects it.

        Not DEFAULT_SLUG: that slug is protected by a hardcoded literal, so a test that uses it as its
        "live row" passes even if the `Collection` query is deleted outright — which is exactly the
        hole an earlier version of this class had.
        """
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            session.add(Collection(slug=slug, name=slug, enabled=True))
            session.commit()

    def test_it_lists_only_rows_that_no_longer_exist(self, client: TestClient):
        from shortlist.server.db.models import DEFAULT_SLUG

        self._seed(client, "zz_throwaway", n=4)
        self._live_row(client, "zz_live")
        self._seed(client, "zz_live", n=7)  # a row that really exists
        self._seed(client, DEFAULT_SLUG, n=2)  # the default row's slug
        self._seed(client, "", n=1)  # legacy picks recorded before rows had slugs

        listed = client.get("/api/report/deleted-rows").json()

        assert [r["slug"] for r in listed] == ["zz_throwaway"]
        assert listed[0]["picks"] == 4
        assert listed[0]["first_seen"] and listed[0]["last_seen"]

    def test_it_lists_the_biggest_first_and_says_nothing_when_there_is_nothing(self, client: TestClient):
        """The order is what the UI presents, so it is part of the contract."""
        assert client.get("/api/report/deleted-rows").json() == []

        self._seed(client, "zz_small", n=2)
        self._seed(client, "zz_big", n=9)

        assert [r["slug"] for r in client.get("/api/report/deleted-rows").json()] == ["zz_big", "zz_small"]

    def test_clearing_removes_that_history_and_nothing_else(self, client: TestClient):
        from shortlist.server.db.models import DEFAULT_SLUG, PickRow

        self._seed(client, "zz_throwaway", n=4)
        self._live_row(client, "zz_live")
        self._seed(client, "zz_live", n=7)
        self._seed(client, DEFAULT_SLUG, n=2)

        result = client.delete("/api/report/deleted-rows").json()

        assert result["cleared"] == 1 and result["picks"] == 4
        with client.app.state.sessions() as session:
            remaining = {slug for (slug,) in session.query(PickRow.collection_slug).distinct()}
        assert remaining == {"zz_live", DEFAULT_SLUG}, "the live rows' history must survive"

    def test_naming_a_live_rows_slug_deletes_nothing(self, client: TestClient):
        """The eligible set is recomputed server-side, so a client cannot ask us to purge a row that
        still exists — by accident or otherwise.

        Asserted against a REAL Collection: the whole guard is the `Collection` lookup, and a test
        that names DEFAULT_SLUG instead would pass with that lookup removed.
        """
        from shortlist.server.db.models import PickRow

        self._live_row(client, "zz_live")
        self._seed(client, "zz_live", n=3)

        result = client.delete("/api/report/deleted-rows?slug=zz_live").json()

        assert result == {"cleared": 0, "picks": 0, "slugs": []}
        with client.app.state.sessions() as session:
            assert session.query(PickRow).count() == 3

    def test_clear_all_spares_every_row_that_still_exists(self, client: TestClient):
        """`slug=` omitted means "clear the lot" — the branch with the most to lose if the eligible
        set is ever computed wrongly."""
        from shortlist.server.db.models import PickRow

        for slug in ("zz_live_a", "zz_live_b"):
            self._live_row(client, slug)
            self._seed(client, slug, n=4)
        self._seed(client, "zz_gone", n=2)

        result = client.delete("/api/report/deleted-rows").json()

        assert result["slugs"] == ["zz_gone"]
        with client.app.state.sessions() as session:
            assert session.query(PickRow).count() == 8, "both live rows keep their history"

    def test_one_slug_can_be_cleared_without_the_others(self, client: TestClient):
        from shortlist.server.db.models import PickRow

        self._seed(client, "zz_one", n=2)
        self._seed(client, "zz_two", n=5)

        client.delete("/api/report/deleted-rows?slug=zz_one")

        with client.app.state.sessions() as session:
            remaining = {slug for (slug,) in session.query(PickRow.collection_slug).distinct()}
        assert remaining == {"zz_two"}

    def test_it_never_touches_the_delivery_ledger(self, client: TestClient):
        """`deliveries` is what tells a cleanup which Plex collection is which row. Clearing it would
        strand a real collection on a real user's server with nothing left to remove it."""
        from shortlist.server.db.models import Delivery

        self._seed(client, "zz_throwaway", n=2)
        with client.app.state.sessions() as session:
            session.add(Delivery(collection_slug="zz_throwaway", user_slug="sarah", library_key="1", rating_key=99))
            session.commit()

        client.delete("/api/report/deleted-rows")

        with client.app.state.sessions() as session:
            assert session.query(Delivery).count() == 1

    def test_clearing_is_audited(self, client: TestClient):
        """ "Where did those numbers go" must be answerable afterwards (plex-safety rule 10)."""
        from shortlist.server.db.models import Event

        self._seed(client, "zz_throwaway", n=2)

        client.delete("/api/report/deleted-rows")

        with client.app.state.sessions() as session:
            event = session.query(Event).filter_by(scope="report.clear_deleted_rows").one()
        # Per-slug, not just a total: a "clear all" over six rows has to say which one's history went.
        assert event.message["rows"] == {"zz_throwaway": 2}
        assert event.message["picks"] == 2

    def test_nothing_to_clear_is_not_an_error(self, client: TestClient):
        assert client.delete("/api/report/deleted-rows").json()["cleared"] == 0

    def test_it_is_owner_only(self, client: TestClient):
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/report/deleted-rows").status_code == 401
        assert client.delete("/api/report/deleted-rows").status_code == 401


class TestRowEffectiveness:
    """`GET /api/collections/{id}/effectiveness` — is one row working?

    The value under test is the MATURITY rule. A pick only counts as a hit if it is watched within
    `HIT_WINDOW_DAYS`, so a rate computed over picks younger than that reads as failure for no
    reason but time — and this panel sits beside the settings someone would then go and change.
    """

    def _picks(self, client: TestClient, slug: str, specs: list[tuple[int, int, bool, str]]) -> None:
        """specs = (tmdb_id, days_ago, watched, library)."""
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            for tmdb_id, days_ago, watched, library in specs:
                created = now - timedelta(days=days_ago)
                session.add(
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=tmdb_id,
                        media_type="movie",
                        rating_key=tmdb_id,
                        rank=1,
                        collection_slug=slug,
                        library=library,
                        title=f"T{tmdb_id}",
                        created_at=created,
                        watched_at=created if watched else None,
                    )
                )
            session.commit()

    def _row_id(self, client: TestClient, slug: str = "picked") -> int:
        return next(c["id"] for c in client.get("/api/collections").json() if c["slug"] == slug)

    def test_a_row_that_never_delivered_says_so_rather_than_scoring_zero(self, client: TestClient):
        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["first_delivered_at"] is None
        assert body["matured"] is None
        assert body["delivered"] == 0

    def test_picks_too_young_to_judge_are_counted_but_not_scored(self, client: TestClient):
        """The whole point. Five picks delivered yesterday, none watched — that is 0%, and reporting
        it would send someone to change settings that were never the problem."""
        self._picks(client, "picked", [(i, 1, False, "Movies") for i in range(1, 6)])

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["delivered"] == 5, "young picks still count towards the size"
        assert body["matured"] is None, "but they must not produce a rate"
        assert body["first_delivered_at"] is not None

    def test_a_matured_cohort_is_scored_and_excludes_the_young(self, client: TestClient):
        # Four matured (2 watched), plus two delivered yesterday that must not dilute the rate.
        self._picks(
            client,
            "picked",
            [
                (1, 60, True, "Movies"),
                (2, 60, True, "Movies"),
                (3, 60, False, "Movies"),
                (4, 60, False, "Movies"),
                (5, 1, False, "Movies"),
                (6, 1, False, "Movies"),
            ],
        )

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["delivered"] == 6, "all time counts everything"
        assert body["matured"]["delivered"] == 4, "the score ignores picks that have not had their window"
        assert body["matured"]["watched"] == 2
        assert body["matured"]["rate"] == 0.5

    def test_each_library_is_scored_separately(self, client: TestClient):
        """A row across two libraries is two Plex collections, and the Movies half landing while the
        TV half does not is the most actionable thing this panel can say."""
        self._picks(
            client,
            "picked",
            [(1, 60, True, "Movies"), (2, 60, True, "Movies"), (3, 60, False, "TV Shows"), (4, 60, False, "TV Shows")],
        )

        by_library = {
            lib["library"]: lib
            for lib in client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()["per_library"]
        }

        assert by_library["Movies"]["rate"] == 1.0
        assert by_library["TV Shows"]["rate"] == 0.0

    def test_another_row_s_history_is_not_counted(self, client: TestClient):
        self._picks(client, "someone_else", [(1, 60, True, "Movies")])

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["delivered"] == 0

    def test_an_unknown_row_is_a_404(self, client: TestClient):
        assert client.get("/api/collections/9999/effectiveness").status_code == 404

    def test_the_runs_count_matches_the_run_list_the_tile_links_to(self, client: TestClient):
        """The Runs tile is a LINK to `/api/runs?collection=<slug>`, so the number on it has to be
        the length of that list. A tile reading 3 above a list of 1 is worse than no tile."""
        self._picks(client, "picked", [(1, 40, True, "Movies")])
        self._picks(client, "picked", [(2, 20, False, "Movies")])
        self._picks(client, "someone_else", [(3, 10, False, "Movies")])

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()
        listed = client.get("/api/runs", params={"collection": "picked"}).json()

        assert body["runs"] == 2, "one run per _picks call, and the other row's run is not ours"
        assert body["runs"] == len(listed), "the tile and the list it opens must agree"

    def test_the_runs_count_drops_a_run_that_has_been_pruned(self, client: TestClient):
        """`runs.retention` deletes old runs and leaves the picks behind with a null `run_id`
        (migration 0040). Both the count and the list it links to then stop claiming that run —
        the alternative is a tile that counts history nobody can open."""
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import Run
        from shortlist.server.services.run_persistence import prune_runs

        self._picks(client, "picked", [(1, 40, True, "Movies")])
        self._picks(client, "picked", [(2, 20, False, "Movies")])
        # Age the first run past retention and prune it the way the real job does, rather than
        # deleting the row by hand — the behaviour under test is `prune_runs` nulling `run_id`.
        with client.app.state.sessions() as session:
            oldest = session.query(Run).order_by(Run.id).first()
            oldest.started_at = datetime.now(UTC) - timedelta(days=400)
            session.commit()
            assert prune_runs(session, retention_months=1) == 1
            session.commit()

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()
        listed = client.get("/api/runs", params={"collection": "picked"}).json()

        assert body["runs"] == 1 == len(listed)
        assert body["delivered"] == 2, "the picks themselves outlive their run"

    def test_last_delivered_at_is_the_most_recent_delivery(self, client: TestClient):
        """`first_delivered_at` tells "never run" from "ran once"; this tells "ran last night" from
        "ran in March and has been idle since", which is the one a stalled row shows up in."""
        from datetime import UTC, datetime, timedelta

        self._picks(client, "picked", [(1, 60, False, "Movies"), (2, 3, False, "Movies")])

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["first_delivered_at"] < body["last_delivered_at"]
        assert body["last_delivered_at"].startswith((datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d"))
