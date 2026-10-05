"""A month-long spotlight follows calendar boundaries, including leap years."""

from datetime import date

import pytest

from shortlist.engine.seasons import DateRule, Season, build_on, next_anchors, shown_on


def _month(month: int) -> Season:
    return Season("spotlight", "Spotlight", "🎬", DateRule("month", month=month), "", lead_days=0, after_days=0)


@pytest.mark.parametrize("year,last", [(2027, 28), (2028, 29), (2100, 28), (2400, 29)])
def test_february_covers_every_day_and_never_march(year: int, last: int) -> None:
    season = _month(2)
    catalogue = {season.slug: season}
    for day in range(1, last + 1):
        window = shown_on([season.slug], 90, 30, date(year, 2, day), catalogue=catalogue)
        assert window is not None
        assert (window.starts, window.ends, window.anchor) == (
            date(year, 2, 1),
            date(year, 2, last),
            date(year, 2, last),
        )
    assert shown_on([season.slug], 90, 30, date(year, 1, 31), catalogue=catalogue) is None
    assert shown_on([season.slug], 90, 30, date(year, 3, 1), catalogue=catalogue) is None


def test_december_month_prebuilds_before_open_and_ends_before_new_year() -> None:
    season = _month(12)
    catalogue = {season.slug: season}
    window = build_on([season.slug], 0, 0, date(2026, 11, 30), catalogue=catalogue)
    assert window is not None
    assert (window.starts, window.ends) == (date(2026, 12, 1), date(2026, 12, 31))
    assert shown_on([season.slug], 0, 0, date(2027, 1, 1), catalogue=catalogue) is None


def test_a_day_event_interrupts_a_month_then_the_month_returns() -> None:
    month = _month(2)
    event = Season("event", "Event", "💘", DateRule("fixed", month=2, day=14), "", lead_days=7, after_days=0)
    catalogue = {season.slug: season for season in (month, event)}
    for day, expected in [(1, "spotlight"), (7, "event"), (14, "event"), (15, "spotlight"), (28, "spotlight")]:
        window = shown_on(list(catalogue), 30, 0, date(2027, 2, day), catalogue=catalogue)
        assert window is not None and window.season.slug == expected


def test_month_rule_normalises_ignored_fields_and_keeps_current_month_in_next_dates() -> None:
    rule = DateRule("month", month=2, day=29, nth=4, weekday=6, offset=20)
    rule.validate()
    assert rule.normalised() == DateRule("month", month=2)
    assert rule.label() == "All of February"
    assert next_anchors(_month(2), date(2028, 2, 15)) == [date(2028, 2, 29), date(2029, 2, 28)]


def test_a_day_event_wins_a_tie_with_a_month_end_anchor() -> None:
    month = _month(10)
    event = Season("halloween", "Halloween", "🎃", DateRule("fixed", month=10, day=31), "", lead_days=7)
    catalogue = {season.slug: season for season in (month, event)}
    window = shown_on(list(catalogue), 30, 0, date(2026, 10, 25), catalogue=catalogue)
    assert window is not None and window.season.slug == "halloween"


@pytest.mark.parametrize("month", [0, 13])
def test_month_rule_refuses_invalid_month(month: int) -> None:
    with pytest.raises(ValueError, match="Pick a month"):
        DateRule("month", month=month).validate()
