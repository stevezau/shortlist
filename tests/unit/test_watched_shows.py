"""Tests for the watched-show filter — the logic that decides when a show is "finished" and should
not be recommended back to a user (issue #12: in-progress shows were being recommended).

The counts are Plex's OWN per-user ``viewedLeafCount`` / ``leafCount`` (marks included), passed to
``_watched_titles`` as ``{tmdb_id: (viewed_episodes, total_episodes)}``."""

from shortlist.engine.models import MediaType
from shortlist.engine.rows import _engaged_floor, _watched_titles


class TestWatchedShowFilter:
    """The ``_watched_titles`` function decides which shows count as "finished" (already watched) based
    on episodes watched vs. total episodes. A show is finished when the user has watched either:

    - >= ``show_pct`` of its episodes (default 0.8 = 80%), OR
    - >= a length-scaled "engaged" floor (``_engaged_floor``): ``max(3, 15% of episodes)``.

    The bar is ``min(total * show_pct, _engaged_floor(total))``, so for a short show the percentage is
    tighter, and for a long show the scaled floor is tighter — but that floor is no longer a flat 3:
    3 episodes of a 200-episode run is still a discovery, so the floor grows with length."""

    def test_a_show_with_3_episodes_watched_of_a_short_series_is_finished(self):
        """3 episodes of a 6-episode limited series = past the pilot, given it a real try → finished.
        For a short show the engaged floor is still 3 (15% of 6 = 0.9, below the minimum)."""
        finished = _watched_titles(set(), {100: (3, 6)}, 0.8)
        assert (100, MediaType.SHOW) in finished, "3 of 6 (>= floor 3) → finished"

    def test_a_few_episodes_of_a_long_series_is_not_finished(self):
        """The fix: 3 episodes of a 60-episode show is 5% — plainly still a discovery, NOT finished.
        The engaged floor scales to 15% of length (9 here), so a light sample no longer suppresses a
        long show the way the old flat-3 floor did."""
        # bar = min(60*0.8=48, floor=max(3, 60*0.15=9)=9) = 9; 3 < 9 → still a fresh pick
        finished = _watched_titles(set(), {100: (3, 60)}, 0.8)
        assert (100, MediaType.SHOW) not in finished, "3 of 60 (< floor 9) → not finished"

    def test_a_long_series_watched_to_its_scaled_floor_is_finished(self):
        """Once past ~15% of a long run, the person is engaged, not discovering → finished."""
        finished = _watched_titles(set(), {100: (9, 60)}, 0.8)  # 9 = exactly the 15%-of-60 floor
        assert (100, MediaType.SHOW) in finished, "9 of 60 (>= floor 9) → finished"

    def test_two_episodes_of_a_short_series_is_not_finished(self):
        """2 episodes = still sampling, below the floor-of-3 → not finished yet."""
        finished = _watched_titles(set(), {100: (2, 6)}, 0.8)
        assert (100, MediaType.SHOW) not in finished, "2 < floor 3 → not finished"

    def test_a_short_show_at_80_percent_is_finished(self):
        """For a 10-episode show, the percentage bar (8) is tighter than the scaled floor (max(3,1.5)=3)."""
        finished = _watched_titles(set(), {200: (8, 10)}, 0.8)  # 8 of 10 = 80%
        assert (200, MediaType.SHOW) in finished, "8/10 = 80% → finished"

    def test_a_short_show_at_70_percent_is_finished_by_the_floor(self):
        """7 of 10 = 70% (< 80%) but well past the floor of 3 → finished by the floor bar."""
        # 7 >= min(10*0.8=8, floor=max(3, 1.5)=3) = 3 → finished
        finished = _watched_titles(set(), {200: (7, 10)}, 0.8)
        assert (200, MediaType.SHOW) in finished, "7 episodes >= floor 3 → finished"

    def test_a_long_returning_series_never_hits_80_percent(self):
        """Gold Rush on SFLIX: 160 episodes of 226 = 71%, never hits 80%. The scaled floor
        (15% of 226 ≈ 34) catches it — and 160 is well past that."""
        # 160 >= min(226*0.8=180.8, floor=max(3, 226*0.15=33.9)=33.9) = 33.9 → finished
        finished = _watched_titles(set(), {300: (160, 226)}, 0.8)
        assert (300, MediaType.SHOW) in finished, "160 watched >> floor 34 → finished by the floor bar"

    def test_a_show_with_unknown_total_is_treated_as_finished(self):
        """If Plex reports episodes watched but no total (leafCount absent/0), treat it as finished to
        be conservative — better to skip a potential recommendation than to re-recommend something the
        user has already worked through."""
        assert (400, MediaType.SHOW) in _watched_titles(set(), {400: (5, None)}, 0.8)
        assert (401, MediaType.SHOW) in _watched_titles(set(), {401: (5, 0)}, 0.8)

    def test_movies_are_always_finished(self):
        """Movies have no episode complexity — any watch = finished."""
        finished = _watched_titles({500, 600}, {}, 0.8)
        assert (500, MediaType.MOVIE) in finished
        assert (600, MediaType.MOVIE) in finished


class TestEngagedFloor:
    """``_engaged_floor`` scales the "watched enough to not be a discovery" bar with series length."""

    def test_short_series_uses_the_minimum(self):
        """Below the crossover (~20 episodes), the flat 3-episode minimum holds."""
        assert _engaged_floor(6) == 3  # 15% of 6 = 0.9, floored to 3
        assert _engaged_floor(10) == 3  # 15% of 10 = 1.5, floored to 3

    def test_long_series_scales_up(self):
        """Past the crossover the floor grows to ~15% of length, so a light sample stays a discovery."""
        assert _engaged_floor(60) == 9.0  # 15% of 60
        assert _engaged_floor(200) == 30.0  # 15% of 200

    def test_crossover_is_around_twenty_episodes(self):
        """15% of 20 = 3, exactly the minimum — the two bars meet here."""
        assert _engaged_floor(20) == 3.0
