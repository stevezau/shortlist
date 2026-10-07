"""SDK assertions for running cancellation and permission-filtered pagination."""

from __future__ import annotations

import json
import threading
from copy import deepcopy

import pytest

from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.types import ASSISTANT_CAPABILITIES
from shortlist.server.db.models import Collection, Event, RequestCandidate, Run, RunSharedRow, RunUser
from tests.integration.test_assistant_mcp_tool_matrix import (
    OWNER_ACCOUNT_ID,
    McpWire,
    _apply,
    _wait_for_operation,
    _wire_app,
)

pytestmark = pytest.mark.integration


def test_sdk_cancellation_stops_an_actually_running_engine_and_releases_the_writer(tmp_path, monkeypatch):
    from shortlist.server.services import run_service

    entered = threading.Event()
    release = threading.Event()
    original_engine_run = run_service.engine_run
    observed = []

    def gated_engine(ctx, profiles):
        entered.set()
        assert release.wait(30), "test did not release the real engine"
        observed.append(ctx.cancelled())
        return original_engine_run(ctx, profiles)

    monkeypatch.setattr(run_service, "engine_run", gated_engine)
    with _wire_app(tmp_path, monkeypatch) as (wire, app, state):
        app.state.scheduler.pause()
        before = deepcopy(state.collections)
        plan = wire.exercise(
            [
                (
                    "shortlist_plan_run",
                    {
                        "row_ids": [10],
                        "person_ids": [1],
                        "dry_run": True,
                        "max_provider_calls": 0,
                        "max_images": 0,
                        "max_acquisitions": 0,
                    },
                )
            ]
        )["shortlist_plan_run"]["data"]
        receipt = _apply(wire, plan["change_id"], "cancel-running")
        try:
            assert entered.wait(30), "real worker never reached the engine"
            run_id = receipt["result"]["run_id"]
            with app.state.sessions() as session:
                running = session.get(Run, run_id)
                assert running.status == "running"
                assert running.began_at is not None and running.finished_at is None
            cancelled = wire.exercise([("shortlist_cancel_operation", {"operation_id": receipt["operation_id"]})])[
                "shortlist_cancel_operation"
            ]["data"]
            assert cancelled == {"run_id": run_id, "status": "stopping", "cancel_requested": True}
            assert app.state.run_service.is_running(), "the writer stays reserved until cooperative cleanup finishes"
            with app.state.sessions() as session:
                assert session.get(Run, run_id).stats["cancel_requested"] is True
        finally:
            release.set()
        assert _wait_for_operation(wire, receipt["operation_id"])["status"] == "cancelled"
        assert observed == [True], "the real engine must receive the cancellation signal"
        report = wire.exercise([("shortlist_get_run_report", {"id": run_id})])["shortlist_get_run_report"]["data"]
        assert report["status"] == "aborted"
        with app.state.sessions() as session:
            stopped = session.get(Run, run_id)
            assert stopped.began_at is not None and stopped.finished_at is not None
            assert session.query(Event).filter(Event.scope == "assistant.run_cancelled").count() == 1
        assert not app.state.run_service.is_running()
        assert state.collections == before
        assert app.state.matrix_provider_calls == []


def _constrained_wire(wire, app, *, capabilities=ASSISTANT_CAPABILITIES):
    repository = app.state.assistant_auth.repository
    grant = repository.create_grant(
        owner_account_id=OWNER_ACCOUNT_ID,
        client_id="pagination-scope-client",
        name="Pagination scope test",
        preset=GrantPreset.INSPECT,
        capabilities=capabilities,
        constraints=GrantConstraints(
            row_ids=frozenset({10, 12}), library_keys=frozenset({"1"}), setting_groups=frozenset({"system"})
        ),
    )
    return McpWire(wire.url, repository.issue_local_credential(grant.grant_id).take())


def _all_pages(wire, name, *, key):
    pages = []
    offset = 0
    while True:
        result = wire.exercise([(name, {"limit": 1, "offset": offset})])[name]["data"]
        assert len(result["items"]) <= 1
        pages.extend(result["items"])
        if result["next_offset"] is None:
            break
        assert result["next_offset"] == offset + 1
        offset = result["next_offset"]
        assert offset < 20
    assert len({item[key] for item in pages}) == len(pages)
    empty = wire.exercise([(name, {"limit": 1, "offset": 100})])[name]["data"]
    assert empty == {"items": [], "next_offset": None}
    for invalid in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        wire.expect_error(name, invalid)
    return pages


