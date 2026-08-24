"""API contract tests: triggering and reading engine runs."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from shortlist.server.db.models import User

pytestmark = pytest.mark.integration

# The exact key set each response carries, spelled out. These endpoints declare Pydantic response
# models, and a response model FILTERS: any key it does not declare is dropped from the payload,
# silently, with nothing failing until a page goes blank in production. Asserting the whole key set
# (not "some field I care about is present") is what proves the models describe the shape instead of
# eating part of it.
RUN_SUMMARY_KEYS = {
    "id",
    "trigger",
    "started_at",
    # When the engine actually began, as opposed to when the row was created. None for a run that
    # never left the queue — which is how "waited 9 minutes then cancelled" is told apart from
    # "worked for 9 minutes".
    "began_at",
    "finished_at",
    "status",
    "dry_run",
    "stats",
    "error",
    "promotion_blockers",
}
RUN_DETAIL_KEYS = RUN_SUMMARY_KEYS | {"users", "shared_rows"}
RUN_USER_KEYS = {
    "username",
    "display_name",
    "slug",
    "status",
    "error",
    "reason",
    "duration_ms",
    "llm_tokens",
    "llm_tokens_by_step",
    "exa_searches",
    "diff",
    "picks",
    "breakdown",
    "has_trace",
    "rows_considered",
    "cost",
}
#: A shared row's entry. Same field set as a user's minus the three user fields, plus the row's own
#: slug and its title as rendered at run time. `cost` is per-person only — a shared row belongs to
#: nobody, so there is no "this person" to attribute it to.
RUN_SHARED_ROW_KEYS = (RUN_USER_KEYS - {"username", "display_name", "slug", "rows_considered", "cost"}) | {
    "collection_slug",
    "row_title",
}
PICK_KEYS = {"rank", "title", "reason", "seed_title", "sources", "affinity", "year", "rating"}
TRACE_KEYS = {"username", "display_name", "status", "error", "reason", "trace", "breakdown", "requests"}
TRACE_REQUEST_KEYS = {"status", "detail", "arr_slug", "excluded"}
RUN_LOG_KEYS = {"seq", "ts", "run_id", "user", "stage", "counts", "reason"}
RUNS_SUMMARY_KEYS = {"total", "ok", "error", "last_finished", "last_status"}

REPORT_KEYS = {
    "window",
    "window_days",
    "since",
    "first_pick",
    "overall",
    "watch_sync",
    "coverage",
    "runs",
    "requests",
    "trend",
    "per_user",
    "per_row",
    "top_titles",
    "recent",
}


async def _noop_async(*_args, **_kwargs) -> None:
    """Stands in for the real watch sync, which would reach Plex."""


class TestRunsApi:
    def test_empty_list_then_trigger(self, client: TestClient):
        assert client.get("/api/runs").json() == []
        r = client.post("/api/runs", json={"dry_run": True})
        assert r.status_code == 202
        assert set(r.json()) == {"run_id"}
        run_id = r.json()["run_id"]
        # The run fails fast (Plex unconfigured) but must exist with a terminal/queued status.
        for _ in range(50):
            runs = client.get("/api/runs").json()
            if runs and runs[0]["status"] in ("error", "ok"):
                break
            time.sleep(0.05)
        assert runs[0]["id"] == run_id
        assert runs[0]["status"] == "error"  # no plex configured in this app instance
        assert set(runs[0]) == RUN_SUMMARY_KEYS
        detail = client.get(f"/api/runs/{run_id}")
        assert detail.status_code == 200
        assert set(detail.json()) == RUN_DETAIL_KEYS

    def test_a_skipped_users_reason_reaches_the_run_detail_without_looking_like_a_failure(self, client: TestClient):
        """The whole point of `reason` (issue #3): "skipped" has to explain itself in the UI. It is
        carried SEPARATELY from `error` because the run page counts every non-null error as a failed
        user — so a skip must arrive with a reason and a null error."""
        from shortlist.server.db.models import Run, RunUser

        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user_id,
                    status="skipped",
                    reason="There are no per-person rows to build.",
                )
            )
            session.commit()
            run_id = run.id

        result = client.get(f"/api/runs/{run_id}").json()["users"][0]

        assert result["status"] == "skipped"
        assert result["reason"] == "There are no per-person rows to build."
        assert result["error"] is None

    def _run_with_a_shared_row(self, client: TestClient, **overrides) -> int:
        """A finished run whose only work was a shared row — the exact shape of live run #37."""
        from shortlist.server.db.models import Run, RunSharedRow

        with client.app.state.sessions() as session:
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunSharedRow(
                    run_id=run.id,
                    collection_slug="popular",
                    **{
                        "row_title": "👥 Popular Movies on SFLIX",
                        "status": "ok",
                        "diff": {"added": ["Dune"], "removed": []},
                        "picks": [{"rank": 1, "title": "Dune", "reason": "3 people watched it"}],
                        "trace": {"gathers": [{"source": "popular"}]},
                        "breakdown": [{"row_slug": "popular", "library_key": "1"}],
                        **overrides,
                    },
                )
            )
            session.commit()
            return run.id

    def test_a_shared_row_appears_in_the_run_detail_with_every_key(self, client: TestClient):
        """A shared row belongs to no user, so it never appears in `users` — and before it had a
        record of its own, a run whose only work was a shared row rendered as a wall of skipped
        people with its actual output nowhere on screen."""
        run_id = self._run_with_a_shared_row(client)

        body = client.get(f"/api/runs/{run_id}").json()

        assert body["users"] == [], "a shared row is nobody's — it must not be faked into the people list"
        assert len(body["shared_rows"]) == 1
        row = body["shared_rows"][0]
        assert set(row) == RUN_SHARED_ROW_KEYS
        assert row["collection_slug"] == "popular"
        assert row["row_title"] == "👥 Popular Movies on SFLIX"
        assert [p["title"] for p in row["picks"]] == ["Dune"]
        assert set(row["picks"][0]) == PICK_KEYS, (
            "picks come from a JSON column nothing validates — a partial one must be filled in on "
            "read, not fail response validation and 500 the whole run page"
        )
        assert row["has_trace"] is True, "the trace flag is what puts a Trace link on the row"

    def test_a_shared_rows_trace_is_served_like_a_users(self, client: TestClient):
        """Same response model as the per-user trace so one view renders both: a shared row runs the
        same pipeline minus the per-person history stage. Its TITLE stands in for the person."""
        run_id = self._run_with_a_shared_row(client)

        body = client.get(f"/api/runs/{run_id}/rows/popular/trace").json()

        assert set(body) == TRACE_KEYS, "the trace view reads one shape — a fork here would need a second view"
        assert body["display_name"] == "👥 Popular Movies on SFLIX"
        assert body["trace"] == {"gathers": [{"source": "popular"}]}
        assert client.get(f"/api/runs/{run_id}/rows/nope/trace").status_code == 404

    def test_a_legacy_run_reports_no_shared_rows_rather_than_failing(self, client: TestClient):
        """Nothing is backfilled: a run recorded before this existed keeps exactly what it had. The
        key must still be present and empty, so the UI can say "not recorded for this run" instead of
        the page losing a field it renders around."""
        from shortlist.server.db.models import Run

        with client.app.state.sessions() as session:
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.commit()
            run_id = run.id

        assert client.get(f"/api/runs/{run_id}").json()["shared_rows"] == []

    def test_clearing_run_history_takes_the_shared_rows_with_it(self, client: TestClient):
        """`run_shared_rows.run_id` is a real FK and `PRAGMA foreign_keys` is ON, so a bulk delete
        that forgot this table would either strand the rows or fail the whole clear. A bulk ORM
        delete does not cascade in Python — the same reason the log lines are deleted explicitly."""
        from shortlist.server.db.models import Run, RunSharedRow

        run_id = self._run_with_a_shared_row(client)

        assert client.delete("/api/runs").status_code == 200
        with client.app.state.sessions() as session:
            assert session.query(Run).count() == 0
            assert session.query(RunSharedRow).filter_by(run_id=run_id).count() == 0

    def test_run_detail_carries_every_key_for_a_finished_and_a_still_pending_user(self, client: TestClient):
        """Both branches of the users list — the people the run has finished, and the ones it hasn't
        started yet (synthesised from `stats.expected_users` so the page can pre-populate mid-run) —
        must carry the SAME keys, and every one of them. The response model would otherwise be free
        to drop a field from one branch only, which is the hardest version of this bug to see."""
        from shortlist.server.db.models import PickRow, Run, RunUser

        with client.app.state.sessions() as session:
            done = session.query(User).filter_by(slug="sarah").first()
            run = Run(
                trigger="manual",
                status="running",
                # mike has not been reached yet; sarah has.
                stats={"expected_users": [{"username": "mike", "display_name": "Mike", "slug": "mike"}]},
            )
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=done.id, status="ok", duration_ms=1200, llm_tokens=42))
            session.add(
                PickRow(
                    run_id=run.id,
                    user_id=done.id,
                    tmdb_id=1,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    title="Dune",
                    reason="because you watched Arrival",
                    seed_title="Arrival",
                    sources="tmdb_similar",
                    affinity=0.5,
                )
            )
            session.commit()
            run_id = run.id

        body = client.get(f"/api/runs/{run_id}").json()

        assert set(body) == RUN_DETAIL_KEYS
        by_slug = {u["slug"]: u for u in body["users"]}
        assert set(by_slug) == {"sarah", "mike"}
        assert set(by_slug["sarah"]) == RUN_USER_KEYS
        assert set(by_slug["mike"]) == RUN_USER_KEYS, "a pending user is the same shape, not a subset"
        assert by_slug["mike"]["status"] == "pending"
        assert set(by_slug["sarah"]["picks"][0]) == PICK_KEYS

    def test_run_detail_carries_per_row_cost(self, client: TestClient):
        """`RunUser.cost` (Task 5/6) is a nullable JSON blob — this proves it survives serialization
        untouched: exact row durations, and which rows shared a pool."""
        from shortlist.server.db.models import Run, RunUser

        with client.app.state.sessions() as session:
            user = session.query(User).filter_by(slug="sarah").first()
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user.id,
                    status="ok",
                    duration_ms=1200,
                    llm_tokens=42,
                    cost={
                        "setup_ms": 300,
                        "rows": {"picked-for-you": {"duration_ms": 12040, "blocked_ms": 40}},
                        "pools": [
                            {
                                "label": "similar-titles",
                                "tokens": 500,
                                "exa_searches": 2,
                                "duration_ms": 900,
                                "rows": ["picked-for-you"],
                            }
                        ],
                    },
                )
            )
            session.commit()
            run_id = run.id

        body = client.get(f"/api/runs/{run_id}").json()
        user_out = body["users"][0]
        assert user_out["cost"]["rows"]["picked-for-you"]["duration_ms"] == 12040
        assert user_out["cost"]["pools"][0]["rows"] == ["picked-for-you"]

    def test_run_detail_reports_null_cost_for_a_legacy_run(self, client: TestClient):
        """A run recorded before 0069 must say "not recorded", never zero — a `RunUser` row simply
        left with `cost=None`, the same as every row written before the column existed."""
        from shortlist.server.db.models import Run, RunUser

        with client.app.state.sessions() as session:
            user = session.query(User).filter_by(slug="sarah").first()
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=user.id, status="ok", duration_ms=1200, llm_tokens=42))
            session.commit()
            run_id = run.id

        body = client.get(f"/api/runs/{run_id}").json()
        assert body["users"][0]["cost"] is None

    def test_pending_users_report_null_cost(self, client: TestClient):
        """A synthesised pending user must report `cost: null` too, so the payload shape does not
        change mid-run — the same reasoning as every other field on the pending branch."""
        from shortlist.server.db.models import Run, RunUser

        with client.app.state.sessions() as session:
            done = session.query(User).filter_by(slug="sarah").first()
            run = Run(
                trigger="manual",
                status="running",
                # mike has not been reached yet; sarah has.
                stats={"expected_users": [{"username": "mike", "display_name": "Mike", "slug": "mike"}]},
            )
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=done.id, status="ok", duration_ms=1200, llm_tokens=42))
            session.commit()
            run_id = run.id

        body = client.get(f"/api/runs/{run_id}").json()
        pending = [u for u in body["users"] if u["status"] == "pending"]
        assert pending and all(u["cost"] is None for u in pending)

    def test_run_detail_shows_the_display_name_not_the_bare_username(self, client: TestClient):
        """The runs view must read a person the same way the Users page does — nickname → Tautulli
        friendly name → username (User.display_name). The bug: it only emitted `username`, so a
        Tautulli friendly name populated after the run still showed the raw Plex login (SFLIX)."""
        from shortlist.server.db.models import Run, RunUser

        with client.app.state.sessions() as session:
            user = session.query(User).first()
            user.nickname = ""  # no owner override — the Tautulli name should win
            user.friendly_name = "Joe - Richard's Mate"
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=user.id, status="ok"))
            session.commit()
            run_id, raw_username = run.id, user.username

        result = client.get(f"/api/runs/{run_id}").json()["users"][0]

        assert result["username"] == raw_username  # still carried, for search + avatar
        assert result["display_name"] == "Joe - Richard's Mate"

    def test_the_run_page_gets_provenance_on_the_breakdown_it_actually_renders(self, client: TestClient):
        """The run page renders the stored `breakdown` blob, NOT the picks list — so provenance
        added to the picks table alone was invisible on the one screen built to answer "why was
        this picked?". Backfilled from the picks rows so existing runs explain themselves too,
        instead of staying blank until they are rebuilt."""
        from shortlist.server.db.models import PickRow, Run, RunUser

        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user_id,
                    status="ok",
                    # A blob written before provenance existed: picks without sources/affinity.
                    breakdown=[
                        {
                            "row_slug": "picked",
                            "row_title": "Picked",
                            "library_key": "1",
                            "picks": [{"rank": 1, "title": "Torchwood", "tmdb_id": 55, "media_type": "show"}],
                        }
                    ],
                )
            )
            session.add(
                PickRow(
                    run_id=run.id,
                    user_id=user_id,
                    tmdb_id=55,
                    media_type="show",
                    rating_key=1,
                    rank=1,
                    title="Torchwood",
                    sources="tmdb_similar",
                    affinity=0.28,
                )
            )
            session.commit()
            run_id = run.id

        entry = client.get(f"/api/runs/{run_id}").json()["users"][0]["breakdown"][0]["picks"][0]

        assert entry["sources"] == ["tmdb_similar"]
        assert entry["affinity"] == 0.28

    def test_a_breakdown_pick_with_no_matching_row_is_left_alone(self, client: TestClient):
        """Never invent provenance: a pick the picks table doesn't know about stays blank, which the
        UI renders as nothing rather than as a confident claim."""
        from shortlist.server.db.models import Run, RunUser

        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user_id,
                    status="ok",
                    breakdown=[
                        {
                            "row_slug": "picked",
                            "library_key": "1",
                            "picks": [{"rank": 1, "title": "Unknown", "tmdb_id": 999, "media_type": "movie"}],
                        }
                    ],
                )
            )
            session.commit()
            run_id = run.id

        entry = client.get(f"/api/runs/{run_id}").json()["users"][0]["breakdown"][0]["picks"][0]

        assert "sources" not in entry or entry["sources"] == []

    def test_the_trace_endpoint_carries_the_error_and_the_delivered_ending(self, client: TestClient):
        """The trace page must be able to explain a FAILED person and show what each library's row
        ended up with — so the trace endpoint returns `error`/`reason` and the per-library breakdown
        (the delivered ending of the flow), not just the partial stage trace."""
        from shortlist.server.db.models import PickRow, Run, RunUser

        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user_id,
                    status="error",
                    error="RuntimeError: every candidate source failed",
                    trace={"history": {"total": 3, "recent": [], "watched_movies": 3, "watched_shows": 0}},
                    breakdown=[
                        {
                            "row_slug": "picked",
                            "row_title": "Picked",
                            "library_key": "1",
                            "library_title": "Movies",
                            "picks": [{"rank": 1, "title": "Dune", "tmdb_id": 55, "media_type": "movie"}],
                        }
                    ],
                )
            )
            session.add(
                PickRow(
                    run_id=run.id,
                    user_id=user_id,
                    tmdb_id=55,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    title="Dune",
                    sources="tmdb_similar",
                    affinity=0.9,
                )
            )
            session.commit()
            run_id = run.id

        payload = client.get(f"/api/runs/{run_id}/users/{user_id}/trace").json()

        assert set(payload) == TRACE_KEYS
        assert payload["status"] == "error"
        assert "every candidate source failed" in payload["error"]
        # The delivered ending is present and provenance-enriched from the picks table.
        assert payload["breakdown"][0]["library_title"] == "Movies"
        assert payload["breakdown"][0]["picks"][0]["sources"] == ["tmdb_similar"]

    def test_the_trace_only_carries_request_outcomes_for_titles_it_mentions(self, client: TestClient):
        """The overlay used to be built from `SELECT * FROM request_candidates` — the WHOLE inbox,
        for one person's page, on a table that only grows (every night adds the titles no library
        held). Everything the overlay never looked up was then thrown away.

        Scoped to the tmdb ids this trace actually mentions, so the page still gets every outcome it
        can render and nothing else.
        """
        from shortlist.server.db.models import RequestCandidate, Run, RunUser

        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user_id,
                    status="ok",
                    # Nested two levels deep, which is where the real trace keeps its candidate
                    # returns — the id walk must not depend on a hand-listed set of stage names.
                    trace={"candidates": {"tmdb_similar": {"returns": [{"tmdb_id": 77, "title": "Wanted"}]}}},
                )
            )
            for tmdb_id, title in ((77, "Wanted"), (88, "Somebody else's"), (99, "Nobody's")):
                session.add(
                    RequestCandidate(
                        tmdb_id=tmdb_id, media_type="movie", title=title, status="sent", detail="sent to Radarr"
                    )
                )
            session.commit()
            run_id = run.id

        payload = client.get(f"/api/runs/{run_id}/users/{user_id}/trace").json()

        assert list(payload["requests"]) == ["77:movie"]
        assert set(payload["requests"]["77:movie"]) == TRACE_REQUEST_KEYS
        assert payload["requests"]["77:movie"]["status"] == "sent"

    def test_a_failed_run_exposes_why_not_just_that_it_failed(self, client: TestClient):
        """The reason lived only inside `stats`, which no client read — so a run that failed for a
        run-level reason (a share filter Plex refused) surfaced as a bare "Failed" and the operator
        had to go read container logs (issue #1)."""
        from shortlist.server.db.models import Run

        blocker = "LisaPlex1234 (plex account 12345): plex.tv rejected the share-filter update: HTTP 400"
        with client.app.state.sessions() as session:
            run = Run(
                trigger="manual",
                status="error",
                stats={
                    "users_ok": 1,
                    "users_error": 0,
                    "error": "privacy sync failed",
                    "promotion_blockers": [blocker],
                },
            )
            session.add(run)
            session.commit()
            run_id = run.id

        detail = client.get(f"/api/runs/{run_id}").json()
        listed = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)

        assert detail["error"] == "privacy sync failed"
        assert detail["promotion_blockers"] == [blocker]
        assert listed["error"] == "privacy sync failed"  # the list carries it too

    def test_cancel_a_run_that_isnt_running_returns_409(self, client: TestClient):
        # A run that already finished (or never existed) can't be cancelled — the endpoint says so
        # rather than pretending it stopped something.
        assert client.post("/api/runs/999999/cancel").status_code == 409

    def test_cancelling_a_live_run_answers_that_it_is_stopping(self, client: TestClient, monkeypatch):
        monkeypatch.setattr(client.app.state.run_service, "cancel_run", lambda run_id: True)

        r = client.post("/api/runs/1/cancel")

        assert r.status_code == 200
        assert r.json() == {"cancelling": True}

    def test_runs_can_be_filtered_to_one_row(self, client: TestClient):
        """?collection=<slug> narrows the list to runs that actually built that row (their picks carry
        its slug), so the Rows page can link a row to its own run history."""
        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).first().id
            built, other = Run(trigger="manual", status="ok"), Run(trigger="manual", status="ok")
            session.add_all([built, other])
            session.flush()
            # Only `built` produced a pick in the "hidden_gems" row.
            session.add(
                PickRow(
                    run_id=built.id,
                    user_id=uid,
                    tmdb_id=1,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    collection_slug="hidden_gems",
                    title="Dune",
                )
            )
            session.commit()
            built_id, other_id = built.id, other.id

        filtered = client.get("/api/runs?collection=hidden_gems").json()
        assert [r["id"] for r in filtered] == [built_id]  # only the run that built it
        all_ids = {r["id"] for r in client.get("/api/runs").json()}
        assert {built_id, other_id} <= all_ids  # unfiltered still shows both
        assert client.get("/api/runs?collection=nonexistent").json() == []

    def test_runs_summary_reports_counts(self, client: TestClient):
        from shortlist.server.db.models import Run

        with client.app.state.sessions() as session:
            session.add_all(
                [
                    Run(trigger="manual", status="ok"),
                    # "schedule", not "scheduled" — the scheduler writes the former, and `trigger` is
                    # now a Literal, so a made-up value here would 500 any endpoint that lists it.
                    Run(trigger="schedule", status="ok"),
                    Run(trigger="manual", status="error"),
                ]
            )
            session.commit()

        summary = client.get("/api/runs/summary").json()
        assert set(summary) == RUNS_SUMMARY_KEYS
        assert summary["total"] == 3
        assert summary["ok"] == 2
        assert summary["error"] == 1
        assert summary["last_status"] == "error"  # the newest run

    def test_clear_runs_deletes_history_but_keeps_picks_for_the_dashboard(self, client: TestClient):
        from shortlist.server.db.models import PickRow, Run, RunUser

        with client.app.state.sessions() as session:
            uid = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=uid, status="ok"))
            session.add(
                PickRow(run_id=run.id, user_id=uid, tmdb_id=1, media_type="movie", rating_key=1, rank=1, title="Dune")
            )
            session.commit()

        assert client.delete("/api/runs").json() == {"deleted": 1}
        assert client.get("/api/runs").json() == []
        with client.app.state.sessions() as session:
            assert session.query(PickRow).count() == 1  # picks survive (dashboard metrics preserved)
            pick = session.query(PickRow).first()
            assert pick.run_id is None  # detached from the deleted run
            assert session.query(RunUser).count() == 0  # per-user detail is gone

    def test_retention_prunes_runs_older_than_configured_months_but_keeps_picks(self, client: TestClient):
        """prune_runs deletes runs older than retention_months but keeps their picks (nulls run_id)
        so the dashboard's lifetime metrics survive. Runs inside the window are untouched."""

        from shortlist.server.db.models import PickRow, Run, RunUser
        from shortlist.server.services.run_persistence import prune_runs

        with client.app.state.sessions() as session:
            uid = session.query(User).first().id
            now = datetime.now(UTC)
            stale = Run(trigger="manual", status="ok", started_at=now - timedelta(days=100))  # beyond 3 months
            recent = Run(trigger="manual", status="ok", started_at=now - timedelta(days=2))  # inside
            session.add_all([stale, recent])
            session.flush()
            stale_id, recent_id = stale.id, recent.id
            for i, run in enumerate((stale, recent)):
                session.add(RunUser(run_id=run.id, user_id=uid, status="ok"))
                session.add(
                    PickRow(run_id=run.id, user_id=uid, tmdb_id=i, media_type="movie", rating_key=i, rank=1, title="X")
                )
            session.commit()

            prune_runs(session, retention_months=3)
            session.commit()

            kept_runs = {r.id for r in session.query(Run).all()}
            assert stale_id not in kept_runs  # old run pruned
            assert recent_id in kept_runs  # recent run kept
            assert session.query(RunUser).filter(RunUser.run_id == stale_id).count() == 0  # detail gone
            # Picks survive with a null run_id (dashboard metrics preserved).
            assert session.query(PickRow).count() == 2
            orphaned = session.query(PickRow).filter(PickRow.run_id.is_(None)).first()
            assert orphaned is not None and orphaned.tmdb_id == 0  # stale run's pick, now detached

    def test_pruning_a_run_never_touches_the_delivery_ledger(self, client: TestClient):
        """`deliveries` says which Plex collection IS which row for which person.

        It is keyed by SLUG rather than by foreign key precisely so it outlives runs and rows.
        Pruning it would strand real collections on real users' servers with nothing left that knows
        to clean them up — a silent, unrecoverable leak of other people's rows onto their screens.
        """
        from shortlist.server.db.models import Delivery, PickRow, Run
        from shortlist.server.services.run_persistence import prune_runs

        with client.app.state.sessions() as session:
            uid = session.query(User).first().id
            now = datetime.now(UTC)
            stale = Run(trigger="manual", status="ok", started_at=now - timedelta(days=100))
            session.add(stale)
            session.flush()
            session.add(
                PickRow(run_id=stale.id, user_id=uid, tmdb_id=1, media_type="movie", rating_key=1, rank=1, title="X")
            )
            session.add(Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=4242))
            session.commit()

            prune_runs(session, retention_months=3)
            session.commit()

            assert session.query(Run).count() == 0, "the old run should have gone"
            assert session.query(Delivery).count() == 1, "the delivery ledger must survive a prune"
            assert session.query(PickRow).count() == 1, "the impact ledger must survive a prune"

    def test_pruning_a_run_takes_its_activity_log_with_it(self, client: TestClient):
        """Narration is the storage hog and is meaningless without the run it describes."""
        from shortlist.server.db.models import Run, RunLogLine
        from shortlist.server.services.run_persistence import prune_runs

        with client.app.state.sessions() as session:
            now = datetime.now(UTC)
            stale = Run(trigger="manual", status="ok", started_at=now - timedelta(days=100))
            recent = Run(trigger="manual", status="ok", started_at=now - timedelta(days=2))
            session.add_all([stale, recent])
            session.flush()
            session.add_all(
                [
                    RunLogLine(run_id=stale.id, seq=0, stage="history", user_slug="sarah"),
                    RunLogLine(run_id=recent.id, seq=0, stage="history", user_slug="sarah"),
                ]
            )
            recent_id = recent.id
            session.commit()

            prune_runs(session, retention_months=3)
            session.commit()

            remaining = session.query(RunLogLine).all()
            assert [line.run_id for line in remaining] == [recent_id]

    def test_events_retention_is_off_by_default_and_prunes_when_set(self, client: TestClient):
        """The audit trail answers "what changed on whose share at 03:31" (plex-safety rule 10), so
        it is the one record kept forever unless the owner asks otherwise."""
        from shortlist.server.db.models import Event
        from shortlist.server.services.run_persistence import prune_events

        with client.app.state.sessions() as session:
            now = datetime.now(UTC)
            session.add_all(
                [
                    Event(scope="privacy.sync", level="info", message={}, ts=now - timedelta(days=400)),
                    Event(scope="privacy.sync", level="info", message={}, ts=now - timedelta(days=2)),
                ]
            )
            session.commit()

            prune_events(session, retention_months=0)  # the default
            session.commit()
            assert session.query(Event).count() == 2, "0 must mean keep forever"

            prune_events(session, retention_months=3)
            session.commit()
            assert session.query(Event).count() == 1

    def test_runs_list_pages_backwards_with_a_cursor(self, client: TestClient):
        """Cursor, not offset: runs are inserted while you read, so an offset would skip or repeat
        rows as the list shifts under you."""
        from shortlist.server.db.models import Run

        with client.app.state.sessions() as session:
            session.add_all([Run(trigger="manual", status="ok") for _ in range(5)])
            session.commit()

        first = client.get("/api/runs?limit=2").json()
        assert len(first) == 2
        second = client.get(f"/api/runs?limit=2&before_id={first[-1]['id']}").json()
        assert len(second) == 2
        # No overlap and strictly older — the two properties an offset loses under concurrent writes.
        assert {r["id"] for r in first}.isdisjoint({r["id"] for r in second})
        assert max(r["id"] for r in second) < min(r["id"] for r in first)

    def test_trigger_forwards_row_scope_to_the_run(self, client: TestClient, monkeypatch):
        """A manual run can target specific rows — collection_ids must reach start_run (the engine's
        build_only scope), not be silently dropped."""
        captured: dict = {}

        async def fake_start_run(*, trigger, dry_run, user_ids, collection_ids):
            captured.update(trigger=trigger, dry_run=dry_run, user_ids=user_ids, collection_ids=collection_ids)
            return 123

        monkeypatch.setattr(client.app.state.run_service, "start_run", fake_start_run)
        r = client.post("/api/runs", json={"collection_ids": [4, 7], "dry_run": True})
        assert r.status_code == 202 and r.json()["run_id"] == 123
        assert captured["collection_ids"] == [4, 7]
        assert captured["dry_run"] is True and captured["trigger"] == "manual"

    def test_effectiveness_report_counts_hits(self, client: TestClient):

        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add_all(
                [
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="picked",
                        title="A",
                        watched_at=now,
                    ),
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=2,
                        media_type="movie",
                        rating_key=2,
                        rank=2,
                        collection_slug="picked",
                        title="B",
                        watched_at=now,
                    ),
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=3,
                        media_type="movie",
                        rating_key=3,
                        rank=3,
                        collection_slug="picked",
                        title="C",
                    ),
                ]
            )
            session.commit()

        body = client.get("/api/report").json()
        assert body["window"] == "30"  # the default window
        assert body["overall"]["delivered"] == 3
        assert body["overall"]["watched"] == 2
        assert body["per_row"][0]["slug"] == "picked" and body["per_row"][0]["watched"] == 2
        assert any(u["watched"] == 2 for u in body["per_user"])
        assert len(body["recent"]) == 2 and body["recent"][0]["row"]
        assert body["coverage"]["users_with_picks"] == 1
        assert body["coverage"]["users_watched"] == 1
        assert {t["title"] for t in body["top_titles"]} == {"A", "B"}  # the two watched titles
        assert body["runs"]["total"] >= 1
        # Delivered seconds ago, so nothing has had its 30 days — a rate here would be a lie.
        assert body["overall"]["landing"]["rate"] is None

    def _seed_picks(self, client: TestClient, specs: list[tuple[int, int, int | None]]) -> int:
        """Seed picks as (tmdb_id, delivered_days_ago, watched_days_ago|None). Returns the user id."""
        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add_all(
                [
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=tmdb_id,
                        media_type="movie",
                        rating_key=tmdb_id,
                        rank=1,
                        collection_slug="picked",
                        title=f"T{tmdb_id}",
                        created_at=now - timedelta(days=delivered_ago),
                        watched_at=None if watched_ago is None else now - timedelta(days=watched_ago),
                    )
                    for tmdb_id, delivered_ago, watched_ago in specs
                ]
            )
            session.commit()
            return uid

    def test_report_payload_carries_every_key_the_dashboard_reads(self, client: TestClient):
        """The report is the largest shape this API returns and the dashboard reads nearly all of it,
        so its response model is the one most able to blank out a page by quietly dropping a key.
        Every level of it is pinned here — the top-level keys, each nested object, and one element of
        each list (which are empty, and therefore unassertable, until something has been watched)."""
        self._seed_picks(client, [(1, 2, 1), (2, 2, None)])

        body = client.get("/api/report?window=30").json()

        assert set(body) == REPORT_KEYS
        assert set(body["overall"]) == {
            "delivered",
            "watched",
            "watched_prev",
            "watched_delta",
            "finished",
            "bounced",
            "dropped",
            "avg_days_to_watch",
            "avg_days_to_watch_delta",
            "landing",
        }
        assert set(body["overall"]["landing"]) == {
            "delivered",
            "watched",
            "finished",
            "finished_rate",
            "rate",
            "cohort_from",
            "cohort_to",
            "matured_days",
        }
        assert set(body["watch_sync"]) == {"last", "next"}
        assert set(body["coverage"]) == {
            "users_enabled",
            "users_total",
            "users_with_picks",
            # Emitted, not derived: the UI cannot compute it from the two beside it, which count
            # differently-scoped populations.
            "users_idle",
            "users_watched",
            "users_watched_delta",
            "rows_enabled",
        }
        assert set(body["runs"]) == {
            "total",
            "in_window",
            "in_window_delta",
            "last_finished",
            "last_status",
            "errors_last",
        }
        assert set(body["requests"]) == {"sent", "pending", "watched_after_sent"}
        assert set(body["trend"][0]) == {"week", "watched", "finished"}
        assert set(body["per_user"][0]) == {
            "username",
            "display_name",
            "slug",
            "delivered",
            "watched",
            "finished",
        }
        assert set(body["per_row"][0]) == {
            "slug",
            "section_key",
            "library",
            "name",
            "deleted",
            "delivered",
            "watched",
            "finished",
        }
        assert set(body["top_titles"][0]) == {"tmdb_id", "media_type", "title", "watchers"}
        assert set(body["recent"][0]) == {
            "username",
            "display_name",
            "title",
            "media_type",
            "row",
            "library",
            "seed_title",
            "watched_at",
            # Whether they saw it OUT. `watched_at` is Plex's flag, which trips on a series' first
            # finished episode, so without this the feed reports "watched" for a show somebody
            # sampled one episode of — on the page that exists to report whether picks land.
            "finished_at",
        }
        # `all` is the branch with no previous period: the deltas and `since` go null, but not one
        # key may go missing with them.
        assert set(client.get("/api/report?window=all").json()) == REPORT_KEYS

    def test_report_window_excludes_what_falls_outside_it(self, client: TestClient):
        """The whole point of windowing: an old pick must stop dragging on today's numbers.

        Lifetime-cumulative was the bug — a pick can only ever be CREDITED within 30 days of
        delivery, but the old denominator kept it forever, so the figure measured install age.
        """
        self._seed_picks(
            client,
            [
                (1, 2, 1),  # delivered and watched inside every window
                (2, 200, 199),  # ancient: only "all" should see it
            ],
        )

        recent = client.get("/api/report?window=30").json()
        assert recent["overall"]["watched"] == 1
        assert recent["overall"]["delivered"] == 1

        lifetime = client.get("/api/report?window=all").json()
        assert lifetime["overall"]["watched"] == 2
        assert lifetime["overall"]["delivered"] == 2
        assert lifetime["window_days"] is None
        assert lifetime["overall"]["watched_delta"] is None  # "all" has no previous period

    def test_report_reports_the_oldest_pick_so_the_ui_can_explain_a_flat_selector(self, client: TestClient):
        """On a young install every window covers all the data, so 7/30/90/all return identical
        numbers and the selector looks broken. `first_pick` is what lets the UI say why."""
        empty = client.get("/api/report?window=30").json()
        assert empty["first_pick"] is None, "no picks yet — nothing to date the history from"

        self._seed_picks(client, [(1, 2, 1), (2, 200, 199)])

        report = client.get("/api/report?window=30").json()
        # The OLDEST pick, not the oldest one inside the window — the point is to compare the two.
        assert report["first_pick"] is not None
        assert report["first_pick"] < report["since"], "200 days old, so older than a 30-day window"

    def test_report_landing_rate_only_counts_picks_that_had_their_full_chance(self, client: TestClient):
        """The matured cohort. A pick delivered yesterday cannot have been 'watched within 30 days'
        yet, so counting it in the denominator drags the rate toward zero for no reason at all."""
        self._seed_picks(
            client,
            [
                (1, 40, 39),  # matured, watched  -> numerator + denominator
                (2, 40, None),  # matured, unwatched -> denominator only
                (3, 1, None),  # too young to judge -> neither
                (4, 1, None),
                (5, 1, None),
            ],
        )

        landing = client.get("/api/report?window=90").json()["overall"]["landing"]
        assert landing["delivered"] == 2, "the three young picks must not be in the denominator"
        assert landing["watched"] == 1
        assert landing["rate"] == 0.5
        assert landing["matured_days"] == 30

    def test_report_compares_against_the_previous_equal_period(self, client: TestClient):
        self._seed_picks(
            client,
            [
                (1, 3, 3),  # this window
                (2, 4, 4),  # this window
                (3, 40, 40),  # the previous 30 days
            ],
        )

        overall = client.get("/api/report?window=30").json()["overall"]
        assert overall["watched"] == 2
        assert overall["watched_prev"] == 1
        assert overall["watched_delta"] == 1

    def test_report_breakdowns_rank_by_what_was_watched_not_by_rate(self, client: TestClient):
        """`1/31` used to outrank `3/103` — one data point beating three times the evidence, because
        3.2% > 2.9%. Counts, sorted by watched, is what the list is actually for."""
        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            users = session.query(User).order_by(User.id).limit(2).all()
            assert len(users) == 2, "this test needs two users on the roster"
            small, big = users[0].id, users[1].id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)

            def pick(uid, tmdb_id, watched):
                return PickRow(
                    run_id=run.id,
                    user_id=uid,
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    rating_key=tmdb_id,
                    rank=1,
                    collection_slug="picked",
                    title=f"T{tmdb_id}",
                    created_at=now - timedelta(days=1),
                    watched_at=now if watched else None,
                )

            # small: 1 of 4 watched (25%). big: 3 of 30 watched (10%) — but three real watches.
            session.add_all([pick(small, 1, True)] + [pick(small, i, False) for i in range(2, 5)])
            session.add_all([pick(big, 100 + i, i < 3) for i in range(30)])
            session.commit()

        per_user = client.get("/api/report?window=30").json()["per_user"]
        assert per_user[0]["watched"] == 3, "the person who watched most comes first"
        assert "hit_rate" not in per_user[0], "no per-person rate: at these sample sizes it is noise"

    def test_report_does_not_credit_a_request_watched_before_it_was_sent(self, client: TestClient):
        """`watched_after_sent` was a plain set intersection with no ordering check, so a title
        watched, then deleted from the library, then re-requested, counted as a request that paid off."""
        from shortlist.server.db.models import PickRow, RequestCandidate, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add_all(
                [
                    # Watched ten days ago...
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=77,
                        media_type="movie",
                        rating_key=77,
                        rank=1,
                        collection_slug="picked",
                        title="Watched First",
                        created_at=now - timedelta(days=12),
                        watched_at=now - timedelta(days=10),
                    ),
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=88,
                        media_type="movie",
                        rating_key=88,
                        rank=2,
                        collection_slug="picked",
                        title="Watched After",
                        created_at=now - timedelta(days=12),
                        watched_at=now - timedelta(days=1),
                    ),
                ]
            )
            # ...but only requested five days ago, i.e. AFTER the watch. Not a request that paid off.
            session.add(
                RequestCandidate(
                    tmdb_id=77,
                    media_type="movie",
                    title="Watched First",
                    status="sent",
                    updated_at=now - timedelta(days=5),
                )
            )
            # Requested first, watched after — this one genuinely paid off.
            session.add(
                RequestCandidate(
                    tmdb_id=88,
                    media_type="movie",
                    title="Watched After",
                    status="sent",
                    updated_at=now - timedelta(days=5),
                )
            )
            session.commit()

        requests = client.get("/api/report?window=30").json()["requests"]
        assert requests["sent"] == 2
        assert requests["watched_after_sent"] == 1

    def test_report_uses_the_real_send_time_not_a_timestamp_that_drifts(self, client: TestClient):
        """`updated_at` has `onupdate`, so clearing an old title from the Sent log bumped it and pulled
        a months-old request into a recent window. `sent_at` is stamped once and cannot drift."""
        from shortlist.server.db.models import PickRow, RequestCandidate, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add(
                PickRow(
                    run_id=run.id,
                    user_id=uid,
                    tmdb_id=55,
                    media_type="movie",
                    rating_key=55,
                    rank=1,
                    collection_slug="picked",
                    title="Watched Before The Send",
                    created_at=now - timedelta(days=200),
                    watched_at=now - timedelta(days=190),
                )
            )
            # Sent long ago, then TOUCHED recently (hidden from the Sent log) — `updated_at` is now
            # inside the window while the real send is not.
            session.add(
                RequestCandidate(
                    tmdb_id=55,
                    media_type="movie",
                    title="Watched Before The Send",
                    status="sent",
                    sent_at=now - timedelta(days=180),
                    updated_at=now - timedelta(days=1),
                )
            )
            session.commit()

        requests = client.get("/api/report?window=30").json()["requests"]
        assert requests["sent"] == 0, "a request sent 180 days ago is not a send in the last 30"
        assert requests["watched_after_sent"] == 0

    def test_report_still_reads_a_request_sent_before_sent_at_existed(self, client: TestClient):
        """Back-compat: rows written before the column fall back to `updated_at`, which is exactly the
        behaviour they already had — no backfill, nothing silently dropped."""
        from shortlist.server.db.models import PickRow, RequestCandidate, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add(
                PickRow(
                    run_id=run.id,
                    user_id=uid,
                    tmdb_id=66,
                    media_type="movie",
                    rating_key=66,
                    rank=1,
                    collection_slug="picked",
                    title="Legacy Sent",
                    created_at=now - timedelta(days=20),
                    watched_at=now - timedelta(days=2),
                )
            )
            session.add(
                RequestCandidate(
                    tmdb_id=66,
                    media_type="movie",
                    title="Legacy Sent",
                    status="sent",
                    sent_at=None,  # predates the column
                    updated_at=now - timedelta(days=10),
                )
            )
            session.commit()

        requests = client.get("/api/report?window=30").json()["requests"]
        assert requests["sent"] == 1
        assert requests["watched_after_sent"] == 1

    def test_report_falls_back_to_the_default_window_on_a_bogus_value(self, client: TestClient):
        body = client.get("/api/report?window=nonsense").json()
        assert body["window"] == "30"

    def test_report_splits_a_multi_library_row_per_library(self, client: TestClient):
        """A row targeting >1 library is one Plex collection PER library, so the report tracks each
        library as its own line — with its own hit rate and the library's own {library_name} name."""

        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).filter_by(slug="sarah").first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add_all(
                [
                    # The "picked" row delivered a movie into Movies (watched) and a show into TV (not).
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="picked",
                        section_key="10",
                        library="Movies",
                        title="Dune",
                        watched_at=now,
                    ),
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=2,
                        media_type="show",
                        rating_key=2,
                        rank=1,
                        collection_slug="picked",
                        section_key="20",
                        library="TV",
                        title="Shogun",
                    ),
                ]
            )
            session.commit()

        body = client.get("/api/report").json()
        by_library = {r["library"]: r for r in body["per_row"]}
        assert by_library["Movies"]["delivered"] == 1 and by_library["Movies"]["watched"] == 1
        assert by_library["TV"]["delivered"] == 1 and by_library["TV"]["watched"] == 0
        # The {library_name} template renders each library's own name.
        assert by_library["Movies"]["name"] == "✨ Movies Picked for You"
        assert by_library["TV"]["name"] == "✨ TV Picked for You"
        # Overall still dedupes to distinct (user, title): two titles, one watched — not skewed by the split.
        assert body["overall"]["delivered"] == 2 and body["overall"]["watched"] == 1

    def test_report_names_a_deleted_rows_history_by_slug_not_the_default_rows_name(self, client: TestClient):
        """Deleting a row keeps its picks, and those picks must not borrow the DEFAULT row's name.

        Regression: `row_label` fell back to the default template for ANY unknown slug, so every
        deleted row rendered "✨ Movies Picked for You" — the dashboard listed the default row over
        and over with wildly different counts and no way to tell which line was which.
        """

        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).filter_by(slug="sarah").first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add_all(
                [
                    # The live default row.
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="picked",
                        section_key="10",
                        library="Movies",
                        title="Dune",
                    ),
                    # A row the owner deleted; its history stays, but the row is gone.
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=2,
                        media_type="movie",
                        rating_key=2,
                        rank=1,
                        collection_slug="date-night",
                        section_key="10",
                        library="Movies",
                        title="Arrival",
                    ),
                ]
            )
            session.commit()

        per_row = client.get("/api/report").json()["per_row"]
        by_slug = {r["slug"]: r for r in per_row}
        assert by_slug["picked"]["name"] == "✨ Movies Picked for You"
        assert by_slug["picked"]["deleted"] is False
        # The deleted row keeps its own identity instead of impersonating the default row.
        assert by_slug["date-night"]["name"] == "date-night"
        assert by_slug["date-night"]["deleted"] is True
        # Which is the whole point: no two lines in the same library share a name.
        names = [(r["name"], r["library"]) for r in per_row]
        assert len(names) == len(set(names))

    def test_report_still_names_legacy_blank_slug_picks_as_the_default_row(self, client: TestClient):
        """Pre-0004 picks predate multi-row and carry a blank slug — they ARE the default row, so the
        deleted-row fallback above must not relabel them as a row called ""."""

        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).filter_by(slug="sarah").first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                PickRow(
                    run_id=run.id,
                    user_id=uid,
                    tmdb_id=1,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    collection_slug="",
                    section_key="10",
                    library="Movies",
                    title="Dune",
                )
            )
            session.commit()

        per_row = client.get("/api/report").json()["per_row"]
        assert len(per_row) == 1
        assert per_row[0]["name"] == "✨ Movies Picked for You"
        assert per_row[0]["slug"] == "picked" and per_row[0]["deleted"] is False

    def test_report_tracks_a_title_moving_rows_and_a_second_watcher(self, client: TestClient):
        """The full lifecycle: a title watched in one row, later moved to another row (no re-credit,
        the watch predates it), then watched by a DIFFERENT person in that other row. Overall must not
        double-count; each row is credited only for the watches that happened while the title was in it."""

        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            x = session.query(User).filter_by(slug="sarah").first().id
            y = session.query(User).filter_by(slug="mike").first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add_all(
                [
                    # X got Dune in row A and watched it there.
                    PickRow(
                        run_id=run.id,
                        user_id=x,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="rowA",
                        section_key="10",
                        library="Movies",
                        title="Dune",
                        watched_at=now,
                    ),
                    # Dune later moved to row B for X — but X already watched it, so row B gets no credit.
                    PickRow(
                        run_id=run.id,
                        user_id=x,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="rowB",
                        section_key="10",
                        library="Movies",
                        title="Dune",
                    ),
                    # Y got Dune in row B and watched it there.
                    PickRow(
                        run_id=run.id,
                        user_id=y,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="rowB",
                        section_key="10",
                        library="Movies",
                        title="Dune",
                        watched_at=now,
                    ),
                ]
            )
            session.commit()

        body = client.get("/api/report").json()
        # Distinct (user, title): (X, Dune) + (Y, Dune) = 2 delivered; both watched = 2. The move is not
        # a third recommendation.
        assert body["overall"]["delivered"] == 2 and body["overall"]["watched"] == 2
        by_slug = {r["slug"]: r for r in body["per_row"]}
        assert by_slug["rowA"]["delivered"] == 1 and by_slug["rowA"]["watched"] == 1  # X only
        # Row B holds both people's copies; only Y's was watched while the title lived there.
        assert by_slug["rowB"]["delivered"] == 2 and by_slug["rowB"]["watched"] == 1

    def test_recent_feed_shows_one_line_per_watch_not_per_delivery(self, client: TestClient):
        """A title re-recommended nightly has one pick row PER RUN, and the watch-sync stamps the same
        watched_at on every one of them. The feed must still show that watch ONCE — the dashboard was
        listing "Jarrah watched Beckham · 40m ago" five times, once per delivery."""

        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).filter_by(slug="sarah").first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add_all(
                [
                    # Three nightly deliveries of Beckham, all crediting the one watch...
                    *(
                        PickRow(
                            run_id=run.id,
                            user_id=uid,
                            tmdb_id=1,
                            media_type="show",
                            rating_key=1,
                            rank=1,
                            collection_slug="picked",
                            section_key="20",
                            library="TV",
                            title="Beckham",
                            created_at=now - timedelta(days=n),
                            watched_at=now - timedelta(minutes=40),
                        )
                        for n in (3, 2, 1)
                    ),
                    # ...and a second, genuinely different title, which must still get its own line.
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=2,
                        media_type="movie",
                        rating_key=2,
                        rank=1,
                        collection_slug="picked",
                        section_key="10",
                        library="Movies",
                        title="Day & Night",
                        watched_at=now - timedelta(days=1),
                    ),
                ]
            )
            session.commit()

        recent = client.get("/api/report").json()["recent"]
        assert [(w["title"], w["row"]) for w in recent] == [
            ("Beckham", "✨ TV Picked for You"),
            ("Day & Night", "✨ Movies Picked for You"),
        ]
        # The line carries the time of the watch, not of any one delivery.
        assert recent[0]["watched_at"].startswith((now - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M"))

    def test_report_sync_queues_a_real_job_so_the_ui_can_see_it(self, client: TestClient, monkeypatch):
        """The dashboard's "sync now" button. It returns immediately (202) — the sync fetches history
        for every user, so it cannot be awaited on the request.

        It must leave a `jobs` ROW behind. It used to call `sync_watched_background()` directly, which
        did the work but recorded nothing: no toast, nothing in the header's activity popover, nothing
        in Jobs -> Activity. The Jobs page only seemed to show it because its progress bar listens to
        the SSE stream instead. Asserting on the row is what stops that divergence coming back.
        """
        from shortlist.server.db.models import Job

        monkeypatch.setattr(client.app.state.run_service, "sync_watched", _noop_async, raising=False)

        r = client.post("/api/report/sync")

        assert r.status_code == 202
        assert r.json()["started"] is True
        with client.app.state.sessions() as session:
            queued = session.query(Job).filter(Job.kind == "sync.history").all()
        assert len(queued) == 1, "the dashboard button must go through the job queue like every other"
        assert r.json()["job_id"] == queued[0].id

    def test_deleted_rows_lists_orphaned_history_and_says_what_clearing_it_removed(self, client: TestClient):
        """Both endpoints of the one destructive action on the dashboard, and both branches of the
        DELETE: nothing eligible (naming a row that still exists) and something eligible."""
        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            for tmdb_id, slug in ((1, "picked"), (2, "date-night"), (3, "date-night")):
                session.add(
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=tmdb_id,
                        media_type="movie",
                        rating_key=tmdb_id,
                        rank=1,
                        collection_slug=slug,
                        title=f"T{tmdb_id}",
                    )
                )
            session.commit()

        listed = client.get("/api/report/deleted-rows").json()
        assert [r["slug"] for r in listed] == ["date-night"]  # "picked" still exists
        assert set(listed[0]) == {"slug", "picks", "first_seen", "last_seen"}
        assert listed[0]["picks"] == 2

        # A live row's slug is not eligible, and refusing still answers in full rather than 404ing.
        assert client.delete("/api/report/deleted-rows?slug=picked").json() == {
            "cleared": 0,
            "picks": 0,
            "slugs": [],
        }

        cleared = client.delete("/api/report/deleted-rows").json()
        assert set(cleared) == {"cleared", "picks", "slugs"}
        assert cleared == {"cleared": 1, "picks": 2, "slugs": ["date-night"]}
        assert client.get("/api/report/deleted-rows").json() == []
        with client.app.state.sessions() as session:
            assert session.query(PickRow).count() == 1, "the live row's history must survive"

    def test_unknown_run_404(self, client: TestClient):
        assert client.get("/api/runs/424242").status_code == 404

    def test_run_log_endpoint_returns_a_list(self, client: TestClient):
        # A run whose process never buffered a log returns an empty list, not a 404 — the page seeds
        # its activity feed from this and tops it up over SSE.
        r = client.get("/api/runs/424242/log")
        assert r.status_code == 200 and r.json() == []

    def test_run_log_pages_from_a_seq_and_downloads_as_text(self, client: TestClient):
        from shortlist.server.db.models import Run

        service = client.app.state.run_service
        with client.app.state.sessions() as session:
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.commit()
            run_id = run.id

        sink = service._new_run_log(run_id)
        sink({"stage": "history", "user": "sarah", "counts": {"titles": 113}})
        sink({"stage": "skipped", "user": "mike", "counts": {}, "reason": "no watch history yet"})
        sink({"stage": "finished", "user": "Shortlist", "counts": {"ok": 1}})

        full = client.get(f"/api/runs/{run_id}/log").json()
        assert [e["stage"] for e in full] == ["history", "skipped", "finished"]
        # `reason` is the one key the writer omits when there's nothing to say — both branches come
        # back with the same keys, so a client never has to guess whether a line is truncated.
        assert set(full[0]) == RUN_LOG_KEYS and full[0]["reason"] is None
        assert set(full[1]) == RUN_LOG_KEYS and full[1]["reason"] == "no watch history yet"

        # Topping up: only what the client hasn't already got.
        tail = client.get(f"/api/runs/{run_id}/log?after_seq=0").json()
        assert [e["stage"] for e in tail] == ["skipped", "finished"]

        text = client.get(f"/api/runs/{run_id}/log?format=text")
        assert text.headers["content-type"].startswith("text/plain")
        assert "sarah" in text.text and "titles=113" in text.text

    def test_run_log_read_back_from_the_durable_rows_carries_the_same_keys(self, client: TestClient):
        """The other half of the log endpoint: a run this process never buffered is served from
        `run_log_lines` instead of the in-memory tail. Same shape, so a page reload after a restart
        renders identically."""
        from shortlist.server.db.models import Run, RunLogLine

        with client.app.state.sessions() as session:
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(RunLogLine(run_id=run.id, seq=0, stage="history", user_slug="sarah", counts={"titles": 7}))
            session.add(RunLogLine(run_id=run.id, seq=1, stage="skipped", user_slug="mike", reason="paused"))
            session.commit()
            run_id = run.id

        lines = client.get(f"/api/runs/{run_id}/log").json()

        assert [set(line) for line in lines] == [RUN_LOG_KEYS, RUN_LOG_KEYS]
        assert lines[0]["counts"] == {"titles": 7} and lines[0]["ts"] is not None
        assert lines[0]["reason"] is None and lines[1]["reason"] == "paused"


class TestClosedSetFieldsMatchWhatTheCodeWrites:
    """Every value the producing code can write must survive its response model.

    `trigger`, `window` and a trace request's `status` are now `Literal[...]` rather than `str`, so
    the OpenAPI schema carries the enum and the SPA stops re-declaring it by hand. That is only safe
    while the set is a superset of what the writers emit: a Literal is validated on the way OUT, and
    a value outside it does not degrade — it raises, and the endpoint 500s with real data in the
    database. So each of these drives the value through the real handler rather than asserting the
    annotation.
    """

    def test_every_trigger_the_producers_write_survives_the_runs_endpoints(self, client: TestClient, monkeypatch):
        """Captured from the producers, not hand-listed — that is the whole point.

        Exactly two code paths start a run: `POST /api/runs` and the scheduler's cron job, and both
        insert through `RunService.start_run`. This intercepts that one seam to record what each
        really passes, then pushes every captured word back through the real response model. Add a
        third producer with a new word and this fails here, instead of 500ing the Runs page.

        (`wizard` is in the Literal because `runs.trigger` has documented it since 0001, but no
        producer writes it — the wizard's first run is a `POST /api/runs` like any other.)
        """
        import asyncio

        from shortlist.server.db.models import Run
        from shortlist.server.scheduler import _make_job

        captured: list[str] = []

        async def fake_start_run(*, trigger, dry_run, user_ids=None, collection_ids=None):
            captured.append(trigger)
            return 1

        monkeypatch.setattr(client.app.state.run_service, "start_run", fake_start_run)
        client.post("/api/runs", json={"dry_run": True})
        # The scheduler's job body, called directly: it is the only place "schedule" is written, and
        # waiting for a cron to fire is not a test. `start_run` is stubbed, so nothing is scheduled.
        asyncio.run(_make_job(client.app, "30 3 * * *", [])())

        assert set(captured) == {"manual", "schedule"}, "a new producer must be added to the Literal"

        with client.app.state.sessions() as session:
            wanted = {}
            for trigger in sorted(set(captured)):
                run = Run(trigger=trigger, status="ok")
                session.add(run)
                session.flush()
                wanted[run.id] = trigger
            session.commit()

        listed = {r["id"]: r["trigger"] for r in client.get("/api/runs").json()}
        for run_id, trigger in wanted.items():
            assert listed[run_id] == trigger
            assert client.get(f"/api/runs/{run_id}").json()["trigger"] == trigger

    def test_started_at_is_always_a_timestamp_never_null(self, client: TestClient):
        """`runs.started_at` is NOT NULL with an ORM-side default, so the model declares `str`, not
        `str | None` — it only ever read optional because `iso_utc` is typed that way. A row inserted
        WITHOUT naming the column is the case that proves the default fires."""
        from shortlist.server.db.models import Run

        with client.app.state.sessions() as session:
            run = Run(trigger="manual", status="ok")  # no started_at: the default must supply one
            session.add(run)
            session.commit()
            run_id = run.id

        listed = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)

        assert isinstance(listed["started_at"], str) and listed["started_at"]
        assert client.get(f"/api/runs/{run_id}").json()["started_at"] == listed["started_at"]

    @pytest.mark.parametrize("window", ["7", "30", "90", "all"])
    def test_every_selectable_report_window_is_echoed_back(self, client: TestClient, window: str):
        body = client.get(f"/api/report?window={window}").json()

        assert body["window"] == window

    def test_an_unknown_window_is_normalised_before_it_reaches_the_payload(self, client: TestClient):
        """Why the Literal is safe despite `window` coming straight off the query string:
        `report_service.effectiveness` falls back to the default for anything it does not know."""
        assert client.get("/api/report?window=nonsense").json()["window"] == "30"

    @pytest.mark.parametrize("status", ["pending", "sent", "rejected"])
    def test_every_request_status_survives_the_trace_overlay(self, client: TestClient, status: str):
        """The three `request_candidates.status` values, written by `api/requests.py` (rejected,
        pending, sent) and `run_persistence` (pending, sent)."""
        from shortlist.server.db.models import RequestCandidate, Run, RunUser

        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user_id,
                    status="ok",
                    trace={"candidates": {"tmdb_similar": {"returns": [{"tmdb_id": 4242, "title": "Wanted"}]}}},
                )
            )
            session.add(RequestCandidate(tmdb_id=4242, media_type="movie", title="Wanted", status=status))
            session.commit()
            run_id = run.id

        payload = client.get(f"/api/runs/{run_id}/users/{user_id}/trace").json()

        assert payload["requests"]["4242:movie"]["status"] == status
