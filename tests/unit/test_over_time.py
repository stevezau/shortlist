"""Over-time controls on an AI row (#138): refresh share, no-repeat cooldown, keep-out rows."""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from shortlist.engine.models import Candidate, MediaType, OverTime, TitleKey
from shortlist.engine.over_time import apply_exclusions, excluded_titles

TODAY = date(2026, 10, 4)


class FakeHistory:
    def __init__(self, first_shown: set[TitleKey] | None = None, latest: dict[str, set[TitleKey]] | None = None):
        self.first_shown = first_shown or set()
        self.latest_by_row = latest or {}
        self.first_shown_calls: list[tuple[str, str, date]] = []
        self.latest_calls: list[tuple[str, str]] = []

    def first_shown_since(self, user_slug: str, row_slug: str, since: date) -> set[TitleKey]:
        self.first_shown_calls.append((user_slug, row_slug, since))
        return self.first_shown

    def latest(self, user_slug: str, row_slug: str) -> set[TitleKey]:
        self.latest_calls.append((user_slug, row_slug))
        return self.latest_by_row.get(row_slug, set())


def _candidate(tmdb_id: int, media: MediaType = MediaType.MOVIE) -> Candidate:
    return Candidate(tmdb_id=tmdb_id, title=f"T{tmdb_id}", media_type=media)


def _excluded(over_time: OverTime, **overrides) -> set[TitleKey]:
    args = {
        "user_slug": "sarah",
        "row_slug": "twists",
        "today": TODAY,
        "history": None,
        "built_this_run": {},
        "spare": set(),
    }
    return excluded_titles(over_time, **{**args, **overrides})


class TestOverTime:
    def test_unset_is_inactive_with_the_pre_existing_keep_fraction(self):
        assert OverTime().active is False
        assert OverTime().fingerprint() == ""
        assert OverTime().keep_fraction() == 2 / 3

    def test_refresh_share_sets_the_keep_fraction(self):
        assert OverTime(refresh_share=0.5).keep_fraction() == 0.5
        assert OverTime(refresh_share=1.0).keep_fraction() == 0.0

    def test_fingerprint_is_ordered_and_sorts_avoid(self):
        assert OverTime(0.5, 30, ("b", "a")).fingerprint() == "share=0.5;cooldown=30;avoid=a,b"

    def test_refresh_share_outside_zero_to_one_is_clamped(self):
        assert OverTime(refresh_share=1.5).keep_fraction() == 0.0
        assert OverTime(refresh_share=-0.2).keep_fraction() == 1.0

    def test_each_control_alone_is_active(self):
        assert OverTime(refresh_share=0.0).active
        assert OverTime(repeat_cooldown_days=0).active
        assert OverTime(avoid_rows=("x",)).active


