"""Persisting a run's request queue: what lands in the approval inbox, and what's left alone."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import MediaType, MissingTitle, RequestOutcome, RequestReport
from shortlist.server.db.models import Base, RequestCandidate
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.run_service import RunService


def _sessions(tmp_path: Path):
    run_migrations(tmp_path)
    return make_session_factory(make_engine(tmp_path))


def _report(
    queued: list[MissingTitle],
    *,
    dry_run: bool = False,
    present: set | None = None,
    arr_present: set | None = None,
    sent: list[MissingTitle] | None = None,
    outcomes: list[RequestOutcome] | None = None,
):
    return SimpleNamespace(
        dry_run=dry_run,
        requests=RequestReport(
            queued=queued, arr_present=arr_present or set(), sent=sent or [], outcomes=outcomes or []
        ),
        library_present=present or set(),
    )


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

    def test_auto_sent_titles_persist_the_arr_slug(self, tmp_path: Path):
        # The nightly auto-send route must file the arr's titleSlug so the inbox deep-links to it —
        # both when the title was already queued (existing row) and brand new (fresh insert).
        sessions = _sessions(tmp_path)

        def _outcome(tmdb_id: int) -> RequestOutcome:
            return RequestOutcome(
                tmdb_id=tmdb_id,
                title=f"t{tmdb_id}",
                media_type=MediaType.MOVIE,
                status="requested",
                detail="queued in Radarr",
                arr_slug=f"movie-{tmdb_id}",
            )

        # Existing pending row -> a later run auto-sends it: status flips to sent and the slug lands.
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1)]))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(
                s, 2, _report([], sent=[_title(1, arr_slug="movie-1")], outcomes=[_outcome(1)])
            )
            s.commit()
        # A never-queued title auto-sent straight in is inserted as sent WITH its slug.
        with sessions() as s:
            RunService._persist_request_queue(
                s, 3, _report([], sent=[_title(2, arr_slug="movie-2")], outcomes=[_outcome(2)])
            )
            s.commit()
        with sessions() as s:
            rows = {r.tmdb_id: r for r in s.query(RequestCandidate).all()}
            assert (rows[1].status, rows[1].arr_slug) == ("sent", "movie-1")
            assert (rows[2].status, rows[2].arr_slug) == ("sent", "movie-2")

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

    def test_poster_path_is_persisted_on_insert(self, tmp_path: Path):
        # The engine side of the poster chain is well covered, but THIS is where the value reaches
        # the database — and nothing exercised it: deleting `poster_path=m.poster_path` from
        # `_candidate_row` left the whole suite green.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, poster_path="/art.jpg")]))
            s.commit()
        with sessions() as s:
            assert s.query(RequestCandidate).one().poster_path == "/art.jpg"

    def test_a_resurfaced_title_keeps_its_artwork_when_the_new_pass_has_none(self, tmp_path: Path):
        """The retain rule, and the ONLY way rows queued before 0044 ever get artwork.

        A later run can surface the same title from a poster-less source (Trakt, the web search). If
        that blanked the stored path, the inbox would lose artwork it already had — and the backfill
        the migration relies on would never happen, because it IS this expression.
        """
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, poster_path="/art.jpg")]))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(s, 2, _report([_title(1, poster_path="")]))
            s.commit()
        with sessions() as s:
            assert s.query(RequestCandidate).one().poster_path == "/art.jpg"

    def test_a_resurfaced_title_gains_artwork_it_did_not_have(self, tmp_path: Path):
        # The backfill direction: a row stored before 0044 (or from a poster-less source) picks the
        # artwork up on the first run that re-surfaces it with one.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, poster_path="")]))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(s, 2, _report([_title(1, poster_path="/later.jpg")]))
            s.commit()
        with sessions() as s:
            assert s.query(RequestCandidate).one().poster_path == "/later.jpg"

    def test_overview_is_persisted_on_insert(self, tmp_path: Path):
        # Same gap the poster test above exists to close: the engine chain can be perfect and the
        # synopsis still never reach the database if `_candidate_row` doesn't carry it.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, overview="A synopsis.")]))
            s.commit()
        with sessions() as s:
            assert s.query(RequestCandidate).one().overview == "A synopsis."

    def test_a_resurfaced_title_keeps_its_synopsis_when_the_new_pass_has_none(self, tmp_path: Path):
        """The retain rule for text, and the ONLY way rows queued before 0071 ever get a synopsis."""
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, overview="A synopsis.")]))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(s, 2, _report([_title(1, overview="")]))
            s.commit()
        with sessions() as s:
            assert s.query(RequestCandidate).one().overview == "A synopsis."

    def test_a_resurfaced_title_gains_a_synopsis_it_did_not_have(self, tmp_path: Path):
        # The backfill direction — the whole migration note for 0071 is this expression.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, overview="")]))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(s, 2, _report([_title(1, overview="Filled in later.")]))
            s.commit()
        with sessions() as s:
            assert s.query(RequestCandidate).one().overview == "Filled in later."

    def test_pending_titles_now_in_the_library_are_dropped(self, tmp_path: Path):
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1), _title(2)]))
            s.commit()
        # Next run: title 1 has since arrived in the library, so it should leave the inbox; a run
        # with nothing newly queued must still prune.
        with sessions() as s:
            RunService._persist_request_queue(s, 2, _report([], present={(1, MediaType.MOVIE)}))
            s.commit()
        with sessions() as s:
            assert {r.tmdb_id for r in s.query(RequestCandidate).all()} == {2}

    def test_pending_titles_an_arr_now_tracks_are_dropped(self, tmp_path: Path):
        # ONE PIECE case: added to Sonarr by hand (or by a send that predates the ledger) but
        # unaired/undownloaded, so never in Plex — only the arr-presence prune can clear the row.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1), _title(2, media_type=MediaType.SHOW)]))
            s.add(RequestCandidate(tmdb_id=1, media_type="show", title="x", rating=1.0, vote_count=1, status="sent"))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(s, 2, _report([], arr_present={(1, "movie"), (1, "show"), (2, "show")}))
            s.commit()
        with sessions() as s:
            rows = [(r.tmdb_id, r.media_type, r.status) for r in s.query(RequestCandidate).all()]
            assert rows == [(1, "show", "sent")]  # both pending pruned; the sent ledger row survives

    def test_a_title_pruned_as_present_is_not_re_queued_the_same_run(self, tmp_path: Path):
        # Issue #104. The two answers to "does Sonarr have this show?" can disagree — the pool drop
        # matches on TVDB, `arr_present` on Sonarr's own tmdbId — so one run can both prune a pending
        # row as present AND re-queue the same title. Deleting and re-inserting one unique key in a
        # single flush dies with an IntegrityError, because SQLAlchemy emits the INSERT before the
        # DELETE — and the whole run's report was lost with it, not just the inbox row.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, media_type=MediaType.SHOW)]))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(
                s, 2, _report([_title(1, media_type=MediaType.SHOW)], arr_present={(1, "show")})
            )
            s.commit()
        with sessions() as s:
            # The arr has it: the prune wins, and the run does not put it straight back.
            assert s.query(RequestCandidate).all() == []

    def test_a_title_present_but_never_queued_before_is_not_inserted(self, tmp_path: Path):
        # Same disagreement, first sighting: nothing to prune, so the queued copy would land a row
        # for a title the arr already tracks — an inbox entry the NEXT run would only delete again.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(
                s, 1, _report([_title(1, media_type=MediaType.SHOW)], arr_present={(1, "show")})
            )
            s.commit()
        with sessions() as s:
            assert s.query(RequestCandidate).all() == []

    def test_a_key_reaching_the_persist_twice_lands_one_row(self, tmp_path: Path):
        # Belt and braces for #104. The engine dedupes `queued` before it gets here, but the crash
        # this guards is worth a barrier in BOTH modules: two copies of one key cost the entire run's
        # report, not the duplicate row. Registering each insert makes the second copy a refresh.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, demand=2), _title(1, demand=5), _title(2)]))
            s.commit()
        with sessions() as s:
            rows = {(r.tmdb_id, r.demand) for r in s.query(RequestCandidate).all()}
            assert rows == {(1, 5), (2, 2)}  # the later copy refreshed the first

    def test_a_sent_key_reaching_the_persist_twice_lands_one_row(self, tmp_path: Path):
        # Same barrier on the sent pass, where a fresh insert is followed by a second copy of the key.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([], sent=[_title(1), _title(1)]))
            s.commit()
        with sessions() as s:
            row = s.query(RequestCandidate).one()
            assert (row.tmdb_id, row.status) == (1, "sent")

    def test_a_title_the_run_sent_survives_the_present_prune(self, tmp_path: Path):
        # The send is the newer fact and the one worth keeping: filing it as `sent` is what stops the
        # title being re-requested every night. So the prune must leave its row for the sent pass,
        # rather than deleting it into the same delete-then-insert collision.
        sessions = _sessions(tmp_path)
        with sessions() as s:
            RunService._persist_request_queue(s, 1, _report([_title(1, media_type=MediaType.SHOW)]))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(
                s, 2, _report([], arr_present={(1, "show")}, sent=[_title(1, media_type=MediaType.SHOW)])
            )
            s.commit()
        with sessions() as s:
            row = s.query(RequestCandidate).one()
            assert (row.tmdb_id, row.media_type, row.status) == (1, "show", "sent")
            assert row.sent_at is not None

    def test_present_does_not_drop_sent_or_rejected(self, tmp_path: Path):
        sessions = _sessions(tmp_path)
        with sessions() as s:
            s.add(RequestCandidate(tmdb_id=1, media_type="movie", title="x", rating=1.0, vote_count=1, status="sent"))
            s.commit()
        with sessions() as s:
            RunService._persist_request_queue(s, 2, _report([], present={(1, MediaType.MOVIE)}))
            s.commit()
        with sessions() as s:
            # A sent title being in the library is expected — it must stay in "Already handled".
            assert s.query(RequestCandidate).one().status == "sent"

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


class TestTheOtherLanguageReasonIsAThresholdNotAFailure:
    """`_is_failure_detail` classifies a stored `detail` by its leading words, and the other-language
    reason is covered only because it happens to start with "rating below" — the same prefix the
    auto_min_rating reason uses.

    That coupling is a coincidence of wording, and `QUEUE_REASON_PREFIXES` says so in a comment. This
    pins it: reword either string on its own and a title merely held back starts being recorded as a
    request that FAILED, which then outlives the threshold note it should have been replaced by.
    """

    def test_the_language_bar_reason_is_not_read_as_a_send_failure(self):
        from shortlist.server.services.run_persistence import _is_failure_detail

        assert not _is_failure_detail("rating below the bar for other languages (8.5)")
        assert not _is_failure_detail("rating below auto_min_rating (8.0)")
        # ...while a real Arr failure still is, or the two would be indistinguishable.
        assert _is_failure_detail("Sonarr returned HTTP 503")

    def test_every_queue_reason_the_gate_can_emit_classifies_as_a_threshold(self):
        """Every reason `_auto_eligible` can assign, read out of its own AST — not restated here.

        `requests.py` warns that restating these strings is what lets them drift: an unprefixed reason
        makes `_is_failure_detail` record a merely-held title as a request that FAILED, which then
        outlives the threshold note that should have replaced it.

        Walked with `ast`, not a regex, and that difference is the point. A regex only sees reasons
        written as `reason = "..."`, so one built by a lookup table, a helper call or a concatenation
        is invisible to it — the test passes while covering nothing, which is the failure mode it
        exists to prevent. This sees every FORM of assignment to `reason` (plain, annotated,
        augmented, walrus, tuple-unpacked) and asserts each value is a plain literal, so anything it
        cannot verify fails loudly instead of silently.
        """
        import ast
        import inspect
        import textwrap

        from shortlist.engine import requests as requests_mod
        from shortlist.engine.requests import QUEUE_REASON_PREFIXES
        from shortlist.server.services.run_persistence import _is_failure_detail

        tree = ast.parse(textwrap.dedent(inspect.getsource(requests_mod._auto_eligible)))

        def targets_reason(node) -> bool:
            """Every way `reason` can be written to, not just `reason = ...`.

            `AnnAssign` (`reason: str = ...`), `AugAssign` (`reason += ...`), a walrus and a tuple
            target are all real Python and all invisible to a bare `ast.Assign` + `ast.Name` filter —
            so a new reason written any of those ways would pass SILENTLY, which is the failure this
            test exists to end. Catching them here makes an unverifiable form fail loudly instead.
            """
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets = [node.target]
            else:
                return False
            flat = [e for t in targets for e in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t])]
            return any(isinstance(t, ast.Name) and t.id == "reason" for t in flat)

        assigns = [node for node in ast.walk(tree) if targets_reason(node)]
        assert assigns, "found no assignments to `reason` — has the gate been restructured?"

        leading: list[str] = []
        for node in assigns:
            value = node.value
            assert value is not None, "a bare `reason: str` annotation assigns nothing to check"
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                leading.append(value.value)
            elif isinstance(value, ast.JoinedStr):
                # An f-string: only the leading literal matters, because the prefixes are leading
                # words and any interpolation comes after them. Both degenerate forms are checked
                # rather than indexed blindly — `f""` parses to an EMPTY `values`, so `values[0]`
                # would raise IndexError and report a crash where the point is to report a reason
                # that cannot be prefix-matched.
                first = value.values[0] if value.values else None
                assert isinstance(first, ast.Constant), (
                    "a reason that is empty or starts with an interpolation cannot be prefix-matched; "
                    "give it a literal opening or add its form to QUEUE_REASON_PREFIXES"
                )
                leading.append(first.value)
            else:
                raise AssertionError(
                    f"`reason` assigned a {type(value).__name__} this test cannot verify — a lookup "
                    "table or helper would slip past prefix checking entirely. Keep reasons literal."
                )

        for text in leading:
            assert text.startswith(QUEUE_REASON_PREFIXES), (
                f"{text!r} matches no prefix in QUEUE_REASON_PREFIXES — a held title would be recorded as a failed send"
            )
            assert not _is_failure_detail(text), f"{text!r} reads as a send failure"


_KEYS = st.sampled_from([(1, MediaType.MOVIE), (1, MediaType.SHOW), (2, MediaType.MOVIE), (3, MediaType.SHOW)])


class TestOneRowPerTitleHolds:
    """Issue #104's real invariant: NO report shape may make the persist raise.

    The bug was never about one title — it was that a single duplicate key costs the ENTIRE run's
    report, the whole `persist_report` transaction rolling back with it. Hand-picked cases proved the
    two routes found by reading the code; this proves there is no third, across every combination of
    what a run can claim: rows already in the inbox in any status, titles queued, titles sent, and
    either presence check reporting the title as already there — including the contradictory shapes
    the engine should never emit, since the engine is the module the barrier exists to be independent
    of.
    """

    @given(
        pre=st.lists(st.tuples(_KEYS, st.sampled_from(["pending", "sent", "rejected"])), max_size=4),
        queued=st.lists(_KEYS, max_size=4),
        sent=st.lists(_KEYS, max_size=4),
        present=st.lists(_KEYS, max_size=4),
    )
    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_no_report_shape_can_break_the_persist(self, pre, queued, sent, present):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine)
        with sessions() as s:
            for (tmdb_id, media_type), status in {k: v for k, v in pre}.items():
                s.add(
                    RequestCandidate(
                        tmdb_id=tmdb_id, media_type=media_type.value, title="x", rating=1.0, vote_count=1, status=status
                    )
                )
            s.commit()

        report = _report(
            [_title(tmdb_id, media_type=media_type) for tmdb_id, media_type in queued],
            sent=[_title(tmdb_id, media_type=media_type) for tmdb_id, media_type in sent],
            arr_present={(tmdb_id, media_type.value) for tmdb_id, media_type in present},
        )
        with sessions() as s:
            RunService._persist_request_queue(s, 1, report)
            s.commit()  # the assertion IS this line: a duplicate key here loses the whole report

        with sessions() as s:
            keys = [(r.tmdb_id, r.media_type) for r in s.query(RequestCandidate).all()]
            assert len(keys) == len(set(keys)), "the inbox must hold one row per title"
