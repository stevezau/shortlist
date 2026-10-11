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
from tests.unit.assistant_fixtures import request_env  # noqa: F401


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


class TestAFailureBeforeSendingNeverBlocksTheTitle:
    """A claim records any outcome but `requested`/`skipped_*` as an uncertain send, and an uncertain
    send is excluded from every later run until the owner releases it by hand. So a failure that
    provably sent nothing must not report "error" — or one TMDB or Sonarr blip blocks the show for good,
    while its detail promises it "may work next run"."""

    SHOW = MissingTitle(tmdb_id=202, media_type=MediaType.SHOW, title="Show", year=2020, rating=8, vote_count=900)

    def _send(self, env, tmdb, sonarr=None, monkeypatch=None):
        from shortlist.engine import requests as requests_mod

        if sonarr is not None:
            monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr)
        cfg = RequestConfig(enabled=True, target="arr", sonarr=ArrTarget("http://sonarr.test", "key", 1, "/tv"))
        [outcome] = requests_mod._send_claims(
            [("movies", self.SHOW)],
            {"movies": cfg},
            tmdb,
            dry_run=False,
            min_write_interval=0,
            acquisition_guard=AutomaticRequestGuard(env.state.sessions),
        )
        with env.state.sessions() as session:
            claim = session.scalars(select(AssistantRequestDispatch)).one()
            return outcome, claim.status, durable_handled_requests(session)

    def test_an_unreachable_tmdb_during_the_tvdb_lookup(self, request_env):
        from tests.unit.test_requests import FakeTmdb

        outcome, claim_status, handled = self._send(request_env, FakeTmdb(raise_on=202))

        assert outcome.status.startswith("skipped_"), outcome.status
        assert "next run" in outcome.detail
        assert claim_status == "refused"
        assert handled == set()

    def test_sonarr_failing_its_lookup_before_the_add(self, request_env, monkeypatch):
        from shortlist.engine.clients.arr import ArrError, SonarrClient
        from tests.unit.test_requests import FakeTmdb

        class LookupDown(SonarrClient):
            def __init__(self):
                super().__init__(ArrTarget("http://sonarr.test", "key", 1, "/tv"))

            def _get(self, path, **params):
                raise ArrError("Sonarr unreachable (ConnectError)")

            def _post(self, path, body):
                pytest.fail("nothing may be posted after a failed lookup")

        outcome, claim_status, handled = self._send(request_env, FakeTmdb({202: 81189}), LookupDown(), monkeypatch)

        assert outcome.status.startswith("skipped_"), outcome.status
        assert "unreachable" in outcome.detail
        assert claim_status == "refused"
        assert handled == set()

    def test_a_failed_add_post_is_still_uncertain(self, request_env, monkeypatch):
        """The other side of the line: once the add itself was attempted, Sonarr may have taken it, so
        the claim stays uncertain and the title is not sent twice."""
        from shortlist.engine.clients.arr import ArrError, SonarrClient
        from tests.unit.test_requests import FakeTmdb

        class AddTimesOut(SonarrClient):
            def __init__(self):
                super().__init__(ArrTarget("http://sonarr.test", "key", 1, "/tv"))

            def _get(self, path, **params):
                if path.startswith("/api/v3/series/lookup"):
                    return [{"tvdbId": 81189, "title": "Show", "titleSlug": "show", "seasons": []}]
                return []

            def _post(self, path, body):
                raise ArrError("Sonarr unreachable (ReadTimeout)")

        outcome, claim_status, handled = self._send(request_env, FakeTmdb({202: 81189}), AddTimesOut(), monkeypatch)

        assert outcome.status == "error"
        assert claim_status == "outcome_unknown"
        assert handled == {(202, "show")}
