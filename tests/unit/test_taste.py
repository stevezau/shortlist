"""The wide taste profile (#152): what a person has watched, shaped for the AI's pick prompt."""

from datetime import UTC, datetime, timedelta

from shortlist.engine.history import RatingsPolicy
from shortlist.engine.models import MediaType, WatchedItem
from shortlist.engine.taste import build_taste

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def movie(title, days_ago=0, *, plays=1, year=2000, tmdb_id=None, rating=None) -> WatchedItem:
    return WatchedItem(
        title=title,
        media_type=MediaType.MOVIE,
        watched_at=NOW - timedelta(days=days_ago),
        year=year,
        tmdb_id=tmdb_id,
        watch_count=plays,
        user_rating=rating,
    )


def show(title, days_ago=0, *, seen=3, total=10, year=2010, tmdb_id=None, rating=None) -> WatchedItem:
    return WatchedItem(
        title=title,
        media_type=MediaType.SHOW,
        watched_at=NOW - timedelta(days=days_ago),
        year=year,
        tmdb_id=tmdb_id,
        watch_count=seen,
        viewed_leaf_count=seen,
        leaf_count=total,
        user_rating=rating,
    )


TRUSTED = RatingsPolicy(threshold=2, trusted=True)


def titles(items) -> list[str]:
    return [i.title for i in items]


def test_recent_is_newest_first_and_capped():
    history = [movie(f"M{n}", n) for n in range(30)]
    taste = build_taste(history, blocked=set(), ratings=None, recent_limit=5)
    assert titles(taste.recent) == ["M0", "M1", "M2", "M3", "M4"]


def test_a_show_collapses_to_one_line_with_its_progress():
    history = [show("Slow Horses", 1, seen=4, total=30), show("Slow Horses", 2, seen=6, total=30)]
    taste = build_taste(history, blocked=set(), ratings=None)
    assert titles(taste.recent) == ["Slow Horses"]
    assert "Slow Horses (2010) - show, 6 of 30 episodes so far" in taste.render()


def test_a_title_in_two_libraries_is_merged():
    history = [movie("Heat", 5, plays=2), movie("Heat", 1, plays=1), show("Silo", 3, seen=2), show("Silo", 9, seen=7)]
    taste = build_taste(history, blocked=set(), ratings=None)
    assert titles(taste.recent) == ["Heat", "Silo"]
    heat, silo = taste.recent
    assert heat.watch_count == 3
    assert heat.watched_at == NOW - timedelta(days=1)
    assert silo.viewed_leaf_count == 7
    assert silo.watched_at == NOW - timedelta(days=3)


def test_blocked_titles_are_dropped_from_every_list():
    history = [movie("Seen", 0, tmdb_id=7, plays=5), movie("Other", 1, tmdb_id=8)]
    taste = build_taste(history, blocked={7}, ratings=None)
    assert titles(taste.recent) == ["Other"]
    assert taste.favourites == []


def test_ratings_count_only_when_enabled_trusted_and_human():
    history = [movie("Good", 1, rating=10.0), movie("Fraction", 2, rating=9.1), movie("Plain", 3)]
    off = build_taste(history, blocked=set(), ratings=None)
    assert off.rated_high == [] and titles(off.recent) == ["Good", "Fraction", "Plain"]
    untrusted = build_taste(history, blocked=set(), ratings=RatingsPolicy(threshold=2, trusted=False))
    assert untrusted.rated_high == []
    disabled = build_taste(history, blocked=set(), ratings=RatingsPolicy(threshold=None, trusted=True))
    assert disabled.rated_high == []
    on = build_taste(history, blocked=set(), ratings=TRUSTED)
    assert titles(on.rated_high) == ["Good"]
    assert titles(on.recent) == ["Fraction", "Plain"]


def test_a_low_rated_title_is_never_in_recent_or_favourites():
    history = [movie("Bad", 0, plays=4, rating=2.0), movie("Fine", 1)]
    taste = build_taste(history, blocked=set(), ratings=TRUSTED)
    assert titles(taste.rated_low) == ["Bad"]
    assert titles(taste.recent) == ["Fine"]
    assert taste.favourites == []