def test_sdk_pagination_filters_rows_runs_and_request_provenance_before_returning_pages(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch) as (wire, app, _state):
        with app.state.sessions() as session:
            session.add_all(
                [
                    Collection(id=11, slug="foreign-secret-row", name="Foreign secret", library_keys=["1"]),
                    Collection(id=12, slug="second-allowed", name="Second permitted", library_keys=["1"]),
                    Run(id=100, trigger="manual", status="ok"),
                    Run(id=101, trigger="manual", status="ok"),
                    Run(id=102, trigger="manual", status="ok"),
                ]
            )
            session.flush()
            session.add_all(
                [
                    RunUser(
                        run_id=100,
                        user_id=1,
                        rows_considered={"matrix-row": "due", "foreign-secret-row": "due"},
                        trace={"history": "PRIVATE-WATCH-HISTORY"},
                        error="PRIVATE-ERROR",
                    ),
                    RunSharedRow(run_id=101, collection_slug="foreign-secret-row", status="ok", picks=[]),
                    RunSharedRow(run_id=102, collection_slug="second-allowed", status="ok", picks=[]),
                    RequestCandidate(
                        id=100, tmdb_id=9001, media_type="movie", title="Permitted first", row_slug="matrix-row"
                    ),
                    RequestCandidate(
                        id=101, tmdb_id=9002, media_type="movie", title="FOREIGN-REQUEST", row_slug="foreign-secret-row"
                    ),
                    RequestCandidate(
                        id=102, tmdb_id=9003, media_type="movie", title="Permitted second", row_slug="second-allowed"
                    ),
                    RequestCandidate(id=103, tmdb_id=9004, media_type="movie", title="UNSCOPED-REQUEST", row_slug=None),
                    RequestCandidate(
                        id=104,
                        tmdb_id=9005,
                        media_type="movie",
                        title="HIDDEN-REQUEST",
                        row_slug="matrix-row",
                        hidden=True,
                    ),
                ]
            )
            session.commit()
        limited = _constrained_wire(wire, app)
        rows = _all_pages(limited, "shortlist_list_rows", key="id")
        assert {item["id"] for item in rows} == {10, 12}
        runs = _all_pages(limited, "shortlist_list_runs", key="run_id")
        assert [item["run_id"] for item in runs] == [102, 100]
        assert runs[1]["people"][0]["rows_considered"] == {"matrix-row": "due"}
        requests = _all_pages(limited, "shortlist_list_requests", key="id")
        assert [item["id"] for item in requests] == [102, 100]
        assert [item["title"] for item in requests] == ["Permitted second", "Permitted first"]
        for tool, request in (("shortlist_get_row", {"id": 11}), ("shortlist_get_run_report", {"id": 101})):
            limited.expect_error(tool, request)
        public = json.dumps([rows, runs, requests])
        for private in (
            "foreign-secret-row",
            "PRIVATE-WATCH-HISTORY",
            "PRIVATE-ERROR",
            "FOREIGN-REQUEST",
            "UNSCOPED-REQUEST",
            "HIDDEN-REQUEST",
        ):
            assert private not in public
        denied = _constrained_wire(
            wire, app, capabilities=ASSISTANT_CAPABILITIES - {Capability.ACTIVITY_READ, Capability.REQUESTS_READ}
        )
        for tool in ("shortlist_list_requests", "shortlist_list_runs", "shortlist_get_activity"):
            denied.expect_error(tool, {"limit": 1}, code="missing_permission")


def test_sdk_activity_pagination_excludes_foreign_grants_and_free_form_details(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch) as (wire, app, _state):
        limited = _constrained_wire(wire, app)
        own = set()
        for value in (20, 25, 30):
            own.add(
                limited.exercise([("shortlist_plan_configuration", {"values": {"plex.timeout_s": value}})])[
                    "shortlist_plan_configuration"
                ]["data"]["change_id"]
            )
        foreign = wire.exercise([("shortlist_plan_configuration", {"values": {"plex.timeout_s": 35}})])[
            "shortlist_plan_configuration"
        ]["data"]["change_id"]
        with app.state.sessions() as session:
            session.add(
                Event(
                    scope="assistant.synthetic",
                    level="info",
                    message={"change_id": foreign, "private": "FOREIGN-AUDIT-SENTINEL"},
                )
            )
            session.add(
                Event(
                    scope="assistant.synthetic",
                    level="info",
                    message={"change_id": next(iter(own)), "private": "OWN-RAW-AUDIT-SENTINEL"},
                )
            )
            session.commit()
        activity = _all_pages(limited, "shortlist_get_activity", key="event_id")
        assert {item["change_id"] for item in activity} == own
        public = json.dumps(activity)
        assert foreign not in public
        assert "FOREIGN-AUDIT-SENTINEL" not in public and "OWN-RAW-AUDIT-SENTINEL" not in public
