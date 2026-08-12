"""The agregarr mirror leaves an audit record on BOTH paths that can run it.

There are two, and they audit differently: `sync.check`/`privacy.sync` persist no run and audit
themselves through `jobs._audit_agregarr_mirrors`, while the nightly run — the path that actually
does this every night — persists a run and emits its shelf events only through
`run_persistence._emit_hub_ordering_events`. Auditing in one place leaves the other silent, which is
the failure this file exists to prevent (plex-safety rule 10).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from shortlist.engine.models import RunReport
from shortlist.server.db.models import Event
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services import jobs
from shortlist.server.services.run_persistence import _emit_hub_ordering_events


@pytest.fixture
def session(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    with make_session_factory(engine)() as db:
        yield db
    engine.dispose()


def report_with(mirrors: list[dict]) -> RunReport:
    report = RunReport(started_at=datetime.now(UTC))
    report.agregarr_mirrors = mirrors
    return report


def scopes(session) -> list[str]:
    return [e.scope for e in session.query(Event).all()]


class TestPersistedRunPath:
    def test_a_write_is_audited(self, session):
        report = report_with([{"library": "Movies", "ok": True, "changed": True, "items": 84, "moved": 58}])

        _emit_hub_ordering_events(session, 1, report)
        session.commit()

        event = session.query(Event).one()
        assert event.scope == "run.agregarr_order"
        assert event.level == "info"
        assert event.message["library"] == "Movies"
        assert event.message["moved"] == 58

    def test_a_failure_is_audited_as_a_warning(self, session):
        report = report_with([{"library": "Movies", "ok": False, "error": "AgregarrError: unreachable"}])

        _emit_hub_ordering_events(session, 1, report)
        session.commit()

        event = session.query(Event).one()
        assert event.level == "warning"
        assert "unreachable" in event.message["error"]

    def test_a_no_op_check_is_still_audited(self, session):
        """ "agregarr already agreed" is the fact that distinguishes a shelf that moved because
        agregarr fought us from one that moved for some other reason."""
        report = report_with([{"library": "Movies", "ok": True, "changed": False, "items": 84, "moved": 0}])

        _emit_hub_ordering_events(session, 1, report)
        session.commit()

        assert session.query(Event).one().message["changed"] is False

    def test_nothing_is_emitted_when_no_agregarr_is_connected(self, session):
        _emit_hub_ordering_events(session, 1, report_with([]))
        session.commit()

        assert scopes(session) == []

    def test_a_changed_run_records_the_order_before_and_after(self, session):
        """A tally is not a diff. This renumbers rows Shortlist does not own, there is no snapshot,
        and uninstall does not restore agregarr — so the event is the only record of the prior
        order (plex-safety rule 10)."""
        report = report_with(
            [
                {
                    "library": "Movies",
                    "ok": True,
                    "changed": True,
                    "order_before": ["foreign-900", "row-100"],
                    "order_after": ["row-100", "foreign-900"],
                }
            ]
        )

        _emit_hub_ordering_events(session, 1, report)
        session.commit()

        message = session.query(Event).one().message
        assert message["order_before"] == ["foreign-900", "row-100"]
        assert message["order_after"] == ["row-100", "foreign-900"]


class TestJobHandlerPath:
    """`sync.check` and `privacy.sync` persist no run, so they audit through `jobs`, not
    `run_persistence`. Covering only the other path would let this half regress to silence."""

    @staticmethod
    def _capture(monkeypatch) -> list[tuple]:
        audits: list[tuple] = []
        monkeypatch.setattr(jobs, "write_audit", lambda st, scope, level, **f: audits.append((scope, level, f)))
        return audits

    def test_a_write_is_audited_under_the_shelf_scope(self, monkeypatch):
        audits = self._capture(monkeypatch)
        report = report_with([{"library": "Movies", "ok": True, "changed": True, "moved": 58, "items": 84}])

        jobs._audit_agregarr_mirrors(object(), report, dry_run=False)

        assert len(audits) == 1
        scope, level, fields = audits[0]
        assert (scope, level) == ("shelf.agregarr", "info")
        assert fields["library"] == "Movies" and fields["moved"] == 58
        assert fields["dry_run"] is False

    def test_a_failure_is_audited_as_a_warning(self, monkeypatch):
        audits = self._capture(monkeypatch)
        report = report_with([{"library": "Movies", "ok": False, "error": "AgregarrError: unreachable"}])

        jobs._audit_agregarr_mirrors(object(), report, dry_run=False)

        assert audits[0][1] == "warning"
        assert "unreachable" in audits[0][2]["error"]

    def test_a_dry_run_entry_is_flagged_even_when_the_job_is_not(self, monkeypatch):
        audits = self._capture(monkeypatch)
        report = report_with([{"library": "Movies", "ok": True, "changed": True, "dry_run": True}])

        jobs._audit_agregarr_mirrors(object(), report, dry_run=False)

        assert audits[0][2]["dry_run"] is True
