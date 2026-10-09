"""`/api/collections`: create, edit, delete, title uniqueness, durable Plex edits, seeds and deleted rows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shortlist.server.auth import SESSION_COOKIE
from shortlist.server.db.models import DEFAULT_SLUG, User
from tests.integration.api_collections_support import POSTER_KEYS, _plex_jobs

pytestmark = pytest.mark.integration


# Response KEY SETS, spelled out — see the same note in test_api_users.py. A Pydantic response model
# FILTERS the payload, so every model here sets `extra="allow"`; naming every key is what fails if
# that config is ever stripped, or if a default starts inventing a key the handler never sent.

#: Every key `row_editing.serialize_row` renders — `GET`, `POST` and `PATCH /api/collections` alike.
COLLECTION_KEYS = {
    "ai_instructions",
    "ai_paused",
    "ai_tokens",
    "theme_id",
    "theme_name",
    "theme_emoji",
    "theme_mode",
    "explore_brief",
    "theme_days",
    "refresh_share",
    "repeat_cooldown_days",
    "avoid_rows",
    "id",
    "slug",
    "name",
    "description",
    "sort_title_prefix",
    "last_run_id",
    "preview_titles",
    "build",
    "audience",
    "audience_user_ids",
    "enabled",
    "schedule",
    "size",
    "media",
    "sort_order",
    "name_template",
    "fallback_name",
    "min_watchers",
    "request_tag",
    "candidate_sources",
    "watched_pct",
    "rewatch",
    "rewatch_cooldown_days",
    "requests_row",
    "requests_window_days",
    "requests_tag_pattern",
    "unstarted_only",
    "refresh_days",
    "idle_hold_days",
    "recency",
    "recent_count",
    "max_seeds",
    "max_runtime",
    "min_year",
    "max_year",
    "min_rating",
    "cold_start",
    "seed_window",
    # This row's own request floors and Arr target; null on any of them means inherit the global.
    "req_min_rating",
    "req_min_votes",
    "req_min_demand",
    "req_min_year",
    "req_max_year",
    "req_auto_send",
    "req_auto_min_demand",
    "req_auto_min_rating",
    "req_max_per_row",
    "req_radarr_quality_profile_id",
    "req_radarr_root_folder",
    "req_sonarr_quality_profile_id",
    "req_sonarr_root_folder",
    "req_sonarr_monitor",
    "req_language_mode",
    "req_preferred_languages",
    "req_min_rating_other",
    "req_auto_user_tag",
    "pick_order",
    "placement",
    "placement_friends",
    "show_days",
    "shown_today",
    "seasons",
    "season_lead_days",
    "season_after_days",
    "season_status",
    "pin_top",
    "hub_anchor",
    "library_keys",
    "poster",
    # Preview-only, and null on every live response. `_serialize` never produces them: they are
    # declared on `CollectionOut` with a default so a dry-run PATCH can ride them back beside the
    # unchanged row AND so the SPA's generated types know about them — an undeclared key reaches the
    # client but is invisible in the OpenAPI schema. That is the documented exception to this file's
    # "a default lets a handler INVENT a key" rule, and naming them here is what keeps it deliberate.
    "dry_run",
    "plan",
    "preview_incomplete",
}


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
        section = SimpleNamespace(title="Movies", key="1", type="movie")
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

    def _shared_row_with_a_lock_spy(self, client: TestClient, monkeypatch, lock) -> tuple[int, list[str], list[bool]]:
        """A shared row on a fake Plex, with `jobs.plex_writer_lock` pinned to ``lock`` and every Plex read
        recording whether that lock was held at the time."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.services import jobs

        created = client.post("/api/collections", json={"name": "Popular", "build": "shared"})
        cid, slug = created.json()["id"], created.json()["slug"]
        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("🔥 Popular" + row_marker(0), f"shortlist__shared_{slug}")]
        )
        monkeypatch.setattr(jobs, "plex_writer_lock", lambda: lock)
        plex = client.app.state.run_service.build_context().plex
        sections = plex.sections.return_value
        held: list[bool] = []
        plex.sections.side_effect = lambda *a, **kw: (held.append(lock.locked()), sections)[1]
        return cid, deleted, held

    def test_a_real_cleanup_holds_the_one_writer_lock_while_it_touches_plex(self, client: TestClient, monkeypatch):
        """The job worker and every run hold this lock; the cleanup button did not. A cleanup overlapping a run
        that delivers the same row could forget the ledger key the run had just written, and plays on that row
        went uncredited until its next delivery found it by label again."""
        import asyncio

        cid, deleted, held = self._shared_row_with_a_lock_spy(client, monkeypatch, asyncio.Lock())

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})

        assert r.status_code == 200
        assert len(deleted) == 1
        assert held and all(held), "the removal touched Plex without the one-writer lock held"

    def test_a_cleanup_preview_takes_no_lock_and_still_answers_during_a_run(self, client: TestClient, monkeypatch):
        """A dry run writes nothing, so it has no business holding the one-writer lock — and the preview is
        what the confirm dialog opens on, so refusing it mid-run would leave the dialog with nothing to show."""
        import asyncio

        cid, deleted, held = self._shared_row_with_a_lock_spy(client, monkeypatch, asyncio.Lock())
        monkeypatch.setattr(client.app.state.run_service, "is_running", lambda: True)

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": True})

        assert r.status_code == 200
        assert r.json()["removed"] == ["🔥 Popular"]
        assert deleted == []
        assert held and not any(held), "a preview took the one-writer lock"

    def test_a_real_cleanup_is_refused_while_a_run_is_writing(self, client: TestClient, monkeypatch):
        """A run holds the lock for its whole length — many minutes on a real server — so waiting for it
        would hang the request with nothing on screen to say why. Refuse, say so, and touch nothing."""
        import asyncio

        cid, deleted, held = self._shared_row_with_a_lock_spy(client, monkeypatch, asyncio.Lock())
        monkeypatch.setattr(client.app.state.run_service, "is_running", lambda: True)

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})

        assert r.status_code == 409
        assert "run" in r.json()["detail"].lower() and "nothing was removed" in r.json()["detail"].lower()
        assert deleted == [] and held == [], "a refused cleanup still read or wrote Plex"

    def test_a_real_cleanup_stands_down_when_another_writer_keeps_the_lock(self, client: TestClient, monkeypatch):
        """A writer JOB is short enough to wait for, but only for a bounded time — the same bound the job
        worker uses — so a lock that stays held costs the request a wait, never an open-ended hang."""
        import asyncio

        from shortlist.server.services import jobs

        lock = asyncio.Lock()
        asyncio.run(lock.acquire())  # another writer, mid-flight
        cid, deleted, held = self._shared_row_with_a_lock_spy(client, monkeypatch, lock)
        monkeypatch.setattr(jobs, "WRITER_LOCK_WAIT_S", 0.05)

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})

        assert r.status_code == 409
        assert "nothing was removed" in r.json()["detail"].lower()
        assert deleted == [] and held == []
        assert lock.locked(), "the other writer's hold was released by a request that never had it"

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
        section = SimpleNamespace(title="Movies", key="1", type="movie")
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
        created = client.post("/api/collections", json={"name": "Temp Row"})
        assert created.status_code in (200, 201)
        assert client.delete(f"/api/collections/{created.json()['id']}").status_code == 204
        queued = [job for job in _plex_jobs(client) if job["kind"] == "row.reconcile"]
        assert len(queued) == 1, "the Plex removal must be durable"
        assert queued[0]["payload"]["slug"] == "temp_row"
        assert queued[0]["payload"]["template"] == "Temp Row"

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

    def test_post_refuses_a_dry_run_it_cannot_honour(self, client: TestClient):
        """`CollectionIn` is shared with PATCH, so `dry_run` is in the POST schema too — and creation
        has nothing to preview. Ignoring it silently would mean `POST {"dry_run": true}` answers 201
        having created the row: a documented preview flag that writes, which is the shape
        plex-safety rule 8 exists to prevent."""
        before = len(client.get("/api/collections").json())

        r = client.post(
            "/api/collections",
            json={"name": "Preview Me", "build": "per_person", "dry_run": True},
        )

        assert r.status_code == 422
        assert "dry_run" in r.json()["detail"]
        # The EFFECT, not just the status. The finding was "a documented preview flag that writes",
        # and a test that only reads the code would still pass if the guard were moved below the
        # insert — which is exactly the mistake it exists to catch.
        after = client.get("/api/collections").json()
        assert len(after) == before
        assert not any(row["name"] == "Preview Me" for row in after)

    def test_post_still_creates_a_row_without_the_flag(self, client: TestClient):
        r = client.post("/api/collections", json={"name": "Real Row", "build": "per_person"})

        assert r.status_code == 201
        assert any(row["name"] == "Real Row" for row in client.get("/api/collections").json())


