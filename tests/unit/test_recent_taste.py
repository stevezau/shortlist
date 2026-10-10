"""The recent-titles prompt text, scoped to what a person allows (#152): same format as ever, fewer titles."""

from datetime import UTC, datetime, timedelta

from shortlist.engine.curator.base import taste_summary
from shortlist.engine.history import RatingsPolicy
from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
from shortlist.engine.taste import TastePrompt, recent_taste

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def movie(title, days_ago=0, *, tmdb_id=None, year=2000, rating=None) -> WatchedItem:
    return WatchedItem(
        title=title,
        media_type=MediaType.MOVIE,
        watched_at=NOW - timedelta(days=days_ago),
        tmdb_id=tmdb_id,
        year=year,
        user_rating=rating,
    )


def test_it_is_byte_identical_to_taste_summary_when_nothing_is_filtered():
    history = [movie(f"M{n}", n, year=2000 + n) for n in range(30)] + [movie("No year", 40, year=None)]
    history.append(movie("M0", 50))  # a second copy of a title collapses, as in taste_summary
    profile = UserProfile(username="a", plex_account_id=1, user_type=UserType.SHARED, history=history)
    prompt = recent_taste(history, blocked=set(), ratings=None)
    assert prompt == TastePrompt(taste_summary(profile), False)


def test_a_dont_seed_title_is_dropped():
    history = [movie("Blocked", 0, tmdb_id=7), movie("Kept", 1, tmdb_id=8)]
    text = recent_taste(history, blocked={7}, ratings=None).text
    assert "Blocked" not in text and "- Kept (2000)" in text


def test_a_trusted_low_rating_is_dropped():
    history = [movie("Hated", 0, rating=2.0), movie("Kept", 1)]
    policy = RatingsPolicy(threshold=2, trusted=True)
    assert "Hated" not in recent_taste(history, blocked=set(), ratings=policy).text


def test_the_low_rating_stays_when_ratings_are_off_untrusted_or_not_human():
    history = [movie("Hated", 0, rating=2.0), movie("Fraction", 1, rating=1.5)]
    for policy in (None, RatingsPolicy(threshold=None, trusted=True), RatingsPolicy(threshold=2, trusted=False)):
        assert "Hated" in recent_taste(history, blocked=set(), ratings=policy).text
    assert "Fraction" in recent_taste(history, blocked=set(), ratings=RatingsPolicy(threshold=2, trusted=True)).text


def test_the_limit_counts_titles_that_survive():
    history = [movie("Hated", 0, rating=2.0)] + [movie(f"M{n}", n + 1) for n in range(5)]
    text = recent_taste(history, blocked=set(), ratings=RatingsPolicy(threshold=2, trusted=True), limit=3).text
    assert text.count("\n- ") == 3 and "Hated" not in text


def test_a_title_rated_low_in_one_library_is_dropped_from_every_copy():
    """Plex rates each library's copy separately; seeds drop a disliked title by (tmdb_id, media), so must this."""
    history = [movie("Hated", 0, tmdb_id=5), movie("Hated", 3, tmdb_id=5, rating=2.0), movie("Other", 1, tmdb_id=6)]
    policy = RatingsPolicy(threshold=2, trusted=True, blocked={(5, MediaType.MOVIE)})
    text = recent_taste(history, blocked=set(), ratings=policy).text
    assert "Hated" not in text and "- Other (2000)" in text
