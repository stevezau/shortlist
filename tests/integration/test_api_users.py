"""API contract tests: the users list, the plex.tv roster sync, and per-user rows."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from shortlist.server.auth import SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Server, User
from shortlist.server.settings_store import SettingsStore
from tests.integration.conftest import OWNER_ID, OWNER_JSON

pytestmark = pytest.mark.integration

# Response KEY SETS, spelled out. These endpoints declare Pydantic response models, and such a model
# does not merely describe a payload — it FILTERS it: a key the model forgets to declare is dropped
# on the way out, silently, blanking whatever the page read from it. Which is why every one of them
# sets `extra="allow"` (see `api/serializers.PASSTHROUGH`), and why these assertions name every key
# rather than the two fields a test happens to care about: strip that config, or add a default that
# INVENTS a key the handler never sent, and these are what fail instead of a page in production.

#: Every key `serializers.user_dict` renders — the shape of both `GET /users` and `PATCH /users/{id}`.
USER_KEYS = {
    "id",
    "plex_account_id",
    "username",
    "slug",
    "nickname",
    "friendly_name",
    "display_name",
    "avatar_url",
    "user_type",
    "restricted",
    "restriction_profile",
    "enabled",
    "cold_start",
    "request_tag",
    "prefs",
    "history_depth",
    "last_run_at",
    "hit_rate",
    "preview_titles",
    "unhidden_rows",
    "departed",
}

#: Every key `serializers.pick_dict` renders — shared by `/users/{id}/runs` and `/users/{id}/rows`.
PICK_KEYS = {
    "rank",
    "title",
    "reason",
    "media_type",
    "collection_slug",
    "library",
    "section_key",
    "seed_title",
    "sources",
    "affinity",
    "year",
    "rating",
}


class TestUsersApi:
    def test_list_and_patch(self, client: TestClient):
        users = client.get("/api/users").json()
        assert [u["username"] for u in users] == ["mike", "sarah"]
        assert set(users[0]) == USER_KEYS
        target = next(u for u in users if u["username"] == "mike")
        # NB: `row_size`/`max_rating` used to live in prefs and were read by NOTHING. Per-person row
        # size is a row override (PUT /users/{id}/rows/{cid}); a maturity cap filtered no content at
        # all, so it was removed rather than left looking enforced.
        r = client.patch(f"/api/users/{target['id']}", json={"enabled": True, "prefs": {"excluded_genres": ["Horror"]}})
        assert r.status_code == 200
        assert set(r.json()) == USER_KEYS, "the PATCH renders the same person as the list"
        assert r.json()["enabled"] is True
        assert r.json()["prefs"]["excluded_genres"] == ["Horror"]

    def test_the_list_reports_rows_this_account_can_see_that_are_not_its_own(self, client: TestClient):
        """Plex refuses a hide-list for a managed account with a parental profile, so for those accounts
        nothing hides other people's rows. The last run MEASURED how many each such account can see; the
        Users screen is where the owner would look, so the count has to reach it.

        Keyed by username, because that is the only identifier the engine has when it writes the run —
        the slug is Shortlist's, and a nickname can change between the run and this request."""
        from datetime import UTC, datetime

        from shortlist.server.db.models import Run

        with client.app.state.sessions() as session:
            session.add(
                Run(
                    status="ok",
                    trigger="manual",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    stats={"unhideable_rows": {"mike": [21, 22, 23]}},
                )
            )
            session.commit()

        users = {u["username"]: u for u in client.get("/api/users").json()}

        assert users["mike"]["unhidden_rows"] == 3
        assert users["sarah"]["unhidden_rows"] == 0, "an account Plex does filter is not implicated"

    def test_no_run_yet_is_reported_as_nothing_seen_not_as_a_missing_field(self, client: TestClient):
        """Absent evidence renders as zero, never as null: the SPA branches on the number, and a null
        would make an unmeasured account look the same as an exposed one."""
        assert all(u["unhidden_rows"] == 0 for u in client.get("/api/users").json())

    def test_prefs_pass_through_whatever_an_install_has_accrued(self, client: TestClient):
        """`prefs` is free-form JSON: which keys exist varies by DATA, not by branch (`history_depth`
        appears after a watch sync, `paused` only once someone is paused, and an old install's
        `blocked_seeds` is a list of bare ints). The response model must not narrow it — a declared
        shape here would either drop the keys it doesn't know or invent the ones it does."""
        with client.app.state.sessions() as session:
            user = session.query(User).filter_by(username="sarah").one()
            user.prefs = {"history_depth": 7, "blocked_seeds": [111, 222], "something_new": {"deep": True}}
            session.commit()
            user_id = user.id

        listed = next(u for u in client.get("/api/users").json() if u["id"] == user_id)

        assert listed["prefs"] == {
            "history_depth": 7,
            "blocked_seeds": [111, 222],  # the legacy bare-int shape survives the round trip
            "something_new": {"deep": True},
        }

    def test_a_nickname_changes_the_row_title_but_never_the_label(self, client: TestClient):
        """Plex usernames are often a handle nobody uses, and `{user}` put it on a Home screen (#4).
        The slug — and so `shortlist_<slug>`, which every other account's share filter excludes —
        must not move, or the old exclusions would point at nothing and the row would go public."""
        target = next(u for u in client.get("/api/users").json() if u["username"] == "sarah")
        assert target["display_name"] == "sarah"

        r = client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"})

        assert r.status_code == 200
        assert r.json()["nickname"] == "Sarah B"
        assert r.json()["display_name"] == "Sarah B"
        assert r.json()["slug"] == target["slug"], "the label must not move when someone is renamed"

    def test_a_nickname_change_retitles_the_collection_already_on_plex(self, client: TestClient, monkeypatch):
        """`{user}` renders the display name, so renaming someone gives their collections a title no
        future run will ever write. The next run then builds a SECOND one under the new name and the
        old one lives on, labelled and promoted, with nothing able to address it.

        The reconcile therefore renders the OLD name to find the collection and the NEW one to set —
        rendering the new name on both sides would match nothing and silently do nothing."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import EngineConfig

        target = next(u for u in client.get("/api/users").json() if u["username"] == "sarah")
        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("row.name_template", "{user}'s Picks")
            session.commit()

        renames: list[tuple[str, str]] = []
        marker = row_marker(target["plex_account_id"])
        col = MagicMock(title="sarah's Picks" + marker)
        col.editTitle.side_effect = lambda new: renames.append((col.title, new))
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(title="Movies")]
        plex.find_owned_collections.side_effect = lambda s, label: [col] if label == "shortlist_sarah" else []
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)

        assert client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"}).status_code == 200

        assert renames == [("sarah's Picks" + marker, "Sarah B's Picks" + marker)]

    def test_clearing_a_nickname_falls_back_to_tautullis_name_then_plex(self, client: TestClient):
        from shortlist.server.db.models import User

        target = next(u for u in client.get("/api/users").json() if u["username"] == "sarah")
        with client.app.state.sessions() as session:
            session.get(User, target["id"]).friendly_name = "Sazza"
            session.commit()

        client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"})
        assert self._one(client, target["id"])["display_name"] == "Sarah B"

        cleared = client.patch(f"/api/users/{target['id']}", json={"nickname": ""}).json()

        assert cleared["nickname"] == ""
        assert cleared["display_name"] == "Sazza", "a blank nickname falls back to Tautulli's name"

    def test_a_nickname_that_collides_with_someone_else_is_refused(self, client: TestClient):
        """`{user}` renders display_name, and only the USERNAME is unique on Plex. Two people
        resolving to one display name ask for two collections with one title in one library, which
        PMS refuses — so that person's row would fail every night with a generic-looking Plex
        error. Caught at the point of entry instead, where it can be explained."""
        users = client.get("/api/users").json()
        sarah = next(u for u in users if u["username"] == "sarah")
        other = next(u for u in users if u["id"] != sarah["id"])
        client.patch(f"/api/users/{sarah['id']}", json={"nickname": "The Boss"})

        r = client.patch(f"/api/users/{other['id']}", json={"nickname": "the boss"})

        assert r.status_code == 409, "case differences still collide — Plex titles are not case-sensitive"
        assert "already shows up as" in r.json()["detail"]
        assert self._one(client, other["id"])["nickname"] == "", "the rejected name must not be stored"

    @pytest.mark.parametrize("source", ["friendly_name", "username"])
    def test_a_clash_is_caught_whichever_name_the_other_person_shows_under(self, client: TestClient, source: str):
        """display_name has three sources (nickname → Tautulli → Plex) and any of them can be the
        thing you collide with — the Tautulli one especially, now that a sync adopts it."""
        from shortlist.server.db.models import User

        users = client.get("/api/users").json()
        sarah = next(u for u in users if u["username"] == "sarah")
        other = next(u for u in users if u["id"] != sarah["id"])
        if source == "friendly_name":
            with client.app.state.sessions() as session:
                session.get(User, sarah["id"]).friendly_name = "Sazza"
                session.commit()
            taken = "Sazza"
        else:
            taken = sarah["username"]

        r = client.patch(f"/api/users/{other['id']}", json={"nickname": taken})

        assert r.status_code == 409
        assert f"already shows up as “{taken}”" in r.json()["detail"], "the message names what THEY show as"

    def test_clearing_a_nickname_into_someone_elses_name_is_also_refused(self, client: TestClient):
        """Clearing is the one input that reaches a colliding name without anyone typing it."""
        from shortlist.server.db.models import User

        users = client.get("/api/users").json()
        sarah = next(u for u in users if u["username"] == "sarah")
        other = next(u for u in users if u["id"] != sarah["id"])
        client.patch(f"/api/users/{other['id']}", json={"nickname": "Distinct Name"})
        with client.app.state.sessions() as session:
            session.get(User, other["id"]).friendly_name = sarah["username"]
            session.commit()

        r = client.patch(f"/api/users/{other['id']}", json={"nickname": ""})

        assert r.status_code == 409

    def test_a_nickname_can_be_re_saved_without_colliding_with_itself(self, client: TestClient):
        target = next(u for u in client.get("/api/users").json() if u["username"] == "sarah")
        client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"})

        r = client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"})

        assert r.status_code == 200

    def _one(self, client: TestClient, user_id: int) -> dict:
        return next(u for u in client.get("/api/users").json() if u["id"] == user_id)

    def test_watch_history_depth_is_read_from_prefs_not_gated_on_a_run(self, client: TestClient):
        """`history_depth` is refreshed for EVERY enabled user by the daily watch sync (the share-token
        read returns one item per distinct title, so a binge is one). It must surface without waiting
        for a run to PROCESS someone — a skipped or never-run user used to read "0 titles" forever (the
        beta bug that reported 0 for all 42 accounts while the log showed watches synced)."""
        from shortlist.server.db.models import User

        with client.app.state.sessions() as session:
            user = session.query(User).filter_by(username="sarah").one()
            # The sync writes the distinct-title count the share-token source returned; no run yet.
            user.prefs = {**(user.prefs or {}), "history_depth": 2}
            session.commit()
            user_id = user.id

        listed = next(u for u in client.get("/api/users").json() if u["id"] == user_id)
        assert listed["history_depth"] == 2, "a binge is one title, and it must not need a run to show"

        # The PATCH response carries the same real number, not a stale zero.
        patched = client.patch(f"/api/users/{user_id}", json={"enabled": True}).json()
        assert patched["history_depth"] == 2

    def test_patch_unknown_user_404(self, client: TestClient):
        assert client.patch("/api/users/9999", json={"enabled": True}).status_code == 404

    def test_history_carries_every_field_the_watch_source_records(self, client: TestClient, monkeypatch):
        """Both media cells: a movie has no season/episode, an episode watch carries all three. The
        episode detail is the whole reason the list is readable ("S2E4 — The Ceiling"), and `tmdb_id`
        is the only thing "block this seed" can key on — none of them may be filtered out."""
        uid = next(u["id"] for u in client.get("/api/users").json() if u["slug"] == "sarah")
        watches = [
            {
                "title": "Paddington 2",
                "tmdb_id": 346648,
                "media_type": "movie",
                "watched_at": "2026-07-30T09:00:00+00:00",
                "year": 2017,
                "season": None,
                "episode": None,
                "episode_title": None,
            },
            {
                "title": "Severance",
                "tmdb_id": None,  # nothing with a tmdb:// GUID — the UI must not offer to block it
                "media_type": "show",
                "watched_at": "2026-07-29T21:15:00+00:00",
                "year": 2022,
                "season": 2,
                "episode": 4,
                "episode_title": "Woe's Hollow",
            },
        ]
        monkeypatch.setattr(client.app.state.run_service, "user_history", lambda user_id, limit: watches)

        body = client.get(f"/api/users/{uid}/history").json()

        assert set(body[0]) == {
            "title",
            "tmdb_id",
            "media_type",
            "watched_at",
            "year",
            "season",
            "episode",
            "episode_title",
        }
        assert body == watches

    def test_disabling_a_user_removes_their_rows_from_plex(self, client: TestClient, monkeypatch):
        """Turning a user off deletes every collection under their label — not just stops delivery.

        One user_type suffices: cleanup is by the user's whole label (displays=None), so it's agnostic
        to shared/managed/owner; enable and no-op transitions are guarded by `user.enabled and False`.
        """
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import EngineConfig
        from shortlist.server.db.models import User

        target = next(u for u in client.get("/api/users").json() if u["username"] == "mike")
        client.patch(f"/api/users/{target['id']}", json={"enabled": True})  # ensure the transition is off->on->off
        with client.app.state.sessions() as session:
            slug = session.get(User, target["id"]).slug

        deleted: list[str] = []
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(title="Movies")]
        plex.find_owned_collections.side_effect = lambda s, label: (
            [SimpleNamespace(title="✨ Picked for You" + row_marker(0))] if label == f"shortlist_{slug}" else []
        )
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append(c.title)
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)

        r = client.patch(f"/api/users/{target['id']}", json={"enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False
        assert len(deleted) == 1  # their collection was removed by their whole label

    def test_set_all_users_enabled_toggles_everyone_and_cleans_up_on_disable(self, client: TestClient, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        # Record the actual Plex removals, keyed by the shortlist label they came in on.
        removed_labels: list[str] = []
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(title="Movies")]
        plex.find_owned_collections.side_effect = lambda section, label: [SimpleNamespace(title=label, _label=label)]
        plex.delete_owned_collection.side_effect = lambda collection, prefix: removed_labels.append(collection._label)
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)

        # Enable all -> everyone on, and NOTHING removed from Plex.
        r = client.post("/api/users/set-enabled", json={"enabled": True})
        assert r.status_code == 200 and r.json()["enabled"] is True
        assert set(r.json()) == {"updated", "cleaned", "enabled"}
        assert all(u["enabled"] for u in client.get("/api/users").json())
        assert removed_labels == []  # enabling never triggers a Plex write

        # Disable all -> everyone off, and BOTH users' rows actually removed (by their own label).
        r = client.post("/api/users/set-enabled", json={"enabled": False})
        assert r.status_code == 200 and r.json()["cleaned"] == 2
        assert set(removed_labels) == {"shortlist_sarah", "shortlist_mike"}
        assert not any(u["enabled"] for u in client.get("/api/users").json())


class TestUserSync:
    """`POST /users/sync` — the roster from plex.tv, PLUS the owner, who is never in that list.

    Covers the `user_type` matrix's owner cell: without this the person running Shortlist has no
    user row at all, so a one-person server gets no rows (issue #1).
    """

    @pytest.fixture
    def plextv(self, client: TestClient):
        """Both plex.tv reads at the HTTP boundary, so the response SHAPES are under test too —
        the roster from the recorded fixture, the owner from the payload above."""

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "owner-token")
            session.commit()

        users_xml = (Path(__file__).parent.parent / "fixtures" / "plextv_users.xml.txt").read_text()
        with respx.mock:
            roster = respx.get("https://plex.tv/api/users").mock(return_value=httpx.Response(200, text=users_xml))
            whoami = respx.get("https://plex.tv/api/v2/user").mock(
                return_value=httpx.Response(200, json=dict(OWNER_JSON))
            )
            yield SimpleNamespace(roster=roster, whoami=whoami)

    def _users(self, client: TestClient) -> dict[str, dict]:
        return {u["username"]: u for u in client.get("/api/users").json()}

    def test_a_tautulli_rename_triggers_the_same_row_reconcile_a_nickname_does(
        self, client: TestClient, plextv, monkeypatch
    ):
        """`{user}` renders the friendly name, so a Tautulli rename changes every row title — and the
        collections already on Plex still carry the old one. Without this reconcile a multi-row user
        keeps the stale copy alongside the new one forever: `remove_row` matches by rendered title,
        so no sweep ever collects it."""
        from shortlist.server.services import user_sync

        calls: list = []

        async def spy(state, was_called):
            if was_called:  # a no-op call with nothing renamed is not Plex work
                calls.append(was_called)

        monkeypatch.setattr(user_sync, "rename_after_nickname", spy)
        monkeypatch.setattr(
            user_sync.TautulliClient, "friendly_names", lambda self: {555000100: "Sazza"}, raising=False
        )

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("tautulli.url", "http://tautulli:8181")
            session.commit()

        client.post("/api/users/sync")

        assert self._users(client)["sarah"]["display_name"] == "Sazza"
        assert len(calls) == 1, "a changed display name must reconcile the rows already on Plex"
        # The PREVIOUS name goes with it. It is what their collections are still titled with, and
        # rendering `{user}` from the NEW name on both sides matches nothing — leaving the stale
        # collection in place for the next run to duplicate.
        assert calls[0]["sarah"] == "sarah"

    def test_sync_streams_a_fetch_phase_then_a_save_bar_and_a_finish(self, client: TestClient, plextv, monkeypatch):
        """The Tools page bar reads these events: one indeterminate `fetch`, a determinate `save`
        count, then `sync.finished` echoing the same counts the POST returns."""
        published: list[tuple[str, dict]] = []
        real_publish = client.app.state.bus.publish
        monkeypatch.setattr(
            client.app.state.bus,
            "publish",
            lambda event, data: (published.append((event, data)), real_publish(event, data))[0],
        )

        body = client.post("/api/users/sync").json()

        events = [(e, d) for e, d in published if e in ("sync.progress", "sync.finished")]
        assert all(d["kind"] == "users" for _, d in events), "every sync event must be tagged for the users card"
        phases = [d.get("phase") for e, d in events if e == "sync.progress"]
        assert phases[0] == "fetch", "the opaque plex.tv call is announced as an indeterminate fetch first"
        assert "save" in phases, "the roster upsert drives a determinate save bar"
        save_events = [d for e, d in events if e == "sync.progress" and d.get("phase") == "save"]
        assert save_events[-1]["done"] == save_events[-1]["total"], "the save bar reaches 100%"
        # The finish event carries the same numbers the HTTP response does, so the bar can settle on them.
        finished = next(d for e, d in events if e == "sync.finished")
        assert finished["ok"] is True
        assert {k: finished[k] for k in ("added", "updated", "total")} == {
            k: body[k] for k in ("added", "updated", "total")
        }

        # These payloads are declared in `api/schemas_events.py` so the SPA can generate them rather
        # than hand-write them — and nothing validates an SSE frame at runtime, so the declaration is
        # only as honest as this. Validating the REAL frames catches a shape that drifted; comparing
        # the key sets catches a model that describes a field the publisher does not send (or misses
        # one it does).
        from shortlist.server.api.schemas_events import SyncFinishedEvent, SyncProgressEvent

        for event, data in events:
            model = SyncProgressEvent if event == "sync.progress" else SyncFinishedEvent
            assert set(model.model_validate(data).model_dump(exclude_unset=True)) == set(data), (event, data)

    def test_a_sync_that_changes_no_name_does_no_plex_work(self, client: TestClient, plextv, monkeypatch):
        """The reconcile does Plex I/O — it must not fire on every routine sync."""
        from shortlist.server.services import user_sync

        calls: list = []

        async def spy(state, was_called):
            if was_called:  # a no-op call with nothing renamed is not Plex work
                calls.append(was_called)

        monkeypatch.setattr(user_sync, "rename_after_nickname", spy)

        client.post("/api/users/sync")  # the fixture renames one account, so this one DOES reconcile
        calls.clear()
        client.post("/api/users/sync")

        assert calls == []

    def test_a_sync_that_finds_new_accounts_queues_the_share_filter_pass(self, client: TestClient, plextv):
        """A brand-new account has NO excludes while every existing row is already on Shared Home,
        so until its filter is written it sees EVERY user's private row. Waiting for the nightly run
        leaves that open for up to a day.

        Asserted at the JOB boundary, not by spying on the helper: the whole mechanism lives inside
        that helper, so mocking it would let the enqueue be deleted with the test still green — and
        the leak would be back.
        """
        client.post("/api/users/sync")  # first sync -> the whole roster is new

        # `sync.users` is in the list because the endpoint goes through the queue now — that is the
        # point: it is a WRITER, so it must take the writer lock and defer to an in-flight run like
        # every other writer. `privacy.sync` is the pass it queues on finding new accounts.
        assert sorted(j["kind"] for j in client.get("/api/system/jobs").json()) == [
            "privacy.sync",
            "sync.users",
        ]

    def test_a_sync_that_adds_nobody_writes_no_filters(self, client: TestClient, plextv, monkeypatch):
        """Filter writes are throttled plex.tv traffic across every account — a routine no-change
        sync must not trigger a server-wide pass."""
        from shortlist.server.services import user_sync

        calls: list = []

        async def spy(state, added):
            calls.append(added)

        monkeypatch.setattr(user_sync, "_hide_existing_rows_from_new_accounts", spy)

        client.post("/api/users/sync")
        calls.clear()
        client.post("/api/users/sync")  # everyone already known

        assert calls == []

    def test_a_failure_to_write_the_new_filters_never_fails_the_sync(self, client: TestClient, plextv, monkeypatch):
        """The accounts are already saved and the nightly run is still the backstop, so a Plex
        outage here must not fail the sync the operator asked for."""

        def explode(state, dry_run):
            raise RuntimeError("Plex is down")

        monkeypatch.setattr(client.app.state.run_service, "build_context", explode)

        assert client.post("/api/users/sync").status_code == 200

    def test_a_home_read_blip_never_wipes_a_stored_restriction_profile(self, client: TestClient, plextv, monkeypatch):
        """`/api/home/users` is a best-effort enrichment: when it fails, every profile comes back "".
        Saving that blindly WIPED correct values for everyone — the UI badge vanished and rows got
        built for parental-controlled kids until the next sync happened to heal it.

        A profile we could not read is unknown, and unknown must not overwrite what we already knew.
        """
        import shortlist.engine.clients.plextv as plextv_mod
        from shortlist.server.db.models import User

        # A healthy Home read: the kid's profile is known and gets stored.
        monkeypatch.setattr(
            plextv_mod.PlexTvClient, "home_restriction_profiles", lambda self: {555000200: "little_kid"}
        )
        monkeypatch.setattr(plextv_mod.PlexTvClient, "home_profile_known", lambda self, account_id: True)
        client.post("/api/users/sync")
        with client.app.state.sessions() as session:
            kid = session.query(User).filter_by(plex_account_id=555000200).one()
            assert kid.restriction_profile == "little_kid", "precondition: the profile was stored"

        # Now the Home endpoint goes down. list_users() still returns everyone, profiles all blank.
        monkeypatch.setattr(plextv_mod.PlexTvClient, "home_restriction_profiles", lambda self: {})
        monkeypatch.setattr(plextv_mod.PlexTvClient, "home_profile_known", lambda self, account_id: False)

        assert client.post("/api/users/sync").status_code == 200

        with client.app.state.sessions() as session:
            kid = session.query(User).filter_by(plex_account_id=555000200).one()
            assert kid.restriction_profile == "little_kid", "a failed read must not erase a known profile"

    def test_a_sync_pressed_during_a_run_waits_instead_of_renaming_mid_converge(self, client: TestClient, monkeypatch):
        """`sync.users` is a WRITER — it renames Shortlist collections when a display name drifts.

        This endpoint used to call it directly, bypassing the two things that make it safe: the claim
        gate that refuses to start a writer while a run is in flight, and the writer lock. A rename
        landing mid-converge makes a live collection look orphaned to a run matching by rendered
        title, and converge DELETES orphans (jobs-and-runs-design.md §12).
        """
        from shortlist.server.db.models import Job

        # Exactly what `_plex_busy` reads.
        monkeypatch.setattr(client.app.state.run_service, "is_running", lambda: True)

        r = client.post("/api/users/sync")

        assert r.status_code == 200
        assert r.json()["queued"] is True, "a sync pressed during a run must defer, not run now"
        with client.app.state.sessions() as session:
            job = session.query(Job).filter(Job.kind == "sync.users").one()
        assert job.status == "queued", "the job must still be waiting, not have run against a live run"
        assert job.started_at is None

    @staticmethod
    def _roster_without(*usernames: str) -> str:
        """The recorded roster with some accounts removed — the shape of an unfriend or a truncated read.

        Parsed and re-serialised rather than string-spliced, so the test cannot pass by accidentally
        producing XML that means something other than intended.
        """
        import xml.etree.ElementTree as ET

        tree = ET.fromstring((Path(__file__).parent.parent / "fixtures" / "plextv_users.xml.txt").read_text())
        for user in list(tree):
            if user.get("title") in usernames or user.get("username") in usernames:
                tree.remove(user)
        return ET.tostring(tree, encoding="unicode")

    def _enable_everyone(self, client: TestClient) -> list[str]:
        """Turn on every non-owner account and return their usernames. The fixture roster leaves the
        managed accounts off, and a departure sweep only ever considers ENABLED users — so without
        this the guards below have a population of one and cannot be exercised at all."""
        for user in client.get("/api/users").json():
            if user["user_type"] != "owner":
                client.patch(f"/api/users/{user['id']}", json={"enabled": True})
        return [u["username"] for u in client.get("/api/users").json() if u["user_type"] != "owner"]

    def test_someone_who_left_the_share_is_turned_off_and_their_rows_removed(
        self, client: TestClient, plextv, monkeypatch
    ):
        """Losing access to the server must take the row with it. Left enabled, their collection stays
        on the PMS carrying their label, every other account keeps excluding a row for a person who is
        gone, and the next run keeps rebuilding it."""
        from shortlist.server.services import user_sync

        removed: list[list[str]] = []

        async def spy(state, slugs):
            removed.append(list(slugs))

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")
        self._enable_everyone(client)
        removed.clear()

        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("sarah")))
        client.post("/api/users/sync")

        after = self._users(client)
        assert after["sarah"]["enabled"] is False, "a departed user must not keep getting rows"
        assert any("sarah" in batch for batch in removed), "their collection must come off the server too"
        assert after["teen"]["enabled"] is True, "nobody else is touched"

    def test_a_departure_is_recorded_as_such_not_just_as_switched_off(self, client: TestClient, plextv, monkeypatch):
        """`enabled=0` means two unrelated things — the owner turned them off, or Plex no longer has
        them. Rendered identically, a departed account is an unexplained row nobody can act on, and
        the excludes for their deleted collection sit in every other filter with nothing to prompt a
        clean-up. `departed_at` is the difference."""
        from shortlist.server.db.models import User
        from shortlist.server.services import user_sync

        async def spy(state, slugs):
            return None

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")
        self._enable_everyone(client)

        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("sarah")))
        client.post("/api/users/sync")

        with client.app.state.sessions() as session:
            gone = session.query(User).filter_by(slug="sarah").one()
            stayed = session.query(User).filter_by(slug="teen").one()
            assert gone.departed_at is not None, "a departure must be distinguishable from a manual toggle"
            assert stayed.departed_at is None, "nobody still on the roster is marked as gone"

    def test_someone_who_comes_back_stops_being_marked_as_gone(self, client: TestClient, plextv, monkeypatch):
        """A re-invite — or a roster blip that slipped past both guards — must heal on the next sync
        rather than needing a hand. The flag is a FACT about the roster, so it is re-derived, never
        latched."""
        from shortlist.server.db.models import User
        from shortlist.server.services import user_sync

        async def spy(state, slugs):
            return None

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")
        self._enable_everyone(client)
        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("sarah")))
        client.post("/api/users/sync")

        plextv.roster.mock(
            return_value=httpx.Response(
                200, text=(Path(__file__).parent.parent / "fixtures" / "plextv_users.xml.txt").read_text()
            )
        )
        client.post("/api/users/sync")

        with client.app.state.sessions() as session:
            back = session.query(User).filter_by(slug="sarah").one()
            assert back.departed_at is None, "they are on the roster again — the mark must clear itself"

    def test_an_empty_roster_never_disables_the_whole_server(self, client: TestClient, plextv, monkeypatch):
        """This sweep runs UNATTENDED daily and deletes collections. "plex.tv returned nothing" and
        "nobody shares this server any more" are the same bytes, and only one of them means act —
        so an empty read must do nothing rather than switch off every account on the server."""
        from shortlist.server.services import user_sync

        removed: list[list[str]] = []

        async def spy(state, slugs):
            removed.append(list(slugs))

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")
        enabled = self._enable_everyone(client)
        removed.clear()

        plextv.roster.mock(return_value=httpx.Response(200, text="<MediaContainer/>"))
        client.post("/api/users/sync")

        after = self._users(client)
        assert all(after[name]["enabled"] for name in enabled), "an empty read must not switch anyone off"
        assert not any(removed), "an empty roster must delete nothing"

    def test_most_of_the_server_leaving_at_once_is_refused_and_recorded(self, client: TestClient, plextv, monkeypatch):
        """One person leaving is routine. Most of them leaving in one poll is a truncated read, and
        acting on it costs every one of them a manual re-enable and a full LLM rebuild. Refused —
        but recorded as an event, so a genuine mass removal is still visible to the owner."""
        from shortlist.server.db.models import Event
        from shortlist.server.services import user_sync

        removed: list[list[str]] = []

        async def spy(state, slugs):
            removed.append(list(slugs))

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")
        enabled = self._enable_everyone(client)
        removed.clear()

        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("sarah", "kid")))
        client.post("/api/users/sync")

        after = self._users(client)
        assert all(after[name]["enabled"] for name in enabled), "a suspected partial read must switch nobody off"
        assert not any(removed), "a suspected partial read must delete no collections"
        with client.app.state.sessions() as session:
            refusals = session.query(Event).filter_by(scope="user.departed.refused").all()
            assert refusals, "silently doing nothing would hide a real mass removal"
            assert all(r.level == "error" for r in refusals)
            # One per DECISION declined. The two are guarded independently — a marking refusal must
            # never be able to veto the row takedown — so the audit has to say which one was refused.
            assert {r.message["half"] for r in refusals} == {"mark", "disable"}

    def test_removing_a_departed_person_clears_their_history_but_keeps_the_snapshot(
        self, client: TestClient, plextv, monkeypatch
    ):
        """Remove frees the list and the filters; it does NOT delete the users row.

        `restriction_snapshots` is RESTRICT-keyed to `users.id` (migration 0055) and holds the only
        copy of that account's share filters as they were BEFORE Shortlist — what uninstall restores
        from (plex-safety rule 2). Uninstall skips any snapshot whose user row has gone, so deleting
        the row would silently cost that person their restore. Archive, don't delete."""
        from shortlist.server.db.models import PickRow, RestrictionSnapshotRow, User
        from shortlist.server.services import user_sync

        async def spy(state, slugs):
            return None

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")
        self._enable_everyone(client)
        with client.app.state.sessions() as session:
            user = session.query(User).filter_by(slug="sarah").one()
            uid = user.id
            session.add(
                PickRow(
                    user_id=uid, run_id=None, rank=1, title="Old Pick", tmdb_id=1, media_type="movie", rating_key=9001
                )
            )
            session.add(RestrictionSnapshotRow(user_id=uid, reason="initial", filters_before={"filterMovies": "x"}))
            session.commit()

        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("sarah")))
        client.post("/api/users/sync")

        r = client.delete(f"/api/users/{uid}")

        assert r.status_code == 200
        with client.app.state.sessions() as session:
            user = session.get(User, uid)
            assert user is not None, "the row must survive — the snapshot is keyed to it"
            assert user.removed_at is not None
            assert session.query(PickRow).filter_by(user_id=uid).count() == 0, "history is dropped"
            assert session.query(RestrictionSnapshotRow).filter_by(user_id=uid).count() == 1, (
                "the pre-Shortlist filters are the one record with no second copy — uninstall needs them"
            )
        assert "sarah" not in self._users(client), "a removed person leaves the Users list"

    def test_a_removed_person_who_is_re_invited_comes_back(self, client: TestClient, plextv, monkeypatch):
        """The lifecycle row that is easy to miss: departed -> removed -> re-invited. The list filters
        on `removed_at`, no PATCH field can clear it, and DELETE now 409s because they are no longer
        departed — so left latched, a returning person is invisible for ever and only a hand-edit of
        the database brings them back."""
        from shortlist.server.services import user_sync

        async def spy(state, slugs):
            return None

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")
        self._enable_everyone(client)
        uid = self._users(client)["sarah"]["id"]

        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("sarah")))
        client.post("/api/users/sync")
        assert client.delete(f"/api/users/{uid}").status_code == 200
        assert "sarah" not in self._users(client)

        plextv.roster.mock(
            return_value=httpx.Response(
                200, text=(Path(__file__).parent.parent / "fixtures" / "plextv_users.xml.txt").read_text()
            )
        )
        client.post("/api/users/sync")

        back = self._users(client)
        assert "sarah" in back, "they are on the share again — the archive no longer applies"
        assert back["sarah"]["departed"] is False

    def test_removing_somebody_who_is_still_on_the_share_is_refused(self, client: TestClient, plextv, monkeypatch):
        """Remove is for people Plex no longer has. Offered on an active account it would read as
        'delete this user', silently dropping their whole pick history while their row keeps being
        rebuilt every night."""
        client.post("/api/users/sync")
        self._enable_everyone(client)
        uid = self._users(client)["sarah"]["id"]

        r = client.delete(f"/api/users/{uid}")

        assert r.status_code == 409
        assert "sarah" in self._users(client)

    def test_a_departed_person_is_flagged_in_the_list_until_removed(self, client: TestClient, plextv, monkeypatch):
        """The Users list has to say WHY somebody is switched off, or a departed account is an
        unexplained row with no action attached to it."""
        from shortlist.server.services import user_sync

        async def spy(state, slugs):
            return None

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")
        self._enable_everyone(client)
        assert self._users(client)["sarah"]["departed"] is False

        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("sarah")))
        client.post("/api/users/sync")

        listed = self._users(client)
        assert listed["sarah"]["departed"] is True
        assert listed["teen"]["departed"] is False

    def test_an_old_unfiled_departure_cannot_veto_todays_real_one(self, client: TestClient, plextv, monkeypatch):
        """The regression that made the two guards one `or`-ed check.

        `gone` is a STOCK, not a rate: an account marked on an earlier sync stays in the population
        until the owner presses Remove, so it never leaves `gone`. Measured against everyone known,
        that ratio only ever climbs — and because it also gated the destructive half, a couple of old
        departures nobody had filed away could permanently refuse to take down the rows of someone
        who left today. Silent, self-perpetuating, and it re-creates the exact condition the sweep
        exists to end: they stay enabled, every run keeps trying their dead share token, and their
        collection sits promoted to Shared Home.
        """
        from shortlist.server.services import user_sync

        removed: list[list[str]] = []

        async def spy(state, slugs):
            removed.append(list(slugs))

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")
        self._enable_everyone(client)

        # An old departure nobody filed away: 'kid' left a while ago and is already marked gone.
        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("kid")))
        client.post("/api/users/sync")
        assert self._users(client)["kid"]["departed"] is True
        removed.clear()

        # Today, ONE more person genuinely leaves. The enabled ratio alone would act immediately.
        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("kid", "sarah")))
        client.post("/api/users/sync")

        after = self._users(client)
        assert after["sarah"]["enabled"] is False, "an old, already-filed departure must not veto a new one"
        assert any("sarah" in batch for batch in removed), "their rows must still come down"
        assert after["sarah"]["departed"] is True

    def test_someone_who_left_while_switched_off_is_still_marked_as_gone(self, client: TestClient, plextv, monkeypatch):
        """Being switched off does not make somebody present.

        The sweep only ever compared ENABLED accounts against the roster, so a user who was off when
        they left Plex was stuck twice over: nothing explained the blank row, and Remove refuses
        anyone not marked departed (409) — so they could not be filed away either. Recorded from a
        live server, where five deleted Home accounts sat in the Users list with no way to act on them.
        """
        from shortlist.server.services import user_sync

        removed: list[list[str]] = []

        async def spy(state, slugs):
            removed.append(list(slugs))

        monkeypatch.setattr(user_sync, "remove_users_rows", spy)
        client.post("/api/users/sync")  # new accounts land disabled — "kid" is never switched on
        uid = self._users(client)["kid"]["id"]
        assert self._users(client)["kid"]["enabled"] is False
        removed.clear()

        plextv.roster.mock(return_value=httpx.Response(200, text=self._roster_without("kid")))
        client.post("/api/users/sync")

        listed = self._users(client)
        assert listed["kid"]["departed"] is True, "off is not the same as gone — the list must say which"
        assert listed["teen"]["departed"] is False, "still on the roster, just switched off"
        assert not any(removed), "somebody who was already off has no rows on Plex to take down"
        assert client.delete(f"/api/users/{uid}").status_code == 200, "and now they can be filed away"

    def test_sync_adds_the_owner_disabled_and_badged(self, client: TestClient, plextv):
        r = client.post("/api/users/sync")
        assert r.status_code == 200
        # Two of the fixture's three accounts are already in the DB (sarah, and 555000200 whom
        # plex.tv now calls "kid"). ADDED are the owner plex.tv never returns, plus 555000300 —
        # the issue-#20 account: managed with no parental profile.
        # `detail` and `queued` come from the JOB the endpoint now goes through — the counts are
        # unchanged, and `queued: False` says the sync actually ran rather than deferring behind a run.
        assert r.json() == {
            "added": 2,
            "updated": 2,
            "total": 4,
            "detail": "Synced 4 account(s) — 2 new, 2 updated",
            "queued": False,
        }

        owner = self._users(client)["steve"]
        assert owner["user_type"] == "owner"
        assert owner["plex_account_id"] == OWNER_ID
        assert owner["slug"] == "steve"
        assert owner["avatar_url"] == "https://plex.tv/users/abc/avatar"
        # Off by default: an existing install gains a user to switch on, not a row that turns up on
        # the owner's Home unannounced.
        assert owner["enabled"] is False

    def test_the_owner_lookup_is_authenticated_as_the_stored_plex_token(self, client: TestClient, plextv):
        """The token decides WHOSE account comes back, so sending the wrong one (or none) would
        either 401 or identify somebody else entirely."""
        client.post("/api/users/sync")

        headers = plextv.whoami.calls.last.request.headers
        assert headers["X-Plex-Token"] == "owner-token"
        assert headers["X-Plex-Client-Identifier"] == client.app.state.client_id

    def test_re_syncing_updates_the_owner_instead_of_duplicating_them(self, client: TestClient, plextv):
        client.post("/api/users/sync")
        plextv.whoami.mock(return_value=httpx.Response(200, json={**OWNER_JSON, "username": "steve-renamed"}))

        r = client.post("/api/users/sync")

        assert r.json()["added"] == 0  # nobody new the second time
        by_name = self._users(client)
        assert "steve" not in by_name
        assert by_name["steve-renamed"]["user_type"] == "owner"
        with client.app.state.sessions() as session:
            assert session.query(User).filter_by(plex_account_id=OWNER_ID).count() == 1

    def test_a_token_belonging_to_another_account_never_becomes_the_owner(self, client: TestClient, plextv):
        """Fail-safe: building a row from this account's history and labelling it the owner's would
        hand one person another's picks, so a mismatched token syncs the shared users and stops."""
        plextv.whoami.mock(return_value=httpx.Response(200, json={**OWNER_JSON, "id": 999999}))

        r = client.post("/api/users/sync")

        assert r.status_code == 200
        assert r.json()["total"] == 3  # the fixture's shared users only
        names = self._users(client)
        assert not any(u["user_type"] == "owner" for u in names.values())

    def test_a_re_link_under_a_new_admin_demotes_the_previous_owner(self, client: TestClient, plextv):
        """`owner` is the type `sync_user_restrictions` skips, so a stale one keeps an account
        exempt from restriction on a server it only shares."""
        client.post("/api/users/sync")
        with client.app.state.sessions() as session:
            session.query(Server).first().owner_account_id = 555000999
            session.commit()
        # Re-linking re-homes the whole instance: the previous admin's session stops being the
        # owner's session, so the new admin signs in before anything else happens.
        client.cookies.set(
            SESSION_COOKIE,
            session_serializer(client.app.state.session_secret).dumps(
                {"account_id": 555000999, "username": "new-admin"}
            ),
        )
        plextv.whoami.mock(
            return_value=httpx.Response(200, json={**OWNER_JSON, "id": 555000999, "username": "new-admin"})
        )

        client.post("/api/users/sync")

        by_name = self._users(client)
        assert by_name["new-admin"]["user_type"] == "owner"
        assert by_name["steve"]["user_type"] == "shared"  # the old owner keeps their row, loses the exemption

    @pytest.mark.parametrize(
        ("response", "why"),
        [
            (httpx.Response(500, text="plex.tv is having a day"), "plex.tv errors"),
            (httpx.Response(200, json={"subscription": {"active": True}}), "the payload has no id"),
        ],
    )
    def test_shared_users_still_sync_when_the_owner_lookup_fails(self, client: TestClient, plextv, response, why):
        """The owner is a bonus on top of the roster — a bad answer on that one call must not leave
        everybody else's list stale, whether it fails at the wire or at the payload."""
        plextv.whoami.mock(return_value=response)

        r = client.post("/api/users/sync")

        assert r.status_code == 200, why
        assert r.json()["total"] == 3, why  # every shared user still landed
        assert "sarah" in self._users(client), why
        assert not any(u["user_type"] == "owner" for u in self._users(client).values()), why