class TestNoTwoRowsShareATitle:
    """Every door that sets a row's title refuses one another row already renders.

    Two rows resolving to one template become ONE collection on Plex: per-person rows all carry the
    `shortlist_<userslug>` label and `_find_this_rows_collection` tells them apart by title alone, so
    the second row to deliver overwrites the first's picks, both lose their ledger key to
    `_delivered_keys`'s ambiguity drop, and removing either deletes the collection the other uses.

    The guard used to compare the `name` COLUMN, which is not what a row is titled from. Every test
    here fails against that version.
    """

    #: What the row-template gallery's "Picked for You" tile posts, and what Settings → Defaults holds
    #: for the default row. The same string reached both by two different fields, which is the bug.
    DEFAULT_TEMPLATE = "✨ {library_name} Picked for You"

    def test_a_new_row_cannot_take_the_default_rows_title(self, client: TestClient):
        """The reported bug. The default row's `name` column is "✨ Picked for You" (migration 0001)
        while its TITLE is the global template — so a column-level check saw no clash, and the
        row-template gallery's "Picked for You" tile (which posted exactly this string) added a second
        row that shared one collection per user per library with the row every install ships.

        The tile now posts a distinct name; this asserts the DOOR is shut, not just that one caller
        stopped walking through it."""
        clash = client.post("/api/collections", json={"name": self.DEFAULT_TEMPLATE})

        assert clash.status_code == 422, "the gallery's first tile duplicated the row every install ships"
        assert "default row" in clash.json()["detail"], clash.json()["detail"]

    def test_a_name_template_takes_another_rows_title(self, client: TestClient):
        """`name_template` WINS over `name` when a row is titled, so the check has to run on the merged
        pair. The old one ran on `body.name` alone, which here is this row's own unchanged name — no
        clash, 200, and two rows titled "Friday Films".

        `name` is sent alongside because `CollectionIn.name` is required, which is also how the editor
        PATCHes. Omitting it 422s on schema validation, so a test that sent only `name_template` would
        pass against the bug for the wrong reason."""
        client.post("/api/collections", json={"name": "Friday Films"})
        other = client.post("/api/collections", json={"name": "Sunday Films"}).json()

        clash = client.patch(
            f"/api/collections/{other['id']}",
            json={"name": "Sunday Films", "name_template": "Friday Films"},
        )

        assert clash.status_code == 422, "a row took another's title through the field titles come from"
        assert "Friday Films" in clash.json()["detail"], clash.json()["detail"]

    def test_renaming_the_default_row_cannot_take_another_rows_title(self, client: TestClient):
        """The mirror of the first case: the collision is reachable from either side, and renaming the
        default row writes the global template rather than a column, so it skipped the check entirely."""
        client.post("/api/collections", json={"name": "Friday Films"})
        default = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")

        clash = client.patch(f"/api/collections/{default['id']}", json={"name": "Friday Films"})

        assert clash.status_code == 422
        assert client.get("/api/settings").json()["row.name_template"] != "Friday Films", (
            "the refused rename must not have been written"
        )

    def test_settings_defaults_cannot_take_another_rows_title(self, client: TestClient):
        """Settings → Defaults writes the same global template, so it is a fifth door onto the same
        collision — and a settings PUT carries other keys, which must not half-apply behind a refusal."""
        client.post("/api/collections", json={"name": "Friday Films"})

        clash = client.put("/api/settings", json={"values": {"row.name_template": "Friday Films", "row.size": 27}})

        assert clash.status_code == 422
        settings = client.get("/api/settings").json()
        assert settings["row.name_template"] != "Friday Films"
        assert settings["row.size"] != 27, "the refusal must leave the whole request unwritten"

    def test_a_row_with_its_own_template_may_share_a_name(self, client: TestClient):
        """The deliberate relaxation. A row carrying its own `name_template` is titled from THAT, so its
        `name` — a label on the Rows page — collides with nothing. The old column-level check refused
        this pair even though their Plex titles differ."""
        client.post("/api/collections", json={"name": "Films", "name_template": "🍿 Friday Films"})

        allowed = client.post("/api/collections", json={"name": "Films", "name_template": "📺 Sunday Films"})

        assert allowed.status_code == 201, allowed.text

    def test_a_row_still_saves_under_its_own_unchanged_title(self, client: TestClient):
        """The self-clash guard: editing any other setting re-sends the name, which must not read as a
        row colliding with itself."""
        row = client.post("/api/collections", json={"name": "Friday Films"}).json()

        same = client.patch(f"/api/collections/{row['id']}", json={"name": "Friday Films", "size": 12})

        assert same.status_code == 200, same.text
        assert same.json()["size"] == 12

    def test_the_rename_endpoint_cannot_take_another_rows_title(self, client: TestClient):
        """The sixth door, and the only one that retitles the collections on Plex itself. It documents
        standalone use without the dialog PATCH, so the SPA's PATCH-first flow does not cover it."""
        client.post("/api/collections", json={"name": "Friday Films"})
        other = client.post("/api/collections", json={"name": "Sunday Films"}).json()

        clash = client.post(
            f"/api/collections/{other['id']}/rename",
            json={"name_template": "Friday Films", "dry_run": True},
        )

        assert clash.status_code == 422, "a rename took another row's title and went on to retitle Plex"
        reloaded = next(c for c in client.get("/api/collections").json() if c["id"] == other["id"])
        assert reloaded["name_template"] != "Friday Films", "the refused rename must not have been saved"

    def test_the_rename_endpoint_cannot_take_the_default_rows_title(self, client: TestClient):
        """Its default-row branch writes the GLOBAL template — the same write PATCH and PUT /settings
        both refuse, and the one that renames every user's collection."""
        client.post("/api/collections", json={"name": "Friday Films"})
        default = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")

        clash = client.post(f"/api/collections/{default['id']}/rename", json={"name_template": "Friday Films"})

        assert clash.status_code == 422
        assert client.get("/api/settings").json()["row.name_template"] != "Friday Films"

    def test_two_unnameable_rows_no_longer_clash_on_a_name_neither_of_them_has(self, client: TestClient):
        """The old rule inverted, on purpose (issue #84).

        Both of these used to render the same substitute name for anyone with no watch, so they WERE
        one collection and had to be refused. Nothing substitutes now: for such a person neither row
        has a title and neither is built, so there is nothing to collide on and refusing the second
        row was blocking a configuration that is fine.
        """
        client.post("/api/collections", json={"name": "Because you watched {top_seed}"})

        ok = client.post("/api/collections", json={"name": "More like {top_seed}"})

        assert ok.status_code < 300, f"two unnameable rows cannot share a title they do not have: {ok.text}"

    def test_two_rows_sharing_a_FALLBACK_name_still_clash(self, client: TestClient):
        """Where the collision moved to.

        A fallback name is a real title that really gets written, so two rows carrying the same one
        land on one collection for every person who needs it — the same trap the rule above used to
        catch, now at the name the operator actually chose.
        """
        first = client.post(
            "/api/collections",
            json={"name": "Because you watched {top_seed}", "fallback_name": "Picked for You"},
        )
        assert first.status_code < 300, first.text

        clash = client.post(
            "/api/collections",
            json={"name": "More like {top_seed}", "fallback_name": "Picked for You"},
        )

        assert clash.status_code == 422, "two rows with the same fallback name were allowed"

    def test_a_PATCH_cannot_sneak_in_a_duplicate_fallback_name(self, client: TestClient):
        """POST checked this from the start; PATCH did not — and PATCH is what the row editor saves
        through, so the state POST returns 422 for was reachable in one ordinary edit."""
        client.post("/api/collections", json={"name": "Because you watched {top_seed}", "fallback_name": "Shared"})
        second = client.post("/api/collections", json={"name": "More like {top_seed}"})
        assert second.status_code < 300, second.text

        clash = client.patch(f"/api/collections/{second.json()['id']}", json={"fallback_name": "Shared"})

        assert clash.status_code == 422, "two rows were allowed to share a fallback name via PATCH"

    def test_the_DEFAULT_row_cannot_take_another_row_s_title_as_its_fallback(self, client: TestClient):
        """The one write on the row editor that had no duplicate-title check behind it.

        The default row's `name`/`name_template` are exempt because its title is the global setting —
        but its FALLBACK is a per-row column like anyone else's, and the "no name for newcomers" alert
        points operators straight at that field. Two rows rendering one title for one person in one
        library are a single Plex collection: one row's picks overwrite the other's, and removing
        either deletes the collection the survivor is using.
        """
        client.post("/api/collections", json={"name": "Hidden Gems"})
        # The DEFAULT row by slug, not by a guessed id — the first test I wrote patched id 1, which
        # was the newly created row, so it passed with the guard reverted and proved nothing.
        rows = client.get("/api/collections").json()
        default_id = next(r["id"] for r in rows if r["slug"] == "picked")

        # `name` is REQUIRED on this body — a PATCH without it is refused by request validation before
        # any clash logic runs, which is exactly how the first version of this test passed while
        # proving nothing. The row editor sends the whole row, so this is also what a real save looks
        # like: the default row's own (unchanged) title, plus the field being set.
        default = next(r for r in rows if r["slug"] == "picked")
        clash = client.patch(
            f"/api/collections/{default_id}",
            json={"name": default["name"], "fallback_name": "Hidden Gems"},
        )

        assert clash.status_code == 422, "the default row was allowed to take another row's title"
        # The MESSAGE too, so this fails when the 422 comes from somewhere else. An earlier version of
        # this test asserted the status alone and passed with the guard reverted — the request was
        # being refused for an unrelated reason, so it proved nothing about the clash check at all.
        assert "already the title of" in clash.json()["detail"], clash.json()

    def test_the_error_names_the_field_that_actually_collided(self, client: TestClient):
        """A fallback clash used to quote the row NAME and say "pick a different name" — sending the
        operator to the box that is fine, while the one that collided sits untouched below it."""
        client.post("/api/collections", json={"name": "Because you watched {top_seed}", "fallback_name": "Shared"})
        second = client.post("/api/collections", json={"name": "More like {top_seed}"})

        clash = client.patch(
            f"/api/collections/{second.json()['id']}", json={"name": "More like {top_seed}", "fallback_name": "Shared"}
        )

        assert clash.status_code == 422
        detail = clash.json()["detail"]
        assert "'Shared'" in detail, detail
        assert "nothing watched yet" in detail, detail

    def test_a_fallback_name_cannot_itself_need_a_seed(self, client: TestClient):
        """The nastiest shape this feature could take: the operator does what the alert asks, the
        alert clears, and nothing changes.

        The fallback is what a row is called when `{top_seed}` CANNOT be filled, so one that also
        needs a seed is no fallback at all — `render_row_name` discards it. The API used to accept it
        and the "no name for newcomers" alert saw a non-empty value and went quiet, so the row was
        still not built and the operator had been told it was fixed.
        """
        bad = client.post(
            "/api/collections",
            json={"name": "More like {top_seed}", "fallback_name": "Popular like {top_seed}"},
        )

        assert bad.status_code == 422, "a fallback that also needs a seed was accepted"
        ok = client.post("/api/collections", json={"name": "More like {top_seed}", "fallback_name": "Popular"})
        assert ok.status_code < 300, ok.text

    def test_a_blank_global_template_is_refused(self, client: TestClient):
        """A blank one renders to DEFAULT_ROW_NAME, silently retitling the default row onto any row
        literally named that — the reported bug, reachable in two ordinary requests."""
        blank = client.put("/api/settings", json={"values": {"row.name_template": "   "}})

        assert blank.status_code == 422
        assert client.get("/api/settings").json()["row.name_template"].strip()

    def test_whitespace_only_differences_still_collide(self, client: TestClient):
        """`render_row_name` collapses runs of whitespace in a `{library_name}` template, so these two
        render the identical title."""
        client.post("/api/collections", json={"name": "{library_name} Picks"})

        clash = client.post("/api/collections", json={"name": "{library_name}  Picks"})

        assert clash.status_code == 422, "two templates rendering the identical title were allowed"

    def test_a_shared_row_may_share_a_title_with_a_per_person_row(self, client: TestClient):
        """They cannot become one collection: different invisible markers (`row_marker(0)` vs the
        account's) and different label namespaces, and `_find_this_rows_collection` only searches
        within one label. Refusing the pair was a false positive asserting a collision that can't
        happen."""
        client.post("/api/collections", json={"name": "Friday Films"})

        allowed = client.post("/api/collections", json={"name": "Friday Films", "build": "shared"})

        assert allowed.status_code == 201, allowed.text

    def test_an_existing_clash_does_not_block_an_unrelated_edit(self, client: TestClient):
        """A row that already clashes — made before this guard, or restored from a backup — must stay
        editable. The rule is "no NEW clashes", not "a clashing row may never be touched"; the editor
        re-sends `name` on every save, so otherwise a size change is refused for the wrong reason."""
        first = client.post("/api/collections", json={"name": "Friday Films"}).json()
        second = client.post("/api/collections", json={"name": "Sunday Films"}).json()
        # Force the clash the way a pre-guard install carries one, bypassing the API.
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            session.get(Collection, second["id"]).name = "Friday Films"
            session.commit()

        edit = client.patch(f"/api/collections/{second['id']}", json={"name": "Friday Films", "size": 12})

        assert edit.status_code == 200, f"an unrelated edit was refused on an existing clash: {edit.text}"
        assert edit.json()["size"] == 12
        # And the door is still shut for a NEW clash onto that same title.
        assert client.post("/api/collections", json={"name": "Friday Films"}).status_code == 422
        assert first["id"]