def test_rated_lists_are_ordered_and_capped_with_low_first():
    history = [movie(f"Hi{n}", n, rating=8.0 + (n % 2) * 2) for n in range(6)]
    history += [movie(f"Lo{n}", n, rating=2.0) for n in range(6)]
    taste = build_taste(history, blocked=set(), ratings=TRUSTED, rated_limit=6)
    assert len(taste.rated_low) + len(taste.rated_high) == 6
    assert titles(taste.rated_low) == ["Lo0", "Lo1", "Lo2"]
    assert titles(taste.rated_high) == ["Hi1", "Hi3", "Hi5"]


def test_low_rated_fill_the_whole_cap_when_there_are_no_high():
    history = [movie(f"Lo{n}", n, rating=2.0) for n in range(9)]
    taste = build_taste(history, blocked=set(), ratings=TRUSTED, rated_limit=6)
    assert len(taste.rated_low) == 6


def test_favourites_come_from_what_recent_left_and_interleave_by_type():
    history = [
        movie("RecentFav", 0, plays=9),
        movie("M3", 10, plays=3),
        movie("M2", 11, plays=2),
        show("Long", 12, seen=40, total=80),
        show("Done", 13, seen=10, total=10),
        show("Short", 14, seen=5, total=80),
    ]
    taste = build_taste(history, blocked=set(), ratings=None, recent_limit=1)
    assert titles(taste.recent) == ["RecentFav"]
    assert titles(taste.favourites) == ["M3", "Done", "M2", "Long"]


def test_favourite_limit_caps():
    history = [movie(f"M{n}", n, plays=2) for n in range(20)]
    taste = build_taste(history, blocked=set(), ratings=None, recent_limit=0, favourite_limit=4)
    assert len(taste.favourites) == 4


def test_search_candidates_put_rated_high_first_then_favourites_without_repeats():
    history = [movie("Loved", 5, rating=10.0), movie("Fav", 6, plays=3)]
    taste = build_taste(history, blocked=set(), ratings=TRUSTED, recent_limit=0)
    assert titles(taste.search_candidates()) == ["Loved", "Fav"]


def test_render_exact_text():
    history = [
        show("Slow Horses", 1, seen=6, total=30, year=2022),
        movie("Anora", 2, year=2024),
        movie("Heat", 20, plays=3, year=1995),
        show("The Expanse", 21, seen=62, total=62, year=2015),
        movie("Arrival", 30, year=2016, rating=10.0),
        movie("Conclave", 31, year=2024, rating=2.0),
    ]
    taste = build_taste(history, blocked=set(), ratings=TRUSTED, recent_limit=2)
    assert taste.render() == (
        "What this person has watched. Watching a title does not mean they liked it; their own ratings, "
        "where given, do.\n"
        "\n"
        "Watched recently (newest first):\n"
        "- Slow Horses (2022) - show, 6 of 30 episodes so far\n"
        "- Anora (2024) - film\n"
        "\n"
        "Long-time favourites (rewatched, finished, or watched at length):\n"
        "- Heat (1995) - film, watched 3 times\n"
        "- The Expanse (2015) - show, finished\n"
        "\n"
        "Their own Plex ratings:\n"
        "- Rated highly: Arrival (2016), 5 stars\n"
        "- Rated low: Conclave (2024), 1 star"
    )


def test_render_details_and_truncation():
    history = [
        movie("Twice", 0, plays=2, year=None),
        show("Unknown total", 1, seen=4, total=None),
        movie("x" * 100, 2),
    ]
    text = build_taste(history, blocked=set(), ratings=None, recent_limit=5).render()
    assert "- Twice - film, watched twice" in text
    assert "- Unknown total (2010) - show, 4 episodes" in text
    assert "x" * 80 in text and "x" * 81 not in text


def test_half_star_rating_renders():
    history = [movie("Meh", 0, rating=9.0)]
    text = build_taste(history, blocked=set(), ratings=TRUSTED).render()
    assert "Meh (2000), 4.5 stars" in text


def test_empty_history():
    text = build_taste([], blocked=set(), ratings=None).render()
    assert text == (
        "What this person has watched. Watching a title does not mean they liked it; their own ratings, "
        "where given, do.\n"
        "\n"
        "- (no history yet — recommend broadly popular titles)"
    )
