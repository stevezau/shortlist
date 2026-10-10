"""The seeded default rows and their migration-era shape."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shortlist.engine.models import RowLimits
from shortlist.engine.rows import ROW_ORDERS
from shortlist.server.db.models import DEFAULT_SLUG, User
from shortlist.server.services.season_catalogue import load_catalogue
from shortlist.server.settings_store import SettingsStore
from tests.integration.api_collections_support import POSTER_KEYS, _plex_jobs

pytestmark = pytest.mark.integration


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
        assert [(job["kind"], job["payload"]) for job in _plex_jobs(client)] == [
            (
                "row.rename",
                {
                    "slug": "picked",
                    "new_template": "✨ {library_name} Handpicked",
                    "old_template": before,
                    "scope": "collection.rename",
                },
            )
        ]

    def test_saving_the_default_row_with_an_unchanged_name_does_no_plex_work(self, client: TestClient, monkeypatch):
        """A save that doesn't move the name (e.g. an enable toggle carrying the current name) must not
        touch Plex — the rename reconcile does real I/O and only fires on a real change."""
        client.put("/api/settings", json={"values": {"row.name_template": "✨ {library_name} Picked for You"}})
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        before = client.get("/api/system/jobs").json()
        # The editor round-trips the current template as the name — an unchanged value, so no reconcile.
        r = client.patch(
            f"/api/collections/{picked['id']}",
            json={"name": "✨ {library_name} Picked for You", "enabled": True},
        )
        assert r.status_code == 200
        before_ids = {job["id"] for job in before}
        assert not [job for job in _plex_jobs(client) if job["id"] not in before_ids], (
            "an unchanged default name must not reconcile onto Plex"
        )

    def test_default_row_size_and_name_follow_the_global_setting(self, client: TestClient, tmp_path):
        """The wizard/Settings set row.size and row.name_template; the default 'picked' row must
        deliver at those values, not a size frozen into the collection at migration time."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        client.put("/api/settings", json={"values": {"row.size": 10}})
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        assert next(s for s in specs if s.slug == "rewatch_row").watched_pct == 0.5

    def test_per_row_auto_user_tag_round_trips_and_reaches_the_spec(self, client: TestClient):
        """The seam a typo would hide in: a row's tag-by-person override has to survive the PATCH,
        the serializer AND the spec build, or it silently reads as "inherit the global" for ever."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Kids Row"})
        assert created.status_code == 201
        assert created.json()["req_auto_user_tag"] is None  # a new row inherits

        row_id = created.json()["id"]
        # False is a real answer ("never tag this row by person"), not "unset" — it must not be
        # coerced back to None on the way through, or the global would switch it straight on again.
        patched = client.patch(f"/api/collections/{row_id}", json={"name": "Kids Row", "req_auto_user_tag": False})
        assert patched.status_code == 200 and patched.json()["req_auto_user_tag"] is False

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        assert next(s for s in specs if s.slug == "kids_row").auto_user_tag is False
        # ...and the untouched default row still inherits, so one row's override reaches no other.
        assert next(s for s in specs if s.slug == "picked").auto_user_tag is None

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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        assert next(s for s in specs if s.slug == "fresh_row").refresh_days == 3

    def test_per_row_idle_hold_round_trips_and_reaches_the_spec(self, client: TestClient):
        """The other half of the cadence: how long a row waits when its owner watched nothing."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Patient Row", "idle_hold_days": 28})
        assert created.status_code == 201 and created.json()["idle_hold_days"] == 28
        assert client.post("/api/collections", json={"name": "X", "idle_hold_days": -1}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "idle_hold_days": 400}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.idle_hold_days": 400}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.idle_hold_days": 28}}).status_code == 200
        # 0 is a CHOICE ("never hold this row"), and it is also the shipped default — a bounds check
        # that rejected it would make the feature impossible to turn back off.
        assert client.put("/api/settings", json={"values": {"recommendations.idle_hold_days": 0}}).status_code == 200

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        assert next(s for s in specs if s.slug == "patient_row").idle_hold_days == 28
        # An untouched row still inherits, so one row's ceiling reaches no other.
        assert next(s for s in specs if s.slug == "picked").idle_hold_days is None

    def test_rewatch_cooldown_defaults_to_thirty_round_trips_and_reaches_the_spec(self, client: TestClient):
        """How long a rewatch row keeps a just-finished title out (#114)."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        plain = client.post("/api/collections", json={"name": "Old Favourites", "rewatch": True, "watched_pct": 1})
        assert plain.status_code == 201 and plain.json()["rewatch_cooldown_days"] == 30
        tuned = client.post(
            "/api/collections", json={"name": "Anything Goes", "rewatch": True, "rewatch_cooldown_days": 0}
        )
        assert tuned.status_code == 201 and tuned.json()["rewatch_cooldown_days"] == 0, "0 means no cooldown"
        assert client.post("/api/collections", json={"name": "X", "rewatch_cooldown_days": -1}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "rewatch_cooldown_days": 400}).status_code == 422
        patched = client.patch(
            f"/api/collections/{plain.json()['id']}", json={"name": "Old Favourites", "rewatch_cooldown_days": 90}
        )
        assert patched.status_code == 200 and patched.json()["rewatch_cooldown_days"] == 90, patched.text

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        assert next(s for s in specs if s.slug == "old_favourites").rewatch_cooldown_days == 90
        assert next(s for s in specs if s.slug == "anything_goes").rewatch_cooldown_days == 0

    def test_description_and_sort_title_prefix_round_trip_and_reach_the_spec(self, client: TestClient):
        """Issue #120. Empty by default — "leave that field on Plex alone" — so a row created without them
        never touches a summary or sort title another tool set."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        plain = client.post("/api/collections", json={"name": "Hidden Gems"})
        assert plain.status_code == 201
        assert (plain.json()["description"], plain.json()["sort_title_prefix"]) == ("", "")
        made = client.post(
            "/api/collections",
            json={"name": "Deep Cuts", "description": "Picked for {user}", "sort_title_prefix": "!010_"},
        )
        assert made.status_code == 201, made.text
        assert (made.json()["description"], made.json()["sort_title_prefix"]) == ("Picked for {user}", "!010_")
        assert client.post("/api/collections", json={"name": "X", "sort_title_prefix": "!" * 65}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "description": "d" * 2001}).status_code == 422

        # A trailing space is part of how a prefix sorts, so it is kept; whitespace alone is no value.
        patched = client.patch(
            f"/api/collections/{plain.json()['id']}",
            json={"name": "Hidden Gems", "description": "   ", "sort_title_prefix": "01 "},
        )
        assert patched.status_code == 200, patched.text
        assert (patched.json()["description"], patched.json()["sort_title_prefix"]) == ("", "01 ")
        cleared = client.patch(
            f"/api/collections/{made.json()['id']}",
            json={"name": "Deep Cuts", "description": "", "sort_title_prefix": ""},
        )
        assert (cleared.json()["description"], cleared.json()["sort_title_prefix"]) == ("", "")

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        gems = next(s for s in specs if s.slug == "hidden_gems")
        assert (gems.description, gems.sort_title_prefix) == ("", "01 ")

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
            specs = builder._build_rows(session, store, catalogue=load_catalogue(session))
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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        assert next(s for s in specs if s.slug == "because_row").max_seeds == 1
        # A row that never set one keeps None, so the engine falls back to its own budget.
        assert next(s for s in specs if s.slug == "picked").max_seeds is None

    def test_per_row_limits_round_trip_clear_and_reach_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        limits = {"max_runtime": 120, "min_year": 1990, "max_year": 2020, "min_rating": 7.5}
        created = client.post("/api/collections", json={"name": "Limit Row", **limits})
        assert created.status_code == 201
        assert {k: created.json()[k] for k in limits} == limits
        cid = created.json()["id"]
        listed = next(c for c in client.get("/api/collections").json() if c["id"] == cid)
        assert {k: listed[k] for k in limits} == limits

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        spec = next(s for s in specs if s.slug == "limit_row")
        assert (spec.max_runtime, spec.min_year, spec.max_year, spec.min_rating) == (120, 1990, 2020, 7.5)
        picked = next(s for s in specs if s.slug == "picked")
        assert not picked.limits().active

        cleared = {k: None for k in limits}
        patched = client.patch(f"/api/collections/{cid}", json={"name": "Limit Row", **cleared})
        assert {k: patched.json()[k] for k in limits} == cleared

    def test_an_int_min_rating_builds_the_same_spec_as_a_float_one(self, client: TestClient):
        from shortlist.server.db.models import Collection
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Rated Row", "min_rating": 7})
        assert created.status_code == 201
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            # Hand the builder a genuine int: SQLite hands back a float, so the cast is only exercised this way.
            row = session.query(Collection).filter_by(slug="rated_row").one()
            row.min_rating = 7
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
            assert type(row.min_rating) is int
        spec = next(s for s in specs if s.slug == "rated_row")
        assert isinstance(spec.min_rating, float)
        assert spec.limits().fingerprint() == RowLimits(min_rating=7.0).fingerprint()

    @pytest.mark.parametrize(
        "bad",
        [
            {"max_runtime": 0},
            {"max_runtime": 601},
            {"min_year": 1869},
            {"min_year": 2101},
            {"max_year": 1869},
            {"max_year": 2101},
            {"min_rating": -0.1},
            {"min_rating": 10.1},
        ],
    )
    def test_a_limit_outside_its_bounds_is_rejected(self, client: TestClient, bad: dict):
        assert client.post("/api/collections", json={"name": "X", **bad}).status_code == 422

    def test_a_limit_at_its_bounds_is_accepted(self, client: TestClient):
        ok = {"max_runtime": 1, "min_year": 1870, "max_year": 2100, "min_rating": 0}
        assert client.post("/api/collections", json={"name": "Edge Row", **ok}).status_code == 201

    def test_min_year_after_max_year_is_rejected_naming_both_fields(self, client: TestClient):
        response = client.post("/api/collections", json={"name": "X", "min_year": 2010, "max_year": 2000})
        assert response.status_code == 422
        assert "min_year" in response.text and "max_year" in response.text

    def test_patch_year_order_is_judged_against_the_stored_row(self, client: TestClient):
        cid = client.post("/api/collections", json={"name": "Years Row", "min_year": 1990, "max_year": 2000}).json()[
            "id"
        ]
        url = f"/api/collections/{cid}"

        late_min = client.patch(url, json={"name": "Years Row", "min_year": 2010})
        assert late_min.status_code == 422
        assert "earliest year" in late_min.text
        early_max = client.patch(url, json={"name": "Years Row", "max_year": 1980})
        assert early_max.status_code == 422
        assert "earliest year" in early_max.text

        both = client.patch(url, json={"name": "Years Row", "min_year": 2010, "max_year": 2020})
        assert both.status_code == 200
        assert (both.json()["min_year"], both.json()["max_year"]) == (2010, 2020)

        cleared = client.patch(url, json={"name": "Years Row", "max_year": None})
        assert cleared.status_code == 200 and cleared.json()["max_year"] is None
        assert client.patch(url, json={"name": "Years Row", "min_year": 2050}).status_code == 200

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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
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

            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        spec = next(s for s in specs if s.slug == "top_row")
        assert spec.placement == "library"
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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
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
        assert ok.json()["hub_anchor"]["2"] == {
            "anchor": "",
            "row": picked,
            "before": False,
            "top": False,
            "enabled": True,
        }
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
        assert created.json()["hub_anchor"] == {
            "2": {"anchor": "New Series", "row": "", "before": True, "top": False, "enabled": True}
        }
        # A blank anchor with no top is rejected by the shape.
        blank = client.post("/api/collections", json={"name": "X", "hub_anchor": {"2": {"anchor": ""}}})
        assert blank.status_code == 422
        # A 'top' entry needs no anchor.
        top = client.post("/api/collections", json={"name": "Top Gems", "hub_anchor": {"2": {"top": True}}})
        assert top.status_code == 201 and top.json()["hub_anchor"]["2"]["top"] is True

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )

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
            built = builder._build_rows(session, store, catalogue=load_catalogue(session))

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

    @pytest.mark.parametrize(
        "template",
        [
            pytest.param("Popular Here", id="static-title"),
            # Retired anyway: `remove_row` matches an unrenderable title by its ledger key ONLY, and leaves a copy
            # the ledger does not name alone — so this cannot reach the default row the gate above protects.
            pytest.param("Because you watched {top_seed}", id="top-seed-title"),
        ],
    )
    def test_a_row_switched_to_shared_retires_everyones_per_person_copy(self, client: TestClient, template: str):
        """The switch's own removal can miss a person's copy (the job gives up), and nothing else ever looked
        for it again: it stayed on that person's Home for good. Every shared row's per-person copies are handed
        to the nightly run for removal — for everyone, since whoever holds one was in the row's audience THEN."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Popular Here"})
        cid, slug = created.json()["id"], created.json()["slug"]
        switched = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Popular Here", "name_template": template, "build": "shared", "audience": "subset"},
        )
        assert switched.status_code == 200, switched.text

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            retired = builder._retired_rows(session, SettingsStore(session, client.app.state.secrets))

        copy = next((s for s in retired if s.slug == slug), None)
        assert copy is not None, "a shared row's per-person copies must be queued for removal"
        assert not copy.shared, "the copies are per-person collections, under each person's own label"
        assert copy.audience is None
        assert copy.name_template == template

    def test_a_disabled_shared_row_retires_no_per_person_copy(self, client: TestClient):
        """Retired specs are indexed where they live, so a disabled shared row's libraries would stay in every
        run's index — watches there seeding everyone's picks — for the sake of a copy that stays private."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Popular Here"})
        cid, slug = created.json()["id"], created.json()["slug"]
        switched = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Popular Here", "build": "shared", "audience": "subset", "enabled": False},
        )
        assert switched.status_code == 200, switched.text

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            retired = builder._retired_rows(session, SettingsStore(session, client.app.state.secrets))

        assert slug not in {s.slug for s in retired}

    def test_the_default_row_switched_to_shared_retires_copies_titled_from_the_global_template(
        self, client: TestClient
    ):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        default = next(c for c in client.get("/api/collections").json() if c["slug"] == DEFAULT_SLUG)
        switched = client.patch(
            f"/api/collections/{default['id']}", json={"name": default["name"], "build": "shared", "audience": "subset"}
        )
        assert switched.status_code == 200, switched.text

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            retired = builder._retired_rows(session, SettingsStore(session, client.app.state.secrets))

        copy = next(s for s in retired if s.slug == DEFAULT_SLUG)
        assert copy.name_template == "", "empty: each person's copy renders the global template, or their own"

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
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
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

        # A non-image upload is rejected; Pillow is in the dev extras, so CI always can tell.
        bad = client.post(
            f"/api/collections/{cid}/poster/upload", files={"file": ("x.png", b"not an image", "image/png")}
        )
        assert bad.status_code == 422

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