class TestADeletedRowsSlugIsNotHandedToANewRow:
    """A row's slug is its identity in every history table — last run's picks, the delivery ledger,
    shared-row picks and watch credits, and which row a queued request belongs to. Deleting a row
    frees the slug in `collections` only, so a new row with the same name took it over and inherited
    all of that: seen live on 2026-09-13, a re-created row's first run redelivered the deleted row's
    five picks as "not due to rebuild"."""

    def _history(self, session, kind: str, slug: str) -> None:
        from shortlist.server.db.models import (
            Delivery,
            Job,
            PickRow,
            RequestCandidate,
            Run,
            RunSharedRow,
            SharedRowWatch,
        )

        user = session.query(User).first() or User(plex_account_id=4242, username="sarah", slug="sarah")
        session.add(user)
        run = Run(trigger="manual", status="ok")
        session.add(run)
        session.flush()
        session.add(
            {
                "picks": lambda: PickRow(
                    run_id=run.id,
                    user_id=user.id,
                    tmdb_id=1,
                    media_type="movie",
                    rating_key=10,
                    rank=1,
                    collection_slug=slug,
                    section_key="1",
                ),
                "deliveries": lambda: Delivery(
                    collection_slug=slug, user_slug=user.slug, library_key="1", rating_key=10
                ),
                "run_shared_rows": lambda: RunSharedRow(run_id=run.id, collection_slug=slug),
                "shared_row_watches": lambda: SharedRowWatch(
                    user_id=user.id, collection_slug=slug, tmdb_id=1, media_type="movie"
                ),
                "request_candidates": lambda: RequestCandidate(tmdb_id=1, media_type="movie", title="T", row_slug=slug),
                # Not history yet, but about to be: DELETE queues this before dropping the row, it cannot
                # start while a run is in flight, and that run still holds the old row and persists its
                # picks under the slug. When it does start, it removes by that slug's ledger keys.
                "reconcile_job": lambda: Job(kind="row.reconcile", status="queued", payload={"slug": slug}),
            }[kind]()
        )
        session.commit()

    @pytest.mark.parametrize(
        "kind", ["picks", "deliveries", "run_shared_rows", "shared_row_watches", "request_candidates", "reconcile_job"]
    )
    def test_a_slug_history_still_names_is_not_reused(self, client: TestClient, kind: str):
        from shortlist.server.db.models import Collection

        first = client.post("/api/collections", json={"name": "Hidden Gems"}).json()
        assert first["slug"] == "hidden_gems"
        with client.app.state.sessions() as session:
            self._history(session, kind, "hidden_gems")
            session.delete(session.get(Collection, first["id"]))
            session.commit()

        again = client.post("/api/collections", json={"name": "Hidden Gems"}).json()

        assert again["slug"] != "hidden_gems", f"the new row inherited the deleted row's {kind}"

    def test_a_new_row_never_takes_the_default_rows_slug(self, client: TestClient):
        """`picked` is not just a name: it makes a row THE default row everywhere — titled from the global
        template, credited with every legacy pick stored under a blank slug."""
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            session.query(Collection).filter_by(slug=DEFAULT_SLUG).delete()
            session.commit()

        created = client.post("/api/collections", json={"name": "Picked"}).json()

        assert created["slug"] != DEFAULT_SLUG
        assert created["name"] == "Picked"

    def test_deleting_a_row_drops_its_uploaded_poster(self, client: TestClient):
        """SQLite hands a freed highest id to the next row, and the upload is keyed by id — so a new row
        served, and could push to Plex, the deleted row's artwork."""
        import base64

        from shortlist.server.services import poster_service

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        )
        cid = client.post("/api/collections", json={"name": "With Art"}).json()["id"]
        uploaded = client.post(f"/api/collections/{cid}/poster/upload", files={"file": ("p.png", png, "image/png")})
        assert uploaded.status_code == 200

        assert client.delete(f"/api/collections/{cid}").status_code == 204

        with client.app.state.sessions() as session:
            assert poster_service.load_upload(session, cid) is None

    def test_a_slug_nothing_remembers_is_still_used_as_is(self, client: TestClient):
        from shortlist.server.db.models import Collection

        first = client.post("/api/collections", json={"name": "Hidden Gems"}).json()
        with client.app.state.sessions() as session:
            session.delete(session.get(Collection, first["id"]))
            session.commit()

        again = client.post("/api/collections", json={"name": "Hidden Gems"}).json()

        assert again["slug"] == "hidden_gems"