class TestUserRowsApi:
    def _sarah_id(self, client: TestClient) -> int:
        return next(u["id"] for u in client.get("/api/users").json() if u["slug"] == "sarah")

    def test_rows_lists_the_default_row_with_no_picks_yet(self, client: TestClient):
        uid = self._sarah_id(client)
        rows = client.get(f"/api/users/{uid}/rows").json()
        assert [r["slug"] for r in rows] == ["picked"]
        assert set(rows[0]) == {
            "collection_id",
            "slug",
            "name",
            "media",
            "library",
            "section_key",
            "size",
            "recent_count",
            "is_default",
            "muted",
            "override",
            "picks",
        }
        assert set(rows[0]["override"]) == {"row_size", "recent_count"}
        assert rows[0]["is_default"] is True
        assert rows[0]["muted"] is False
        assert rows[0]["picks"] == []

    def test_override_mute_and_resize_round_trip(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        r = client.put(f"/api/users/{uid}/rows/{cid}", json={"muted": True, "row_size": 20})
        assert r.status_code == 200
        assert set(r.json()) == {"collection_id", "muted", "row_size", "recent_count"}
        row = client.get(f"/api/users/{uid}/rows").json()[0]
        assert row["muted"] is True
        assert row["override"]["row_size"] == 20

    def test_size_override_can_be_reset_to_default(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": 20})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["row_size"] == 20
        # Sending an explicit null (the "Default" choice) must clear it, not be ignored.
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": None})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["row_size"] is None

    def test_mute_toggle_preserves_a_saved_size(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": 20})
        # A mute toggle sends only {muted}; the saved size must survive it.
        client.put(f"/api/users/{uid}/rows/{cid}", json={"muted": True})
        row = client.get(f"/api/users/{uid}/rows").json()[0]
        assert row["muted"] is True
        assert row["override"]["row_size"] == 20

    def test_recent_count_override_round_trips_and_resets(self, client: TestClient):
        uid = self._sarah_id(client)
        row0 = client.get(f"/api/users/{uid}/rows").json()[0]
        cid = row0["collection_id"]
        # The row reports its own effective depth (the global default) so the UI knows what "default" means.
        assert row0["recent_count"] == 10
        assert row0["override"]["recent_count"] is None

        client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": 3})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["recent_count"] == 3
        # An explicit null clears it back to the row's own depth, exactly like the size override.
        client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": None})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["recent_count"] is None

    def test_recent_count_and_size_overrides_are_independent(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": 20})
        # A recent_count-only PUT must not clobber the saved size (server writes only fields it's sent).
        client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": 5})
        override = client.get(f"/api/users/{uid}/rows").json()[0]["override"]
        assert override == {"row_size": 20, "recent_count": 5}

    def test_recent_count_override_is_bounded(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        # Same 1..25 bounds the global/row settings validate — out of range is a 422, not a bad save.
        assert client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": 0}).status_code == 422
        assert client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": 26}).status_code == 422

    def test_override_on_unknown_user_or_row_404(self, client: TestClient):
        assert client.put("/api/users/9999/rows/1", json={"muted": True}).status_code == 404
        uid = self._sarah_id(client)
        assert client.put(f"/api/users/{uid}/rows/9999", json={"muted": True}).status_code == 404

    def test_runs_empty_then_unknown_user_404(self, client: TestClient):
        uid = self._sarah_id(client)
        assert client.get(f"/api/users/{uid}/runs").json() == []
        assert client.get("/api/users/9999/runs").status_code == 404

    def test_user_runs_carry_their_own_outcome_not_the_runs(self, client: TestClient):
        """A run is server-wide: its status says whether the RUN succeeded, which says nothing about
        whether this person got a row. `skipped` with a reason is healthy and must read that way."""
        from shortlist.server.db.models import Run, RunUser

        uid = self._sarah_id(client)
        with client.app.state.sessions() as session:
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=uid,
                    status="skipped",
                    reason="no watch history yet",
                    duration_ms=1200,
                )
            )
            session.commit()

        entry = client.get(f"/api/users/{uid}/runs").json()[0]
        assert entry["status"] == "skipped"
        assert entry["reason"] == "no watch history yet"
        assert entry["duration_ms"] == 1200
        assert entry["run_status"] == "ok"  # the run itself was fine

    def test_a_users_run_entry_carries_its_diff_and_every_picks_field(self, client: TestClient):
        """The run entry and its picks are what the user page renders — provenance (`sources`,
        `affinity`, `seed_title`) included, which is the whole "why is this here?" answer."""
        from shortlist.server.db.models import PickRow, Run, RunUser

        uid = self._sarah_id(client)
        with client.app.state.sessions() as session:
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=uid, status="ok", diff={"added": ["Arrival"]}))
            session.add(
                PickRow(
                    run_id=run.id,
                    user_id=uid,
                    tmdb_id=329865,
                    media_type="movie",
                    rating_key=42,
                    rank=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    title="Arrival",
                    reason="because you watched Interstellar",
                    sources="tmdb_similar,llm_web",
                    affinity=0.8,
                    seed_title="Interstellar",
                )
            )
            session.commit()

        entry = client.get(f"/api/users/{uid}/runs").json()[0]

        assert set(entry) == {
            "run_id",
            "started_at",
            "finished_at",
            "status",
            "error",
            "reason",
            "duration_ms",
            "run_status",
            "dry_run",
            "diff",
            "picks",
        }
        assert entry["diff"] == {"added": ["Arrival"]}, "a free-form diff passes through as stored"
        assert set(entry["picks"][0]) == PICK_KEYS
        assert entry["picks"][0]["sources"] == ["tmdb_similar", "llm_web"]
        assert entry["picks"][0]["affinity"] == 0.8
        assert entry["picks"][0]["seed_title"] == "Interstellar"

    def test_user_runs_summary_counts_the_runs_that_left_them_out(self, client: TestClient):
        """Six entries on a person's page read as "the server ran six times" without this."""
        from shortlist.server.db.models import Run, RunUser

        uid = self._sarah_id(client)
        with client.app.state.sessions() as session:
            included = Run(trigger="manual", status="ok")
            session.add_all([included, Run(trigger="manual", status="ok"), Run(trigger="manual", status="ok")])
            session.flush()
            session.add(RunUser(run_id=included.id, user_id=uid, status="ok"))
            session.commit()

        assert client.get(f"/api/users/{uid}/runs/summary").json() == {"included": 1, "total": 3}
        assert client.get("/api/users/9999/runs/summary").status_code == 404


class TestUserTypeIsTheEnginesOwnEnum:
    """`user_type` is typed on the response as the engine's `UserType`, not a bare `str`.

    That closes the set in the OpenAPI schema (so the SPA stops re-declaring it), but it also means
    the value is validated on the way OUT: a `users.user_type` outside the enum would raise rather
    than degrade, and 500 the whole users list. Every member therefore gets pushed through the real
    endpoint here, and the set is taken from `UserType` itself so a new member cannot be forgotten.
    """

    def test_every_user_type_the_engine_defines_serializes_as_its_plain_word(self, client: TestClient):
        from shortlist.engine.models import UserType

        with client.app.state.sessions() as session:
            for i, member in enumerate(UserType):
                session.add(
                    User(
                        plex_account_id=555009000 + i,
                        username=f"u{member.value}",
                        slug=f"u-{member.value}",
                        user_type=member.value,
                    )
                )
            session.commit()

        by_slug = {u["slug"]: u for u in client.get("/api/users").json()}

        for member in UserType:
            # The plain word, not "UserType.SHARED": a StrEnum must not change the payload the SPA
            # already reads, only the schema that describes it.
            assert by_slug[f"u-{member.value}"]["user_type"] == member.value
