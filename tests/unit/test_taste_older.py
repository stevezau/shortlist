"""The "older watches" group of the history mix (#152): a spread sample of what is left of a history."""

from datetime import UTC, datetime, timedelta

from shortlist.engine.history import RatingsPolicy
from shortlist.engine.models import MediaType
from shortlist.engine.taste import HistoryMix, build_taste, history_mix
from tests.unit.test_taste import NOW, movie, show, titles

TRUSTED = RatingsPolicy(threshold=2, trusted=True)


def _build(history, *, older=5, **kw):
    return build_taste(
        history, blocked=kw.pop("blocked", set()), ratings=kw.pop("ratings", None), older_limit=older, now=NOW, **kw
    )


def test_no_older_limit_means_no_older_section():
    history = [movie(f"M{n}", n) for n in range(40)]
    taste = build_taste(history, blocked=set(), ratings=None, now=NOW)
    assert taste.older == [] and "Also watched" not in taste.render()


def test_older_leaves_out_recent_favourites_rated_and_blocked_titles():
    history = [movie(f"M{n}", n + 1, tmdb_id=n) for n in range(30)]
    history += [movie("Fav", 200, plays=4), movie("Hated", 201, rating=2.0), movie("Loved", 202, rating=10.0)]
    history += [movie("Blocked", 203, tmdb_id=999)]
    taste = _build(history, older=50, blocked={999}, ratings=TRUSTED, recent_limit=5)
    older = titles(taste.older)
    assert not set(older) & {"Fav", "Hated", "Loved", "Blocked", "M0", "M1", "M2", "M3", "M4"}
    assert len(older) == 25


def test_a_favourite_beyond_the_rendered_twelve_is_still_not_older():
    history = [movie(f"Fav{n}", 100 + n, plays=2) for n in range(20)] + [movie("Once", 300)]
    taste = _build(history, older=50, recent_limit=0)
    assert titles(taste.older) == ["Once"]
    assert len(taste.favourites) == 12


def test_a_dropped_show_is_left_out_and_a_finished_one_is_a_favourite_not_older():
    history = [
        show("Dropped", 100, seen=2, total=10),
        show("Finished", 101, seen=2, total=2),
        show("Sampled", 102, seen=3, total=10),
    ]
    assert set(titles(_build(history, older=10, recent_limit=0).older)) == {"Sampled"}


def test_lookback_drops_titles_watched_before_the_window():
    history = [movie("Old", 5 * 365), movie("Mid", 2 * 365), movie("New", 100)]
    assert set(titles(_build(history, older=10, recent_limit=0, lookback_years=3).older)) == {"Mid", "New"}
    assert set(titles(_build(history, older=10, recent_limit=0, lookback_years=1).older)) == {"New"}
    assert len(_build(history, older=10, recent_limit=0).older) == 3


def test_the_sample_covers_the_span_newest_first():
    history = [movie(f"M{n}", n) for n in range(100)]
    older = _build(history, older=5, recent_limit=0).older
    ages = [(NOW - i.watched_at).days for i in older]
    assert ages == sorted(ages)
    assert len(older) == 5
    for index, age in enumerate(sorted(ages)):
        assert index * 20 <= age < (index + 1) * 20


def test_the_sample_rotates_weekly_and_holds_within_a_week():
    history = [movie(f"M{n}", n) for n in range(100)]

    def sample(now):
        return titles(build_taste(history, blocked=set(), ratings=None, recent_limit=0, older_limit=5, now=now).older)

    week = datetime.fromtimestamp(7 * 86400 * 2900, UTC)
    assert sample(week) == sample(week + timedelta(days=3))
    assert sample(week) != sample(week + timedelta(days=7))


def test_a_short_pool_is_taken_whole():
    history = [movie(f"M{n}", n) for n in range(4)]
    assert titles(_build(history, older=10, recent_limit=0).older) == ["M0", "M1", "M2", "M3"]


def test_render_lists_the_older_sample_after_the_favourites():
    history = [movie("Fav", 50, plays=3), movie("Old", 400)]
    text = _build(history, older=3, recent_limit=0).render()
    assert text.index("Long-time favourites") < text.index("Also watched over the years (a sample):")
    assert "- Old (2000) - film" in text


def test_history_mix_resolves_both_groups_to_seeds():
    history = [movie(f"Recent{n}", n) for n in range(12)]
    history += [movie("Fav", 50, plays=3, tmdb_id=1), movie("Old", 400)]
    mix = history_mix(history, blocked=set(), ratings=None, resolve=lambda item: 77, favourites=3, older=3, now=NOW)
    assert isinstance(mix, HistoryMix) and mix.taste.wide
    assert [s.tmdb_id for s in mix.favourite_seeds] == [1]
    assert [s.tmdb_id for s in mix.older_seeds] == [77]


def test_titles_the_recent_searches_already_cover_are_never_favourites_or_older():
    """A row searches its SEEDS as the recent group, and they are not always the profile's newest twelve
    (seeds balance media and can cycle), so the mix must step over them or an older slot is wasted."""
    history = [movie(f"Recent{n}", n, tmdb_id=n) for n in range(12)]
    history += [movie("SeedFav", 40, plays=3, tmdb_id=500), movie("Fav", 50, plays=3, tmdb_id=501)]
    history += [movie("SeedOld", 300, tmdb_id=600), movie("Old", 400, tmdb_id=601)]
    mix = history_mix(
        history,
        blocked=set(),
        ratings=None,
        resolve=lambda item: None,
        favourites=3,
        older=3,
        now=NOW,
        searched={(500, MediaType.MOVIE), (600, MediaType.MOVIE)},
    )
    assert [s.tmdb_id for s in mix.favourite_seeds] == [501]
    assert [s.tmdb_id for s in mix.older_seeds] == [601]


def test_a_blocked_title_with_no_tmdb_guid_is_neither_searched_nor_named():
    """ "Don't seed" holds TMDB ids; a watch on a legacy agent carries none until it is resolved, so the block
    must be checked after resolving, or the title is searched and named in the prompt."""
    history = [movie(f"Recent{n}", n, tmdb_id=n) for n in range(12)]
    history += [movie("Blocked Fav", 50, plays=3), movie("Fav", 60, plays=3, tmdb_id=501)]
    history += [movie("Blocked Old", 300), movie("Old", 400, tmdb_id=601)]
    ids = {"Blocked Fav": 42, "Blocked Old": 43}
    mix = history_mix(
        history,
        blocked={42, 43},
        ratings=None,
        resolve=lambda item: ids.get(item.title),
        favourites=3,
        older=3,
        now=NOW,
    )
    assert [s.tmdb_id for s in mix.favourite_seeds] == [501]
    assert [s.tmdb_id for s in mix.older_seeds] == [601]
    assert "Blocked" not in mix.taste.text
