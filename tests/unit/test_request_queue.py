"""Persisting a run's request queue: what lands in the approval inbox, and what's left alone."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from shortlist.engine.models import MediaType, MissingTitle, RequestReport
from shortlist.server.db.models import RequestCandidate
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.run_service import RunService


def _sessions(tmp_path: Path):
    run_migrations(tmp_path)
    return make_session_factory(make_engine(tmp_path))


def _report(queued: list[MissingTitle], *, dry_run: bool = False):
    return SimpleNamespace(dry_run=dry_run, requests=RequestReport(queued=queued))


def _title(tmdb_id: int, **kw) -> MissingTitle:
    base = dict(title=f"t{tmdb_id}", media_type=MediaType.MOVIE, year=2020, rating=8.0, vote_count=500, demand=2)
    base.update(kw)
    return MissingTitle(tmdb_id=tmdb_id, **base)


class TestPersistRequestQueue:
    def test_new_queued_titles_are_inserted_pending(self, tmp_path: Path):
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 7, _report([_title(1), _title(2)]))
            s.commit()
        with sessions() as s:
            rows = s.query(RequestCandidate).all()
            assert {r.tmdb_id for r in rows} == {1, 2}
            assert all(r.status == "pending" and r.first_seen_run_id == 7 for r in rows)

    def test_dry_run_persists_nothing(self, tmp_path: Path):
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1)], dry_run=True))
            s.commit()
        with sessions() as s:
            assert s.query(RequestCandidate).count() == 0  # a preview must not fill the inbox

    def test_resurfaced_pending_refreshes_facts_without_duplicating(self, tmp_path: Path):
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, demand=2, rating=7.5)]))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(s, 2, _report([_title(1, demand=6, rating=8.9)]))
            s.commit()
        with sessions() as s:
            rows = s.query(RequestCandidate).all()
            assert len(rows) == 1  # the unique (tmdb_id, media_type) key prevents a duplicate
            assert rows[0].demand == 6 and rows[0].rating == 8.9  # latest run's facts win

    def test_sent_or_rejected_rows_are_left_untouched(self, tmp_path: Path):
        sessions = _sessions(tmp_path)
        with sessions() as s:
            s.add(
                RequestCandidate(
                    tmdb_id=1, media_type="movie", title="old", rating=1.0, vote_count=1, status="rejected"
                )
            )
            s.add(RequestCandidate(tmdb_id=2, media_type="movie", title="old", rating=1.0, vote_count=1, status="sent"))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(s, 5, _report([_title(1, demand=9, rating=9.9), _title(2, demand=9)]))
            s.commit()
        with sessions() as s:
            by_id = {r.tmdb_id: r for r in s.query(RequestCandidate).all()}
            # A dismissed title must not reappear, and a sent one must not be re-queued as pending.
            assert by_id[1].status == "rejected" and by_id[1].demand == 1
            assert by_id[2].status == "sent" and by_id[2].demand == 1
