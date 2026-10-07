# ruff: noqa: F811
"""All server acquisition paths share durable title claims without holding SQLite over I/O."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from shortlist.engine.models import ArrTarget, MediaType, MissingTitle, RequestConfig
from shortlist.server.assistant.operation_models import AssistantRequestDispatch
from shortlist.server.db.models import RequestCandidate
from shortlist.server.services.request_actions import (
    AutomaticRequestGuard,
    durable_handled_requests,
    request_send_entry,
    reserve_request_dispatches,
)
from tests.unit.test_assistant_request_dispatch import request_env  # noqa: F401


def automatic_title():
    return MissingTitle(
        tmdb_id=101, media_type=MediaType.MOVIE, title="Wanted", year=2024, rating=8, vote_count=1000, demand=2
    )


def config():
    return RequestConfig(enabled=True, target="arr", radarr=ArrTarget("http://radarr.test", "key", 1, "/movies"))


def test_manual_reservation_blocks_automatic_without_network(request_env):
    env = request_env
    with env.state.sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(RequestCandidate, 1)
        reserve_request_dispatches(session, [row], [request_send_entry(session, row, config())], origin="manual")
        session.commit()
    with AutomaticRequestGuard(env.state.sessions)("movies", automatic_title(), config()) as record:
        assert record is None
    with env.state.sessions() as session:
        assert durable_handled_requests(session) == {(101, "movie")}


def test_automatic_checkpoint_blocks_manual_and_releases_sqlite_before_vendor(request_env):
    env = request_env
    with AutomaticRequestGuard(env.state.sessions)("movies", automatic_title(), config()) as record:
        assert callable(record)
        with env.state.sessions() as session:
            # A second connection can acquire the write lock while the vendor call would be active.
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(RequestCandidate, 1)
            with pytest.raises(ValueError, match="acquisition"):
                reserve_request_dispatches(
                    session, [row], [request_send_entry(session, row, config())], origin="manual"
                )
            session.rollback()
        record(SimpleNamespace(status="requested", detail="accepted", arr_slug=None))
    with env.state.sessions() as session:
        claim = session.scalars(select(AssistantRequestDispatch)).one()
        assert claim.origin == "automatic" and claim.status == "succeeded"
        assert session.get(RequestCandidate, 1).status == "sent"


def test_automatic_uncertainty_survives_missing_candidate_and_blocks_every_fresh_claim(request_env):
    env = request_env
    with env.state.sessions() as session:
        session.delete(session.get(RequestCandidate, 1))
        session.commit()
    with (
        pytest.raises(TimeoutError),
        AutomaticRequestGuard(env.state.sessions)("movies", automatic_title(), config()) as record,
    ):
        assert callable(record)
        raise TimeoutError("vendor may have accepted")
    with env.state.sessions() as session:
        claim = session.scalars(select(AssistantRequestDispatch)).one()
        assert claim.candidate_id is None and claim.status == "outcome_unknown"
        assert durable_handled_requests(session) == {(101, "movie")}
    with AutomaticRequestGuard(env.state.sessions)("another-row", automatic_title(), config()) as record:
        assert record is None


def test_known_refusal_releases_automatic_claim(request_env):
    env = request_env
    with AutomaticRequestGuard(env.state.sessions)("movies", automatic_title(), config()) as record:
        record(SimpleNamespace(status="skipped_no_tvdb", detail="no identity", arr_slug=None))
    with AutomaticRequestGuard(env.state.sessions)("movies", automatic_title(), config()) as record:
        assert callable(record)
        record(SimpleNamespace(status="requested", detail="accepted", arr_slug=None))


def test_music_content_hold_releases_manual_claim_and_keeps_candidate_pending(request_env, monkeypatch):
    from unittest.mock import Mock

    from shortlist.engine import requests as requests_mod
    from shortlist.server.services.request_actions import finish_request_dispatch, start_manual_request_dispatch

    env = request_env
    cfg = config()
    cfg.exclude_music_nonfiction = True
    tmdb = Mock()
    tmdb.details.return_value = {"genres": [{"id": 10402}, {"id": 99}]}
    factory = Mock()
    monkeypatch.setattr(requests_mod, "RadarrClient", factory)
    with env.state.sessions() as session:
        row = session.get(RequestCandidate, 1)
        claims = reserve_request_dispatches(session, [row], [request_send_entry(session, row, cfg)], origin="manual")
        dispatch_id = claims[0].id
        assert start_manual_request_dispatch(session, dispatch_id) is not None
        session.commit()
    report = requests_mod.request_titles_by_row({"movies": cfg}, tmdb, [("movies", automatic_title())], dry_run=False)
    with env.state.sessions() as session:
        finish_request_dispatch(session, dispatch_id, report.outcomes[0])
        session.commit()
        row = session.get(RequestCandidate, 1)
        assert session.get(AssistantRequestDispatch, dispatch_id).status == "refused"
        assert row.status == "pending"
        assert row.detail.startswith("music content filter:")
        # A later metadata retry or deliberate policy exception can acquire a fresh claim.
        cfg.exclude_music_nonfiction = False
        assert reserve_request_dispatches(session, [row], [request_send_entry(session, row, cfg)], origin="manual")
    factory.assert_not_called()


def test_startup_settles_only_orphaned_synchronous_work_and_is_idempotent(request_env):
    from shortlist.server.services.request_actions import recover_abandoned_request_dispatches

    env = request_env
    with env.state.sessions() as session:
        row = session.get(RequestCandidate, 1)
        entry = request_send_entry(session, row, config())
        session.add_all(
            [
                AssistantRequestDispatch(
                    origin="manual",
                    candidate_id=1,
                    destination="http://radarr.test",
                    request_body=entry,
                    status="reserved",
                ),
                AssistantRequestDispatch(
                    origin="automatic",
                    candidate_id=1,
                    destination="http://radarr.test",
                    request_body=entry,
                    status="external_started",
                ),
                AssistantRequestDispatch(
                    origin="assistant",
                    candidate_id=1,
                    destination="http://radarr.test",
                    request_body=entry,
                    status="reserved",
                ),
            ]
        )
        session.commit()
    assert recover_abandoned_request_dispatches(env.state.sessions) == 2
    assert recover_abandoned_request_dispatches(env.state.sessions) == 0
    with env.state.sessions() as session:
        assert {claim.origin: claim.status for claim in session.scalars(select(AssistantRequestDispatch))} == {
            "manual": "refused",
            "automatic": "outcome_unknown",
            "assistant": "reserved",
        }