class TestExcludedTitles:
    def test_cooldown_reads_history_since_today_minus_days(self):
        history = FakeHistory(first_shown={(MediaType.MOVIE, 1)})
        result = _excluded(OverTime(repeat_cooldown_days=30), history=history)

        assert result == {(MediaType.MOVIE, 1)}
        assert history.first_shown_calls == [("sarah", "twists", TODAY - timedelta(days=30))]

    def test_avoid_rows_prefers_this_runs_picks_over_history(self):
        history = FakeHistory(latest={"other": {(MediaType.MOVIE, 9)}})
        built = {("sarah", "other"): {(MediaType.MOVIE, 2)}}
        result = _excluded(OverTime(avoid_rows=("other",)), history=history, built_this_run=built)

        assert result == {(MediaType.MOVIE, 2)}
        assert history.latest_calls == []

    def test_avoid_rows_falls_back_to_the_latest_real_run(self):
        history = FakeHistory(latest={"other": {(MediaType.SHOW, 9)}})
        result = _excluded(OverTime(avoid_rows=("other",)), history=history)

        assert result == {(MediaType.SHOW, 9)}
        assert history.latest_calls == [("sarah", "other")]

    def test_avoid_rows_naming_this_row_is_ignored(self):
        history = FakeHistory(latest={"twists": {(MediaType.MOVIE, 1)}})
        built = {("sarah", "twists"): {(MediaType.MOVIE, 2)}}

        assert _excluded(OverTime(avoid_rows=("twists",)), history=history, built_this_run=built) == set()
        assert history.latest_calls == []

    def test_another_persons_picks_are_not_read(self):
        built = {("mike", "other"): {(MediaType.MOVIE, 2)}}

        assert _excluded(OverTime(avoid_rows=("other",)), built_this_run=built) == set()

    def test_no_history_leaves_only_this_runs_titles(self):
        built = {("sarah", "other"): {(MediaType.MOVIE, 2)}}
        result = _excluded(OverTime(repeat_cooldown_days=30, avoid_rows=("other",)), built_this_run=built)

        assert result == {(MediaType.MOVIE, 2)}

    def test_cooldown_spares_kept_picks_in_excluded_titles(self):
        history = FakeHistory(first_shown={(MediaType.MOVIE, 1), (MediaType.MOVIE, 2)})
        result = _excluded(OverTime(repeat_cooldown_days=7), history=history, spare={(MediaType.MOVIE, 1)})

        assert result == {(MediaType.MOVIE, 2)}

    def test_a_kept_title_in_the_cooldown_window_survives_and_an_unkept_one_is_dropped(self):
        history = FakeHistory(first_shown={(MediaType.MOVIE, 1), (MediaType.MOVIE, 2)})
        excluded = _excluded(OverTime(repeat_cooldown_days=7), history=history, spare={(MediaType.MOVIE, 1)})
        result = apply_exclusions([_candidate(1), _candidate(2), _candidate(3)], excluded)

        assert [c.tmdb_id for c in result.kept] == [1, 3]
        assert result.dropped == 1

    def test_no_cooldown_means_the_history_is_not_asked(self):
        history = FakeHistory(first_shown={(MediaType.MOVIE, 1)})

        assert _excluded(OverTime(), history=history) == set()
        assert history.first_shown_calls == []


class TestApplyExclusions:
    def test_drops_matching_keys_and_counts_them(self):
        candidates = [_candidate(1), _candidate(2), _candidate(3)]
        result = apply_exclusions(candidates, {(MediaType.MOVIE, 2)})

        assert [c.tmdb_id for c in result.kept] == [1, 3]
        assert result.dropped == 1
        assert result.skipped is False

    def test_the_same_id_in_another_media_type_is_not_dropped(self):
        candidates = [_candidate(1, MediaType.SHOW)]

        assert apply_exclusions(candidates, {(MediaType.MOVIE, 1)}).kept == candidates

    def test_exclusions_skipped_when_they_would_empty_the_row(self):
        candidates = [_candidate(1), _candidate(2)]
        result = apply_exclusions(candidates, {(MediaType.MOVIE, 1), (MediaType.MOVIE, 2)})

        assert result.skipped is True
        assert result.kept is candidates
        assert result.dropped == 0

    def test_nothing_excluded_returns_the_same_list(self):
        candidates = [_candidate(1)]
        result = apply_exclusions(candidates, set())

        assert result.kept is candidates
        assert result.skipped is False

    def test_an_empty_candidate_list_is_not_reported_as_skipped(self):
        assert apply_exclusions([], {(MediaType.MOVIE, 1)}).skipped is False

    @given(
        ids=st.lists(st.integers(0, 20), unique=True),
        excluded_ids=st.sets(st.integers(0, 20)),
    )
    def test_kept_is_an_ordered_subsequence_that_accounts_for_every_candidate(self, ids, excluded_ids):
        candidates = [_candidate(i) for i in ids]
        result = apply_exclusions(candidates, {(MediaType.MOVIE, i) for i in excluded_ids})

        kept_ids = [c.tmdb_id for c in result.kept]
        assert kept_ids == [i for i in ids if i in set(kept_ids)]
        if not result.skipped:
            assert len(result.kept) + result.dropped == len(candidates)