class TestTheSameTitleInDifferentLibraries:
    """Issue #121: two rows may share a title when they can never build in the same library.

    Per-person rows are told apart by title only WITHIN a library — the removal, rename and placement
    paths now refuse a title another row builds under there — so a Movies-only and a TV-only row
    wearing one name are two collections, exactly as one row spanning both libraries always was.
    """

    def test_a_movies_row_and_a_tv_row_may_share_a_name(self, client: TestClient):
        """The issue as reported."""
        name = "{library_name} Picked For You"
        assert client.post("/api/collections", json={"name": name, "media": "show"}).status_code == 201

        allowed = client.post("/api/collections", json={"name": name, "media": "movie"})

        assert allowed.status_code == 201, allowed.text

    def test_two_named_libraries_with_nothing_in_common_may_share_a_name(self, client: TestClient):
        assert (
            client.post(
                "/api/collections", json={"name": "Friday", "media": "movie", "library_keys": ["1"]}
            ).status_code
            == 201
        )

        allowed = client.post("/api/collections", json={"name": "Friday", "media": "movie", "library_keys": ["3"]})

        assert allowed.status_code == 201, allowed.text

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ({"media": "movie"}, {"media": "movie", "library_keys": ["3"]}),  # every movie library includes 3
            ({"media": "both"}, {"media": "show"}),
            ({"media": "movie", "library_keys": ["1"]}, {"media": "both", "library_keys": ["1", "2"]}),
        ],
    )
    def test_rows_that_can_meet_in_one_library_still_clash(self, client: TestClient, first, second):
        assert client.post("/api/collections", json={"name": "Friday", **first}).status_code == 201

        clash = client.post("/api/collections", json={"name": "Friday", **second})

        assert clash.status_code == 422
        assert "single collection" in clash.json()["detail"]

    @pytest.mark.parametrize(
        "change",
        [{"media": "both"}, {"media": "movie"}, {"media": "both", "library_keys": ["1", "2"]}],
    )
    def test_moving_a_row_into_a_library_where_its_name_is_taken_is_refused(self, client: TestClient, change):
        """A new door onto the same collision: the title stays put, the libraries move."""
        client.post("/api/collections", json={"name": "Friday", "media": "movie"})
        tv = client.post("/api/collections", json={"name": "Friday", "media": "show"}).json()

        moved = client.patch(f"/api/collections/{tv['id']}", json={"name": "Friday", **change})

        assert moved.status_code == 422, moved.text
        assert "single collection" in moved.json()["detail"]
        with client.app.state.sessions() as session:
            from shortlist.server.db.models import Collection

            assert session.get(Collection, tv["id"]).media == "show", "a refused edit must write nothing"

    def test_moving_a_row_where_its_name_is_free_is_allowed(self, client: TestClient):
        client.post("/api/collections", json={"name": "Friday", "media": "movie", "library_keys": ["1"]})
        tv = client.post("/api/collections", json={"name": "Friday", "media": "show"}).json()

        moved = client.patch(
            f"/api/collections/{tv['id']}", json={"name": "Friday", "media": "movie", "library_keys": ["3"]}
        )

        assert moved.status_code == 200, moved.text

    def test_switching_to_per_person_where_the_name_is_taken_is_refused(self, client: TestClient):
        """A shared row never collides with a per-person one, so the build is part of the same door."""
        client.post("/api/collections", json={"name": "Friday"})
        shared = client.post("/api/collections", json={"name": "Friday", "build": "shared"}).json()

        flipped = client.patch(f"/api/collections/{shared['id']}", json={"name": "Friday", "build": "per_person"})

        assert flipped.status_code == 422, flipped.text
        assert "single collection" in flipped.json()["detail"]

    def test_an_existing_clash_does_not_block_a_library_edit_that_keeps_it(self, client: TestClient):
        """ "No NEW clashes": a row already sharing a library and a title with another (from before this
        guard, or a restore) must stay editable — including its libraries."""
        client.post("/api/collections", json={"name": "Friday", "media": "movie"})
        other = client.post("/api/collections", json={"name": "Sunday", "media": "movie"}).json()
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            session.get(Collection, other["id"]).name = "Friday"
            session.commit()

        edit = client.patch(f"/api/collections/{other['id']}", json={"name": "Friday", "media": "both"})

        assert edit.status_code == 200, edit.text

    def test_the_default_rows_template_may_match_a_row_in_libraries_it_cannot_reach(self, client: TestClient):
        """The default row's title is the global setting, so `PUT /api/settings` is a door too — and it
        is judged by the DEFAULT row's libraries, not as though that row built everywhere."""
        from shortlist.server.db.models import DEFAULT_SLUG, Collection

        with client.app.state.sessions() as session:
            session.query(Collection).filter_by(slug=DEFAULT_SLUG).one().media = "movie"
            session.commit()
        client.post("/api/collections", json={"name": "Friday", "media": "show"})

        allowed = client.put("/api/settings", json={"values": {"row.name_template": "Friday"}})

        assert allowed.status_code == 200, allowed.text

    def test_the_default_rows_template_still_clashes_where_it_can_reach(self, client: TestClient):
        client.post("/api/collections", json={"name": "Friday", "media": "show"})

        clash = client.put("/api/settings", json={"values": {"row.name_template": "Friday"}})

        assert clash.status_code == 422, clash.text


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
        plex.sections.return_value = [SimpleNamespace(title="Movies", key="1", type="movie")]
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
        return _plex_jobs(client)

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
        assert _plex_jobs(client) == []


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
        # SPLIT, not one number. `pick_rows` and `shared_watches` are different tables and a shared row
        # has only the second, so a single `picks` key meant the audit and the API response used one
        # word for two different totals.
        assert event.message["pick_rows"] == 2
        assert event.message["shared_watches"] == 0
        assert event.message["total"] == 2

    def test_nothing_to_clear_is_not_an_error(self, client: TestClient):
        assert client.delete("/api/report/deleted-rows").json()["cleared"] == 0

    def test_it_is_owner_only(self, client: TestClient):
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/report/deleted-rows").status_code == 401
        assert client.delete("/api/report/deleted-rows").status_code == 401
