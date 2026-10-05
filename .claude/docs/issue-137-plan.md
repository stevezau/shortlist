# Custom Seasons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Owners can tick, add from presets, create, edit and delete their own seasons from inside the
Seasonal row editor. A season's films come from TMDB tags, a genre, Plex collections, or hand picks.

**Architecture:** The hardcoded `SEASONS` dict becomes a *catalogue* argument: code built-ins plus rows of
a new `seasons` table, built by the server and passed down (`EngineConfig.seasons`). A season's date is a
`DateRule`, and its films are the union of four sources resolved by `load_titles`. A new `/api/seasons`
router serves CRUD, presets, a live preview and three searches. The row editor's seasons field is
rebuilt around a season list, a presets grid and a season editor dialog.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, plexapi, httpx; React 19 + TS + TanStack
Query + shadcn/ui; pytest (+xdist), vitest, Playwright.

**Spec:** `.claude/docs/issue-137-custom-seasons.md` (read it first; decisions are numbered and cited below as D1–D14).

## Global Constraints

- **Engine:**
  - `shortlist/engine/` never imports `shortlist/server/`.
  - Logging is `from loguru import logger`.
  - Type hints on every parameter and return.
  - 120-character lines; `ruff check . --fix && ruff format .`.
- **Built-ins:** Valentine's, Halloween and Christmas keep their tags, genres, row-level timing and
  their EXACT recipe string. No existing row may rebuild because this shipped (D2, D11).
- **The catalogue is always passed explicitly.** It is a keyword-only argument with no default
  (`*, catalogue`), because a silent default would hide rows that follow a custom season.
- **Plex access to collections is read-only:** only `section.collections()` and
  `collection.items()`. Shortlist's own rows (title ending in the marker, `has_shortlist_marker`)
  are never offered as a source (D6).
- **No test touches the network.** A new PMS response shape gets a recorded fixture in
  `tests/fixtures/` (plex-safety rule 11).
- **Every schema change ships an Alembic migration** that re-runs safely (an
  inspector guard like `0082_watch_state_snapshots.py`) and has a downgrade. The drift test must stay green.
- **API types:** after any endpoint change, regenerate `web/openapi.snapshot.json`, then run
  `pnpm -C web gen:api` (`tests/unit/test_openapi_snapshot.py` fails on drift). New TypeScript
  request and response types come from `api-schema.d.ts`, never written by hand.
- **Frontend:**
  - Follows `.claude/rules/frontend.md`: shadcn primitives, Tailwind tokens only, no `any`.
  - Every data view has loading, error with retry, empty, and success states.
  - Uses real `<button>` and `<label>` elements and a visible `:focus-visible`.
  - Copy is plain English and controls say what happens. No blockquotes in any copy-out text.
- **Testing loop:** write the test first and run only that file (`pytest tests/unit/test_x.py -q`,
  `pnpm -C web exec vitest run path`). Only one pytest runs at a time on this host; a hook enforces it.
  Never run bare `pytest` or `-m e2e` until Task 8.
- **No commits** unless the owner has approved committing for this run. If not approved, leave the
  changes staged-free and report the files touched.
- **Copy:**
  - Built-in badge "Built in", custom badge "Yours".
  - Verdicts: "Too few to fill this row ({n} of {size})",
    "People's rows will be much alike — works best in a shared row", "Enough for this row".
  - The no-tag message: "TMDB has no tag matching “{q}”. Try a broader word, or add a collection or
    your own picks below."
  - The missing-collection note: "Not in your library right now. Kometa only creates its seasonal
    collections in season; on nights it's missing, this season uses its other sources."

## Review Focus

1. **A custom season with no TMDB tags.** An empty `with_keywords` would match ALL of TMDB, so
   `load_titles` must skip the keyword query entirely (test in Task 2).
2. **A Kometa collection that doesn't exist tonight.** The season still builds from its other sources,
   the run logs it, and the season does NOT fail (test in Task 2).
3. **Dates near the edges:** last-weekday rules, Easter offsets, New Year's Eve with days after (the
   window crosses the year end), and 29 Feb refused (tests in Task 1).
4. **Deleting a season some row still ticks.** It is unticked everywhere, except when it is a row's
   only season, which refuses with that row's name (test in Task 4).
5. **Renaming a custom season.** The new name renders in row titles, duplicate-title checks see it,
   and a clash with a built-in name is refused (tests in Tasks 1 and 4).

---

### Task 1: DateRule, per-season timing, and the catalogue as an argument

Pure engine changes, plus threading the catalogue through every caller. The server's catalogue is
built-ins only until Task 3, so behaviour is unchanged and every existing test passes after its call
sites are updated.

**Files:**
- Modify: `shortlist/engine/seasons.py` (whole module)
- Modify: `shortlist/engine/models.py` (`RowSeason` adds `content_hash: str = ""`; `EngineConfig` adds `seasons`)
- Modify: `shortlist/engine/rows.py:124-140` (`row_shown_today`), `rows.py:1007` (recipe)
- Modify: `shortlist/engine/placeholders.py:56-70`, `shortlist/engine/delivery.py:~756`, `shortlist/engine/pipeline.py:397-435`
- Create: `shortlist/server/services/season_catalogue.py`
- Modify: `shortlist/server/services/context_builder.py:~1161-1211` (+ where `EngineConfig(...)` is built)
- Modify: `shortlist/server/jobs.py:~1823-1850`, `shortlist/server/services/collection_reconcile.py:216,1068`
- Modify: `shortlist/server/api/collections.py:327-331,835-844,932,1128-1145`
- Test: `tests/unit/test_seasons.py`, `tests/unit/test_seasonal_rows.py`, `tests/unit/test_placeholders.py`, and other existing tests that call these functions

**Interfaces:**
- Produces:
  - `DateRule(kind, month=1, day=1, nth=1, weekday=0, offset=0)` with `.anchor(year) -> date`,
    `.label() -> str` and `.validate() -> None`, which raises `ValueError` with an owner-facing message.
  - `easter_sunday(year) -> date`.
  - `CollectionRef(section_key: str, title: str)`.
  - `Season` (fields below).
  - `Catalogue = Mapping[str, Season]` and `BUILTIN_SEASONS: dict[str, Season]`.
  - `normalise_slugs(slugs, *, catalogue)`, `shown_on(slugs, lead, after, day, *, catalogue)`, and the
    same keyword-only `catalogue` on `build_on`, `last_shown_day`, `row_season_on` and `next_after`.
  - `next_anchors(season, day, count=2) -> list[date]`.
  - `rows.row_shown_today(show_days, slugs, lead, after, now, *, catalogue)`.
  - `placeholders.catalogue_seasons(catalogue)` and `placeholders.season_renderings(template, catalogue)`.
  - `EngineConfig.seasons: Mapping[str, Season]`.
  - `season_catalogue.load_catalogue(session) -> dict[str, Season]`.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_seasons.py` (new classes; keep the existing ones, updating calls to pass `catalogue=BUILTIN_SEASONS`):

```python
from datetime import date

import pytest

from shortlist.engine import seasons as s


class TestEasterSunday:
    @pytest.mark.parametrize(
        ("year", "expected"),
        [
            (2024, date(2024, 3, 31)),
            (2025, date(2025, 4, 20)),
            (2026, date(2026, 4, 5)),
            (2027, date(2027, 3, 28)),
            (2028, date(2028, 4, 16)),
            (2029, date(2029, 4, 1)),
            (2030, date(2030, 4, 21)),
        ],
    )
    def test_matches_the_published_dates(self, year: int, expected: date) -> None:
        assert s.easter_sunday(year) == expected


class TestDateRule:
    def test_fixed(self) -> None:
        assert s.DateRule("fixed", month=3, day=17).anchor(2027) == date(2027, 3, 17)

    @pytest.mark.parametrize(
        ("rule", "year", "expected"),
        [
            (s.DateRule("nth", month=11, nth=4, weekday=3), 2026, date(2026, 11, 26)),  # US Thanksgiving
            (s.DateRule("nth", month=10, nth=2, weekday=0), 2026, date(2026, 10, 12)),  # Canadian Thanksgiving
            (s.DateRule("nth", month=9, nth=1, weekday=6), 2027, date(2027, 9, 5)),  # AU Father's Day
            (s.DateRule("nth", month=5, nth=-1, weekday=0), 2026, date(2026, 5, 25)),  # last Monday of May
            (s.DateRule("nth", month=6, nth=3, weekday=6), 2026, date(2026, 6, 21)),  # US Father's Day
        ],
    )
    def test_nth_weekday(self, rule: s.DateRule, year: int, expected: date) -> None:
        assert rule.anchor(year) == expected

    def test_easter_offset(self) -> None:
        assert s.DateRule("easter", offset=-21).anchor(2027) == date(2027, 3, 7)  # Mothering Sunday

    @pytest.mark.parametrize(
        ("rule", "label"),
        [
            (s.DateRule("fixed", month=3, day=17), "17 March"),
            (s.DateRule("nth", month=11, nth=4, weekday=3), "4th Thursday of November"),
            (s.DateRule("nth", month=5, nth=-1, weekday=0), "Last Monday of May"),
            (s.DateRule("easter"), "Easter Sunday"),
            (s.DateRule("easter", offset=-21), "21 days before Easter"),
            (s.DateRule("easter", offset=1), "1 day after Easter"),
        ],
    )
    def test_label(self, rule: s.DateRule, label: str) -> None:
        assert rule.label() == label

    @pytest.mark.parametrize(
        ("rule", "message"),
        [
            (s.DateRule("fixed", month=2, day=29), "29 February isn't every year"),
            (s.DateRule("fixed", month=4, day=31), "April has 30 days"),
            (s.DateRule("nth", month=11, nth=5, weekday=3), "1st to 4th, or last"),
            (s.DateRule("easter", offset=64), "within 63 days of Easter"),
            (s.DateRule("monthly"), "unknown kind of date"),
        ],
    )
    def test_validate_refuses_with_a_message_the_owner_can_act_on(self, rule: s.DateRule, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            rule.validate()


def _custom(slug: str, rule: s.DateRule, lead: int | None = None, after: int | None = None) -> s.Season:
    return s.Season(
        slug=slug,
        name=slug.title(),
        emoji="*",
        rule=rule,
        description="",
        keywords=(1,),
        lead_days=lead,
        after_days=after,
    )


class TestPerSeasonTiming:
    def test_a_custom_season_uses_its_own_lead_not_the_rows(self) -> None:
        pat = _custom("pat", s.DateRule("fixed", month=3, day=17), lead=7, after=0)
        catalogue = {**s.BUILTIN_SEASONS, "pat": pat}
        assert s.shown_on(["pat"], 30, 0, date(2027, 3, 9), catalogue=catalogue) is None
        assert s.shown_on(["pat"], 30, 0, date(2027, 3, 10), catalogue=catalogue).season is pat

    def test_built_ins_still_use_the_rows_lead(self) -> None:
        window = s.shown_on(["christmas"], 30, 0, date(2026, 11, 25), catalogue=s.BUILTIN_SEASONS)
        assert window is not None and window.anchor == date(2026, 12, 25)

    def test_new_years_eve_with_a_day_after_crosses_the_year(self) -> None:
        nye = _custom("nye", s.DateRule("fixed", month=12, day=31), lead=7, after=1)
        window = s.shown_on(["nye"], 30, 0, date(2027, 1, 1), catalogue={"nye": nye})
        assert window is not None and window.anchor == date(2026, 12, 31)

    def test_a_moving_date_moves(self) -> None:
        thx = _custom("thx", s.DateRule("nth", month=11, nth=4, weekday=3), lead=0, after=0)
        assert s.shown_on(["thx"], 30, 0, date(2026, 11, 26), catalogue={"thx": thx}) is not None
        assert s.shown_on(["thx"], 30, 0, date(2027, 11, 26), catalogue={"thx": thx}) is None  # 2027: 25 Nov


class TestCatalogueArgument:
    def test_normalise_orders_by_calendar_and_knows_custom_slugs(self) -> None:
        pat = _custom("pat", s.DateRule("fixed", month=3, day=17))
        catalogue = {**s.BUILTIN_SEASONS, "pat": pat}
        assert s.normalise_slugs(["christmas", "pat", "valentines"], catalogue=catalogue) == [
            "valentines",
            "pat",
            "christmas",
        ]

    def test_unknown_slug_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown season"):
            s.normalise_slugs(["nope"], catalogue=s.BUILTIN_SEASONS)

    def test_next_anchors(self) -> None:
        thx = _custom("thx", s.DateRule("nth", month=11, nth=4, weekday=3))
        assert s.next_anchors(thx, date(2026, 11, 27)) == [date(2027, 11, 25), date(2028, 11, 23)]
        assert s.next_anchors(thx, date(2026, 11, 26)) == [date(2026, 11, 26), date(2027, 11, 25)]
```

Also add to `tests/unit/test_placeholders.py`:

```python
def test_season_renderings_include_custom_seasons() -> None:
    pat = Season(
        slug="pat",
        name="St Patrick's Day",
        emoji="☘️",
        rule=DateRule("fixed", month=3, day=17),
        description="",
        keywords=(1,),
    )
    catalogue = {**BUILTIN_SEASONS, "pat": pat}
    assert "☘️ St Patrick's Day picks" in season_renderings("{season_emoji} {season} picks", catalogue)
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `pytest tests/unit/test_seasons.py -q`
Expected: FAIL. `easter_sunday`, `DateRule` and `BUILTIN_SEASONS` are not defined.

- [ ] **Step 3: Implement in `seasons.py`.** Replace `month`/`day` with `rule`, rename `SEASONS` →
  `BUILTIN_SEASONS` (no alias, so every caller has to be updated), and add the following:

```python
from collections.abc import Mapping
from typing import Literal

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
#: Any year sorts a catalogue into calendar order; a fixed one keeps `normalise_slugs` free of a clock.
_ORDER_YEAR = 2026
MAX_EASTER_OFFSET = 63


def easter_sunday(year: int) -> date:
    """Western Easter Sunday by the anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — the algorithm's own name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@dataclass(frozen=True)
class DateRule:
    """When a season falls in a given year (issue #137): a fixed day, the nth (or last) weekday of a
    month, or a number of days from Easter. ``weekday`` is Python's: Monday is 0."""

    kind: Literal["fixed", "nth", "easter"]
    month: int = 1
    day: int = 1
    nth: int = 1  # 1-4, or -1 for the last one
    weekday: int = 0
    offset: int = 0

    def anchor(self, year: int) -> date:
        if self.kind == "fixed":
            return date(year, self.month, self.day)
        if self.kind == "nth":
            if self.nth == -1:
                last = date(year, self.month % 12 + 1, 1) - timedelta(days=1) if self.month < 12 else date(year, 12, 31)
                return last - timedelta(days=(last.weekday() - self.weekday) % 7)
            first = date(year, self.month, 1)
            return first + timedelta(days=(self.weekday - first.weekday()) % 7 + 7 * (self.nth - 1))
        return easter_sunday(year) + timedelta(days=self.offset)

    def label(self) -> str:
        if self.kind == "fixed":
            return f"{self.day} {_MONTHS[self.month - 1]}"
        if self.kind == "nth":
            which = "Last" if self.nth == -1 else _ORDINALS[self.nth]
            return f"{which} {_WEEKDAYS[self.weekday]} of {_MONTHS[self.month - 1]}"
        if self.offset == 0:
            return "Easter Sunday"
        days = abs(self.offset)
        return f"{days} day{'s' if days != 1 else ''} {'before' if self.offset < 0 else 'after'} Easter"

    def validate(self) -> None:
        """Raise ValueError, worded for the owner, when this rule cannot name a day every year."""
        if self.kind not in ("fixed", "nth", "easter"):
            raise ValueError("That's an unknown kind of date.")
        if self.kind in ("fixed", "nth") and not 1 <= self.month <= 12:
            raise ValueError("Pick a month.")
        if self.kind == "fixed":
            if (self.month, self.day) == (2, 29):
                raise ValueError("29 February isn't every year — pick 28 February or 1 March.")
            days = (date(2025, self.month % 12 + 1, 1) - timedelta(days=1)).day if self.month < 12 else 31
            if not 1 <= self.day <= days:
                raise ValueError(f"{_MONTHS[self.month - 1]} has {days} days.")
        if self.kind == "nth":
            if self.nth not in (1, 2, 3, 4, -1):
                raise ValueError("Pick the 1st to 4th, or last, weekday of the month.")
            if not 0 <= self.weekday <= 6:
                raise ValueError("Pick a weekday.")
        if self.kind == "easter" and abs(self.offset) > MAX_EASTER_OFFSET:
            raise ValueError(f"Keep it within {MAX_EASTER_OFFSET} days of Easter.")


@dataclass(frozen=True)
class CollectionRef:
    """A Plex collection a season reads its films from, by library and TITLE — never ratingKey: Kometa
    deletes its seasonal collections out of season and recreates them under a new key (D5)."""

    section_key: str
    title: str
```

`Season` becomes the following (keep the existing field comments):

```python
@dataclass(frozen=True)
class Season:
    slug: str
    name: str
    emoji: str
    rule: DateRule
    description: str
    keywords: tuple[int, ...] = ()
    movie_genres: tuple[int, ...] = ()
    keyword_excluded_genres: tuple[int, ...] = ()
    collections: tuple[CollectionRef, ...] = ()
    picks: tuple[tuple[int, MediaType], ...] = ()
    #: A custom season's own timing (D8). None = the row's, which is what every built-in uses.
    lead_days: int | None = None
    after_days: int | None = None
    builtin: bool = False
    #: Changes when the season's SOURCES change, so its rows rebuild (D11). Empty for built-ins.
    content_hash: str = ""


Catalogue = Mapping[str, Season]
```

Built-ins: `rule=DateRule("fixed", month=2, day=14)` and so on, with `builtin=True`. Window functions:

```python
def normalise_slugs(slugs: list[str], *, catalogue: Catalogue) -> list[str]:
    """De-duplicated, in calendar order. Raises ValueError naming anything the catalogue does not have."""
    unknown = sorted({slug for slug in slugs if slug not in catalogue})
    if unknown:
        raise ValueError(f"unknown season(s) {unknown} — choose from {sorted(catalogue)}")
    wanted = set(slugs)
    return sorted((slug for slug in catalogue if slug in wanted), key=lambda slug: (catalogue[slug].rule.anchor(_ORDER_YEAR), slug))


def _windows(slugs, lead_days, after_days, around, catalogue) -> list[SeasonWindow]:
    ...
        season = catalogue.get(slug)
        if season is None:
            continue
        lead = lead_days if season.lead_days is None else season.lead_days
        after = after_days if season.after_days is None else season.after_days
        for year in (around.year - 1, around.year, around.year + 1):
            anchor = season.rule.anchor(year)
            windows.append(SeasonWindow(season=season, anchor=anchor,
                                        starts=anchor - timedelta(days=lead), ends=anchor + timedelta(days=after)))


def next_anchors(season: Season, day: date, count: int = 2) -> list[date]:
    """The season's next ``count`` days on or after ``day`` — what the editor's year strip draws."""
    found: list[date] = []
    year = day.year
    while len(found) < count:
        anchor = season.rule.anchor(year)
        if anchor >= day:
            found.append(anchor)
        year += 1
    return found
```

`shown_on`, `build_on`, `last_shown_day`, `row_season_on` and `next_after` each gain `*, catalogue: Catalogue` and pass it on. `row_season_on` sets `content_hash=window.season.content_hash`.

- [ ] **Step 4: Thread the catalogue through the engine.**
  - `models.EngineConfig` gains
    `seasons: Mapping[str, Any] = field(default_factory=dict)  # seasons.Catalogue; Any avoids an import cycle`.
  - `rows.row_shown_today(..., now, *, catalogue)` passes it to `shown_on`.
  - `rows.py:1007`: the recipe part becomes
    `f"season={spec.season.slug}@{spec.season.anchor.isoformat()}" + (f"#{spec.season.content_hash}" if spec.season.content_hash else "")`,
    which leaves built-in recipes byte-identical.
  - `placeholders.catalogue_seasons(catalogue)` builds
    `RowSeason(..., anchor=s.rule.anchor(2000))`, and `season_renderings(template, catalogue)`.
  - `delivery.py:~756` passes `ctx.config.seasons`.
  - `pipeline._load_season_titles` reads `ctx.config.seasons.get(slug)` instead of `SEASONS`.

- [ ] **Step 5: Thread it through the server.** Create `shortlist/server/services/season_catalogue.py`:

```python
"""The season catalogue a request, job or run sees: the built-ins, then the owner's own (issue #137)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from shortlist.engine.seasons import BUILTIN_SEASONS, Season


def load_catalogue(session: Session) -> dict[str, Season]:
    """Every season a row may follow. Built once per request, job or run and passed down explicitly."""
    return dict(BUILTIN_SEASONS)
```

Pass `catalogue=load_catalogue(session)` at every call site the spec lists:
- `context_builder.py` `row_shown_today`/`row_season_on`, and the `EngineConfig(seasons=...)` it builds;
- `jobs._rows_visibility`, loaded once before the loop;
- `collection_reconcile.py:216,1068`, where the function's caller already holds a session, so take
  `catalogue` as a parameter;
- `collections.py` `_check_seasons`, `_season_status`, `_serialize` (932) and `GET /seasons`.
  In `GET /seasons`, `SeasonOut.month`/`day` come from `season.rule.anchor(local_now().year)`; Task 4
  replaces that endpoint.

- [ ] **Step 6: Update existing tests** that call the changed functions. Find them with
  `rg -n "shown_on\(|build_on\(|row_season_on\(|next_after\(|last_shown_day\(|normalise_slugs\(|row_shown_today\(|season_renderings\(|catalogue_seasons\(|SEASONS\b" tests`.
  Pass `catalogue=BUILTIN_SEASONS`. Engine configs that include seasonal rows set
  `seasons=dict(BUILTIN_SEASONS)`. Never change an assertion.

- [ ] **Step 7: Run the touched test files, one at a time.**

Run: `pytest tests/unit/test_seasons.py tests/unit/test_placeholders.py tests/unit/test_seasonal_rows.py -q`, then each other file Step 6 touched.
Expected: PASS.

---

### Task 2: Season title sources (collections, picks, no-tag safety) and the recipe hash

**Files:**
- Modify: `shortlist/engine/clients/plex_pms.py` (three read-only methods + two dataclasses)
- Modify: `shortlist/engine/clients/tmdb.py` (`search_keywords`, `list_item`, `discover_all(workers=)`)
- Modify: `shortlist/engine/seasons.py` (`load_titles`, `SeasonTitles.missing_collections`, `season_content_hash`)
- Modify: `shortlist/engine/pipeline.py` (`_load_season_titles` passes `ctx.plex`; logs missing collections)
- Create: `tests/fixtures/pms_collection_children.xml` and `tests/fixtures/pms_section_title_search.xml`.
  Record them from SFLIX read-only with the recipe in the "Recording fixtures" note below. Strip any
  `X-Plex-Token` or token attribute, and keep 3–5 items.
- Test: `tests/unit/test_seasons.py` (`TestLoadTitles`), `tests/unit/test_plex_pms.py`, `tests/unit/test_tmdb.py`

**Interfaces:**
- Consumes: `Season`, `CollectionRef` and `DateRule` from Task 1.
- Produces:
  - `LibraryTitle(tmdb_id: int, media_type: MediaType, title: str, year: int | None)`.
  - `LibraryCollection(section_key: str, section_title: str, title: str, count: int, smart: bool, media_type: MediaType)`.
  - `PlexClient.list_collections() -> list[LibraryCollection]`.
  - `PlexClient.collection_members(section_key, title) -> list[LibraryTitle] | None`.
  - `PlexClient.search_titles(query, limit=10) -> list[LibraryTitle]`.
  - `TmdbClient.search_keywords(query, limit=10) -> list[dict]`, where each dict is `{"id", "name", "movies"}`.
  - `TmdbClient.list_item(tmdb_id, media_type) -> dict | None`.
  - `TmdbClient.discover_all(media_type, params, *, workers=1)`.
  - `seasons.load_titles(tmdb, plex, season, library_index) -> SeasonTitles`.
  - `seasons.season_content_hash(season) -> str`.
  - `seasons.MAX_PLEX_SOURCED = 1000`.

Recording fixtures (read-only; run once; scratchpad output):

```bash
DOCKER_HOST=ssh://plex docker exec -i shortlist python - <<'PY'
from pathlib import Path
from shortlist.server.db.session import make_engine, make_session_factory
from shortlist.server.services.secrets import SecretBox
from shortlist.server.settings_store import SettingsStore
from shortlist.engine.clients.plex_pms import PlexClient
cfg = Path("/config")
with make_session_factory(make_engine(cfg))() as s:
    st = SettingsStore(s, SecretBox(cfg)); url, tok = st.get("plex.url"), st.get("plex.token")
plex = PlexClient(url, tok, timeout=60)
sec = plex.sections(("movie",))[0]
col = next(c for c in sec.collections() if not c.smart and 3 <= c.childCount <= 8)
print(plex._server.query(f"/library/collections/{col.ratingKey}/children?includeGuids=1", method=plex._server._session.get).__class__)
PY
```

The implementer adapts this to print the raw XML text (`plex._server._session.get(url + path, headers=...)`)
for `/library/collections/{rk}/children` and `/library/sections/{key}/all?title=free&includeGuids=1&X-Plex-Container-Start=0&X-Plex-Container-Size=5`,
then saves it with every token removed. The probe prints only XML, never the token. Running this probe
needs no write and no approval.

- [ ] **Step 1: Write failing tests**, using fakes rather than network access:

```python
class _Tmdb:
    def __init__(self, lists: dict[str, list[dict]], details: dict[int, dict | None] | None = None) -> None:
        self.lists, self.details, self.calls = lists, details or {}, []

    def discover_all(self, media_type, params, *, workers=1):
        self.calls.append((media_type, dict(params)))
        return self.lists.get(params.get("with_keywords") or params.get("with_genres"), [])

    def list_item(self, tmdb_id, media_type):
        return self.details.get(tmdb_id)


class _Plex:
    def __init__(self, members: dict[tuple[str, str], list[LibraryTitle] | None]) -> None:
        self.members = members

    def collection_members(self, section_key, title):
        return self.members.get((section_key, title))


def _item(tmdb_id: int, genres: list[int] = ()) -> dict:
    return {"id": tmdb_id, "title": f"t{tmdb_id}", "genre_ids": list(genres), "vote_average": 7.0, "vote_count": 100}


class TestLoadTitles:
    LIB = {MediaType.MOVIE: {1: 11, 2: 12, 3: 13, 50: 150, 60: 160}, MediaType.SHOW: {}}

    def test_a_season_with_no_tags_never_runs_a_keyword_query(self) -> None:
        tmdb = _Tmdb({}, {50: _item(50)})
        season = _custom_season(picks=((50, MediaType.MOVIE),))
        titles = s.load_titles(tmdb, _Plex({}), season, self.LIB)
        assert not [c for c in tmdb.calls if "with_keywords" in c[1]]  # an empty with_keywords is ALL of TMDB
        assert titles.contains(50, MediaType.MOVIE)

    def test_collection_members_join_the_season_and_are_in_library(self) -> None:
        plex = _Plex({("1", "Father's Day Movies"): [LibraryTitle(60, MediaType.MOVIE, "Big Fish", 2003)]})
        tmdb = _Tmdb({}, {60: _item(60, [18])})
        season = _custom_season(collections=(s.CollectionRef("1", "Father's Day Movies"),))
        titles = s.load_titles(tmdb, plex, season, self.LIB)
        assert [i["id"] for i in titles.in_library[MediaType.MOVIE]] == [60]
        assert titles.in_library[MediaType.MOVIE][0]["genre_ids"] == [18]

    def test_a_missing_collection_is_reported_not_raised(self) -> None:
        tmdb = _Tmdb({"1": [_item(1)]})
        season = _custom_season(keywords=(1,), collections=(s.CollectionRef("1", "Thanksgiving Movies"),))
        titles = s.load_titles(tmdb, _Plex({}), season, self.LIB)
        assert titles.missing_collections == ("Thanksgiving Movies",)
        assert titles.contains(1, MediaType.MOVIE)

    def test_left_out_genres_drop_tag_and_collection_films_but_never_a_hand_pick(self) -> None:
        plex = _Plex({("1", "C"): [LibraryTitle(60, MediaType.MOVIE, "Scary", 2000)]})
        tmdb = _Tmdb({"1": [_item(1, [27]), _item(2, [35])]}, {60: _item(60, [27]), 50: _item(50, [27])})
        season = _custom_season(
            keywords=(1,), excluded=(27,), collections=(s.CollectionRef("1", "C"),), picks=((50, MediaType.MOVIE),)
        )
        ids = {i["id"] for i in s.load_titles(tmdb, plex, season, self.LIB).in_library[MediaType.MOVIE]}
        assert ids == {2, 50}

    def test_a_title_tmdb_no_longer_has_is_skipped(self) -> None:
        tmdb = _Tmdb({}, {50: None})
        season = _custom_season(picks=((50, MediaType.MOVIE),))
        assert s.load_titles(tmdb, _Plex({}), season, self.LIB).in_library[MediaType.MOVIE] == []

    def test_built_in_queries_are_unchanged(self) -> None:
        tmdb = _Tmdb({})
        s.load_titles(tmdb, _Plex({}), s.BUILTIN_SEASONS["halloween"], self.LIB)
        assert tmdb.calls == [
            (MediaType.MOVIE, {"with_keywords": "3335|180193|232795|9694|182794"}),
            (MediaType.SHOW, {"with_keywords": "3335|180193|232795|9694|182794"}),
            (MediaType.MOVIE, {"with_genres": "27", "vote_count.gte": 200}),
        ]


class TestContentHash:
    def test_changes_with_sources_not_with_name_or_timing(self) -> None:
        a = _custom_season(keywords=(1,))
        assert s.season_content_hash(a) == s.season_content_hash(dataclasses.replace(a, name="Other", lead_days=3))
        assert s.season_content_hash(a) != s.season_content_hash(dataclasses.replace(a, keywords=(1, 2)))


def test_recipe_carries_the_hash_for_custom_seasons_only() -> None:
    ...  # build two RowSpecs via the existing test_seasonal_rows helpers: built-in → recipe ends "@2026-12-25";
    # custom with content_hash "abc" → recipe contains "season=pat@2027-03-17#abc".
```

`_custom_season(**kw)` is a helper returning `Season(slug="c", name="C", emoji="*", rule=DateRule("fixed", month=3, day=17), description="", ...)`.
It maps `excluded` to `keyword_excluded_genres`.

PlexClient tests (in `test_plex_pms.py`, following the file's existing pattern of a plexapi server over recorded XML):
- `collection_members` maps the recorded children to `LibraryTitle`s with TMDB ids, and returns
  `None` when no collection carries that title (case-insensitive).
- `list_collections` skips titles where `has_shortlist_marker` is true.
- `search_titles` returns at most `limit` results and skips items without a TMDB guid.

TmdbClient tests (in `test_tmdb.py`, following its existing `http_retry` monkeypatch pattern):
- `list_item` maps `genres` to `genre_ids` and returns None for a 404.
- `search_keywords` returns `movies` from a discover page-1 `total_results`.
- `discover_all(workers=4)` returns the same ids as `workers=1` over the recorded paged fixture
  `tests/fixtures/tmdb_discover_paged.json`.

- [ ] **Step 2: Run the new tests and make sure they fail**

Run: `pytest tests/unit/test_seasons.py -q -k "LoadTitles or ContentHash"`
Expected: FAIL. The signature mismatches, and `missing_collections` is undefined.

- [ ] **Step 3: Implement.**
  - `load_titles(tmdb, plex, season, library_index)`:
    1. Run the keyword queries only `if season.keywords`, and the genre query if `season.movie_genres`.
       This is today's code for both.
    2. `excluded` applies to keyword items, as today, and to collection members, using `genre_ids` from
       `list_item`. It never applies to picks. "Unless the film has the season's own genre" still holds.
    3. Collection members: `plex.collection_members(ref.section_key, ref.title)`. None appends `ref.title`
       to `missing_collections` and logs a warning: `"{season}: collection “{title}” isn't in your library tonight — using the season's other sources"`.
    4. Bare ids are the picks first, then collection members, de-duplicated, without ids already
       found, and capped at `MAX_PLEX_SOURCED`. Resolve them with `tmdb.list_item` through a
       `ThreadPoolExecutor(max_workers=4)`. None means skip the title. Any exception propagates, so the
       season fails and its rows keep what they have.
    5. `ids` and `in_library` work as today, over everything found.
  - `season_content_hash`:
    `blake2b(json.dumps([sorted(keywords), sorted(movie_genres), sorted(excluded), sorted((c.section_key, c.title) for c in collections), sorted((i, m.value) for i, m in picks)]).encode(), digest_size=8).hexdigest()`.
  - `TmdbClient.list_item`: `data = self.details(...)`. If `{}`, return None. Otherwise return
    `{"id", "title"/"name", "release_date"/"first_air_date", "genre_ids": [g["id"] ...], "vote_average", "vote_count", "poster_path", "overview", "original_language"}`.
  - `TmdbClient.search_keywords`: `_get("/search/keyword", params={"query": q})`, take the first
    `limit` results, and for each fetch `_get("/discover/movie", params={"with_keywords": str(id), "include_adult": "false"})["total_results"]`
    concurrently with 6 workers. Both calls are cached by `_get`.
  - `discover_all(..., workers=1)`: read page 1, then pages 2..N with a `ThreadPoolExecutor(workers)`
    when `workers > 1`. Reassemble in page order, keep the same de-duplication and the same
    all-or-nothing raise, and use the same single cache entry.
  - The PlexClient methods use `self.sections()`, `section.collections()` (`_section_collections` is
    fine), `collection.items()`, `section.search(title=query, maxresults=limit)` and `_tmdb_guid`.
    Media type comes from `section.type`. `LibraryCollection.count` is `collection.childCount`.
  - `pipeline._load_season_titles` calls `load_titles(ctx.tmdb, ctx.plex, catalogued, library_index)`.
    Log `missing_collections` at INFO with the season name.

- [ ] **Step 4: Run the tests until they pass**, one file at a time: `test_seasons.py`,
  `test_plex_pms.py`, `test_tmdb.py`, then `test_seasonal_rows.py` (the pipeline path).

---

### Task 3: The `seasons` table, migration 0095, and the DB-backed catalogue

**Files:**
- Modify: `shortlist/server/db/models.py` (new `SeasonDef` model, table `seasons`)
- Create: `shortlist/server/db/alembic/versions/0095_custom_seasons.py`
- Modify: `shortlist/server/services/season_catalogue.py` (read rows; `season_from_row`; `make_slug`)
- Test: `tests/unit/test_migrations.py` (new `TestCustomSeasons0095`; review the tests pinned to "0093"/"0094"), `tests/unit/test_season_catalogue.py` (new)

**Interfaces:**
- Consumes: `Season`, `DateRule`, `CollectionRef` and `season_content_hash` from Tasks 1 and 2.
- Produces:
  - `SeasonDef` ORM model.
  - `season_catalogue.load_catalogue(session)`: built-ins, then custom seasons by `id`.
  - `season_catalogue.season_from_row(row: SeasonDef) -> Season`.
  - `season_catalogue.make_slug(name: str, taken: set[str]) -> str`.

Model (all columns NOT NULL unless marked):

```python
class SeasonDef(Base):
    """An owner-defined season (issue #137). Built-ins live in code (`seasons.BUILTIN_SEASONS`)."""

    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(40))
    emoji: Mapped[str] = mapped_column(String(16))
    rule_kind: Mapped[str] = mapped_column(String(8))
    month: Mapped[int] = mapped_column(Integer, server_default="1")
    day: Mapped[int] = mapped_column(Integer, server_default="1")
    nth: Mapped[int] = mapped_column(Integer, server_default="1")
    weekday: Mapped[int] = mapped_column(Integer, server_default="0")
    easter_offset: Mapped[int] = mapped_column(Integer, server_default="0")
    lead_days: Mapped[int] = mapped_column(Integer, server_default="7")
    after_days: Mapped[int] = mapped_column(Integer, server_default="0")
    tags: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")  # [{"id": int, "name": str}]
    genre: Mapped[int | None] = mapped_column(Integer, nullable=True)
    excluded_genres: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    collections: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")  # [{"section_key","section_title","title"}]
    picks: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")  # [{"tmdb_id","media_type","title","year"}]
    preset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at / updated_at: copy the exact column definitions the `collections` model uses.
```

- [ ] **Step 1: Write failing tests.**
  - The migration test class mirrors `TestSeasonalRows0092`. Cover: the table exists with those
    columns and defaults; `downgrade` to 0094 drops it; and a second `upgrade` on a database that
    already has the table succeeds (the guard). The drift test must pass unchanged.
  - Catalogue tests:

```python
def test_custom_seasons_follow_the_built_ins(session) -> None:
    session.add(
        SeasonDef(
            slug="st-patricks-day",
            name="St Patrick's Day",
            emoji="☘️",
            rule_kind="fixed",
            month=3,
            day=17,
            tags=[{"id": 209352, "name": "st. patrick's day"}],
            excluded_genres=[27],
        )
    )
    session.commit()
    catalogue = load_catalogue(session)
    assert list(catalogue)[:3] == ["valentines", "halloween", "christmas"]
    pat = catalogue["st-patricks-day"]
    assert pat.rule == DateRule("fixed", month=3, day=17) and pat.lead_days == 7 and pat.after_days == 0
    assert pat.keywords == (209352,) and pat.keyword_excluded_genres == (27,) and not pat.builtin
    assert pat.content_hash == season_content_hash(pat)


@pytest.mark.parametrize(
    ("name", "taken", "slug"),
    [
        ("St Patrick's Day", set(), "st-patricks-day"),
        ("Christmas", {"christmas"}, "christmas-2"),
        ("🎆", set(), "season"),
    ],
)
def test_make_slug(name, taken, slug) -> None:
    assert make_slug(name, taken) == slug
```

- [ ] **Step 2: Run them and make sure they fail.** Run: `pytest tests/unit/test_season_catalogue.py -q` (FAIL: no `SeasonDef`).
- [ ] **Step 3: Implement** the model, the migration (inspector guard, `create_table`, downgrade `drop_table`), and `season_from_row`:
  - `keywords` = tag ids;
  - `movie_genres` = `(genre,)` if set;
  - `picks` = `((p["tmdb_id"], MediaType(p["media_type"])), ...)`;
  - `content_hash` is computed;
  - `description` = `""`.

  `make_slug` lower-cases, folds apostrophes away, turns runs of non-alphanumerics into `-`, and
  falls back to `"season"`. It appends `-2`, `-3`... while the slug is in `taken`. `load_catalogue`
  appends `season_from_row(row)` for each row ordered by `id`.
- [ ] **Step 4: Run** `pytest tests/unit/test_season_catalogue.py -q`, then `pytest tests/unit/test_migrations.py -q`, and expect PASS.

---

### Task 4: `/api/seasons`: list, presets, CRUD, preview and searches

**Files:**
- Create: `shortlist/server/api/seasons.py` (router `prefix="/seasons"`, `dependencies=[Depends(require_owner)]`; register it in `main.py`'s module tuple)
- Create: `shortlist/server/services/library_index.py` (API-side index: engine cache read, else scan; 10-minute memo)
- Modify: `shortlist/engine/seasons.py` (`PRESETS`; `preview(...)`)
- Modify: `shortlist/server/api/collections.py` (remove `GET /collections/seasons` and `SeasonOut`; keep the validation using the catalogue)
- Modify: `web/openapi.snapshot.json` (regenerate with the one-liner in `tests/unit/test_openapi_snapshot.py`'s docstring)
- Test: `tests/integration/test_api_seasons.py` (new), `tests/unit/test_seasons.py` (`TestPresets`, `TestPreview`), `tests/integration/test_api_collections.py` (moved seasons-list assertions)

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces these endpoints, all under `/api/seasons`:
  - `GET ""` returns `list[SeasonOut]`.
  - `GET /presets` returns `list[PresetOut]`.
  - `POST ""` takes `SeasonIn` and returns `SeasonOut` (201).
  - `PUT /{slug}` takes `SeasonIn` and returns `SeasonOut`.
  - `DELETE /{slug}` returns 204.
  - `POST /preview` takes `SeasonPreviewIn` and returns `SeasonPreviewOut`.
  - `GET /tmdb-tags?q=` returns `list[TagOut]`.
  - `GET /plex-collections?q=` returns `list[PlexCollectionOut]`.
  - `GET /library-search?q=` returns `list[LibraryTitleOut]`.

Models (Pydantic v2):

```python
class DateRuleIO(BaseModel):
    kind: Literal["fixed", "nth", "easter"]
    month: int = 1
    day: int = 1
    nth: int = 1
    weekday: int = 0
    offset: int = 0


class TagIO(BaseModel):
    id: int
    name: str


class CollectionIO(BaseModel):
    section_key: str
    section_title: str
    title: str


class PickIO(BaseModel):
    tmdb_id: int
    media_type: Literal["movie", "show"]
    title: str
    year: int | None = None


class SeasonSourcesIO(BaseModel):
    tags: list[TagIO] = Field(default_factory=list, max_length=20)
    genre: int | None = None
    excluded_genres: list[int] = Field(default_factory=list, max_length=5)
    collections: list[CollectionIO] = Field(default_factory=list, max_length=10)
    picks: list[PickIO] = Field(default_factory=list, max_length=200)


class SeasonIn(SeasonSourcesIO):
    name: str = Field(min_length=1, max_length=40)
    emoji: str = Field(min_length=1, max_length=8)
    rule: DateRuleIO
    lead_days: int = Field(7, ge=0, le=MAX_LEAD_DAYS)
    after_days: int = Field(0, ge=0, le=MAX_AFTER_DAYS)
    preset: str | None = None


class UsedByOut(BaseModel):
    id: int
    name: str


class SeasonOut(SeasonSourcesIO):
    slug: str
    name: str
    emoji: str
    description: str
    builtin: bool
    rule: DateRuleIO
    rule_label: str
    next_dates: list[str]  # ISO, the next two days on or after today (server clock)
    lead_days: int | None  # None for built-ins: they follow the row's
    after_days: int | None
    preset: str | None
    used_by: list[UsedByOut]


class PresetOut(SeasonIn):
    key: str
    note: str  # e.g. "TMDB has no Father's Day tag — add a collection or your own picks."


class SeasonPreviewIn(SeasonSourcesIO):
    rule: DateRuleIO


class CollectionCountOut(BaseModel):
    title: str
    section_key: str
    found: bool
    in_library: int


class SeasonPreviewOut(BaseModel):
    next_date: str | None  # None when the rule is invalid
    rule_error: str | None
    total: int
    from_tags: int  # marginal, in this order: tags, genre, collections, picks
    from_genre: int
    from_collections: int
    from_picks: int
    per_tag: dict[int, int]  # tag id -> films in your libraries
    per_collection: list[CollectionCountOut]
    sample: list[str]  # up to 10 titles, most-voted first


class TagOut(BaseModel):
    id: int
    name: str
    movies: int  # films on TMDB


class PlexCollectionOut(BaseModel):
    section_key: str
    section_title: str
    title: str
    count: int
    smart: bool


class LibraryTitleOut(BaseModel):
    tmdb_id: int
    media_type: Literal["movie", "show"]
    title: str
    year: int | None
```

Behaviour:
- **Validation**, done in a helper shared by POST and PUT, returns 422 with the message in `detail`:
  - `DateRule(...).validate()`.
  - At least one source (tags, genre, collections or picks): "Add at least one tag, collection or film."
  - A name unique case-insensitively across the catalogue, excluding this season on PUT:
    "There's already a season called “Christmas”."
- **POST:** slug = `make_slug(name, set(catalogue))`.
- **PUT:** the slug never changes, and a built-in slug returns 403 "Built-in seasons can't be edited."
- **DELETE:**
  - A built-in returns 403.
  - Load every `Collection` whose `seasons` contains the slug. If any of them has no other season,
    return 409 with "“{season}” is the only season in {row names}. Give those rows another season,
    or delete them, first."
  - Otherwise remove the slug from each row and delete the season in ONE transaction.
- **`used_by`:** the rows whose `seasons` contain the slug, as `[{id, name}]`.
- **`GET /presets`:** returns `seasons.PRESETS` minus any preset key already stored in `seasons.preset`.
- **`POST /preview`:** runs in an executor (`run_in_executor`, like `users.py:557-595`).
  1. Builds a draft `Season`.
  2. Reads `library_index(...)`.
  3. Calls `seasons.preview(tmdb, plex, draft, library_index, workers=6)`.
  4. Returns 503 with "Add a TMDB API key in Settings first." when TMDB isn't set up, and 502 with a
     redacted message when TMDB or Plex fails.
- **`seasons.preview`** returns everything `SeasonPreviewOut` needs. The total is the union after
  exclusions, as `load_titles` computes it. `per_tag` uses `discover_all` per single tag, and
  `per_collection` uses `collection_members`. Marginal counts are computed in source order. The sample
  is the in-library titles sorted by `vote_count` descending, collection and pick titles included.
  When the rule is invalid it still counts the films, setting `next_date=None` and `rule_error`.
- **`library_index(...)`:** for each section, read the engine cache key
  `f"index3:{section.key}:{plex.section_signature(section)}"` via `DbCache(sessions, kind="library_index")`.
  On a miss, scan with `build_library_index` and memoise it in process for 10 minutes keyed by
  `(section.key, signature)`. It never writes the engine cache, whose `tallied` envelope it can't produce.
- **Searches:** `q` must be 2 or more characters, otherwise return `[]`.
  - `/tmdb-tags` returns `tmdb.search_keywords(q)`.
  - `/plex-collections` returns `plex.list_collections()` filtered by a case-insensitive substring and
    sorted by title.
  - `/library-search` returns `plex.search_titles(q, 10)`.
  - Clients come from `run_service`, as `users.py:557` does.
- **`PRESETS`:** exactly the spec's table, as `Preset(key, season: Season, note)` entries. Tag names
  sit in comments beside each id, the way the built-ins do it.

- [ ] **Step 1: Write failing tests** in `tests/integration/test_api_seasons.py`, using the existing
  integration client fixtures and fakes. Mock TMDB and Plex at the `run_service` boundary, the same way
  `tests/integration/test_api_users.py` does for title search.
  1. POST then GET returns the season with `builtin=False`, `rule_label="4th Thursday of November"`,
     `next_dates` of two ISO dates, and `used_by=[]`.
  2. POST refuses 29 Feb, a duplicate of "christmas" (any case), and no sources, each 422 with the exact
     message.
  3. PUT renames the season and keeps the slug. PUT and DELETE on `halloween` return 403.
  4. Deletion:
     - Row A has `[halloween, slug]` and row B has `[slug]`. DELETE returns 409 naming B, and nothing
       changes.
     - Row B's season list set to `[christmas, slug]`. DELETE returns 204, and both rows lose the slug.
  5. A row PATCHed to a custom slug is accepted, and an unknown slug is still refused.
  6. `GET /presets` hides a preset once a season stored with `preset="thanksgiving_us"` exists.
  7. `POST /preview`, with the fake TMDB returning items 1–3 for tag 4543 and the library holding 1 and 2:
     `total == 2`, `per_tag == {4543: 2}`, and `sample` ordered by votes.
     A collection the fake Plex lacks gives `per_collection[0].found is False`.
  8. `/tmdb-tags?q=t` returns `[]`, and so does `/library-search?q=a`, because both queries are too short.

  Unit tests in `test_seasons.py`:
  - `TestPresets`: every preset validates; Father's Day presets have no tags and a non-empty note; tag
    ids equal the spec's table.
  - `TestPreview`: marginal counts add up to the total, and an invalid rule still counts films.
- [ ] **Step 2: Run them and make sure they fail.** Run: `pytest tests/integration/test_api_seasons.py -q` (FAIL: 404s).
- [ ] **Step 3: Implement**, then regenerate `web/openapi.snapshot.json`.
- [ ] **Step 4: Run** `pytest tests/integration/test_api_seasons.py -q`, then `pytest tests/unit/test_openapi_snapshot.py tests/integration/test_api_collections.py -q`, and expect PASS.

---

### Task 5: Row editor UI: season list, presets, year strip and the season editor

**Files:**
- Modify: `web/src/lib/api.ts` (the endpoints above; remove `getSeasons` from the collections path), `web/src/lib/queries.ts` (`useSeasons` → `/api/seasons`; add `useSeasonPresets`, `useSeasonPreview(draft)` (debounced 400 ms by the caller), `useTmdbTags(q)`, `usePlexCollections(q)`, `useLibrarySearch(q)`, and `useCreateSeason`/`useUpdateSeason`/`useDeleteSeason`, which invalidate `["seasons"]`, `["season-presets"]` and the collections list)
- Modify: `web/src/lib/types.ts` (delete the hand-written `Season`; re-export the generated `SeasonOut` type)
- Create: `web/src/lib/season-verdict.ts` (+ `.test.ts`)
- Modify: `web/src/components/rows/row-seasons-field.tsx` (rebuild; props add `rowSize: number`, `perPerson: boolean`)
- Create: `web/src/components/rows/seasons/season-list-item.tsx`, `season-presets.tsx`, `season-year-strip.tsx`, `season-editor-dialog.tsx`, `season-tag-picker.tsx`, `season-collection-picker.tsx`, `season-picks-picker.tsx`, `season-summary.tsx`
- Modify: `web/src/components/rows/row-kind-settings.tsx:515-526` (pass `rowSize`, `perPerson`)
- Modify: `web/src/lib/seasons.ts` (`seasonWindowLabel` uses `next_dates` + timing; keep `seasonStatusLine`, `isNightly`)
- Test: `row-seasons-field.test.tsx` (update), `seasons/season-editor-dialog.test.tsx` (new), `seasons/season-year-strip.test.tsx` (new), `seasons.test.ts`, the fixtures in `row-kind-fixtures.ts` and `row-editor-kinds.test.tsx`

**Interfaces:**
- Consumes: Task 4's generated types: `SeasonOut`, `PresetOut`, `SeasonIn`, `SeasonPreviewOut`, `TagOut`, `PlexCollectionOut`, `LibraryTitleOut`.
- Produces: `seasonVerdict(total: number, rowSize: number, perPerson: boolean): { level: "few" | "alike" | "ok"; text: string }`.

```ts
// web/src/lib/season-verdict.ts
export const ALIKE_BELOW = 100; // #124 judged 63 too few to make per-person rows differ (spec D10)

export function seasonVerdict(total: number, rowSize: number, perPerson: boolean) {
  if (total < rowSize) return { level: "few" as const, text: `Too few to fill this row (${total} of ${rowSize})` };
  if (perPerson && total < ALIKE_BELOW)
    return { level: "alike" as const, text: "People's rows will be much alike — works best in a shared row" };
  return { level: "ok" as const, text: "Enough for this row" };
}
```

**Layout and behaviour.** The canvas mockup is the reference, with the deviations noted here.

- **`RowSeasonsField`**
  - Heading "Seasons" and the hint "Ticked seasons show in this row on their dates. Only films in
    your libraries are used."
  - Below it, one `SeasonListItem` per catalogue season, then "Add more seasons", then the timing
    line, then `SeasonYearStrip`, then the existing status line and the not-nightly warning.
  - Keep the "last ticked season can't be unticked" rule and the `{season}` name hint.
- **`SeasonListItem`** is a `<label>` row with:
  - a checkbox;
  - the emoji and name, with a badge "Built in" or "Yours";
  - `rule_label` and "next {date}";
  - the timing: "from {n} days before", using the row's value for built-ins;
  - the film count from `useSeasonPreview`, for custom seasons only, loaded lazily with a skeleton
    while it loads. Built-ins show none, since they are known large;
  - the verdict chip when it isn't "ok";
  - an "Edit" `<button>` for custom seasons, which opens the editor on that season.
- **`SeasonPresets`:**
  - A grid of `PresetOut` cards: 1 column under 640px, 2 columns at 640px and up, 3 at 1024px and up.
  - Each card shows the emoji, name, `rule_label`, its count from the preview (skeleton while
    loading), the verdict chip, the note, and an "Add" button.
  - Add opens the editor pre-filled, with `preset` set.
  - A "Create your own" primary button sits at the top right of the grid.
  - When the grid is empty: "You've added every ready-made season."
  - The whole grid sits in a collapsed `<details>` labelled "Add more seasons" that is open by default
    when the row has only built-ins.
- **Timing line:** "Built-in seasons show from [30] days before and stay [0] days after." It uses the
  existing inputs and clamps, and shows only when a built-in is ticked.
- **`SeasonYearStrip`:**
  - A 12-month bar. Each ticked season's next window is drawn from `next_dates[0]` minus its lead
    through plus its after, with the row's values for built-ins.
  - It has a today marker and a text line describing any overlap: "{A} and {B} overlap on {dates}:
    the nearer date wins."
  - The bars are `aria-hidden`, and the text line carries the meaning.
- **`SeasonEditorDialog`** is a shadcn `Dialog`, max-width 1100px and scrollable. The title is "Create
  your own season" or "Edit {name}", and the description is "Saved for the whole server — any Seasonal
  row can tick it."
  - **Name:** an emoji input plus a name input.
  - **When:** three toggle buttons ("Same day every year", "A weekday in a month", "Days from
    Easter"), each with native `<select>`s. The next-date line comes from the preview, e.g. "Next: Thu
    26 Nov 2026 — shows from Thu 12 Nov, hidden again from Fri 27 Nov". A `rule_error` shows in red
    under the selects. Lead and after inputs sit below.
  - **Films**, four blocks:
    - **TMDB tags:** a search input (debounced 250 ms) with results showing "{name} — {movies} films
      on TMDB" and an Add button. Chosen tags show "{n} in your libraries" from `per_tag`, with a
      remove button. On zero results, the no-tag copy from Global Constraints.
    - **From your library:** a collection search showing results as title, library, "{count}
      films", and a "Smart" badge if smart, each with Add. A chosen collection shows `per_collection`;
      when `found` is false it shows the missing-collection note.
    - **Picked by hand:** a library search with Add, and chips with remove buttons.
    - **Genres:** "Also include a genre" as a native select of TMDB movie genres, using the existing
      genres query if there is one, otherwise a static list of TMDB's 19 movie genres. "Leave out"
      is a multi-select of genres shown as chips. The help text reads "Hand-picked films are never
      left out."
  - **`SeasonSummary`:** sticky beside the form at 1024px and up, and stacked under it below that.
    - "{total} films in your libraries", the verdict from `seasonVerdict` using the row's size and
      mode, the marginal counts, the sample, and "Also used by: {names}" when `used_by` names other rows.
    - While the first preview loads: "Counting films in your libraries… the first count takes a few
      seconds." An error state has a Retry button.
  - **Footer:**
    - "Save and add to this row" stays disabled until there is a name, a valid rule, and at least
      one source; when disabled, a line says what's missing. Saving ticks the slug in the row's
      form value.
    - Cancel.
    - "Delete season" (edit only) opens a confirm dialog:
      - When other rows use the season: "Remove “{name}” from {rows} and delete it?"
      - Otherwise: "Delete “{name}”?"
      - A 409 shows the server's message in the dialog.

- [ ] **Step 1: Write failing tests** (vitest + testing-library; mock `api` the way `row-seasons-field.test.tsx` does today):

```ts
// season-verdict.test.ts
it.each([
  [10, 15, true, "few"], [40, 15, true, "alike"], [40, 15, false, "ok"], [150, 15, true, "ok"], [15, 15, false, "ok"],
])("verdict(%i, %i, %s) is %s", (total, size, perPerson, level) => {
  expect(seasonVerdict(total, size, perPerson).level).toBe(level);
});
```

`season-editor-dialog.test.tsx` cases, each with its arrange, act and assert:
1. Typing "father's day" with `getTmdbTags` returning `[]` shows the no-tag copy exactly.
2. Adding a collection whose preview returns `found:false` shows the missing-collection note.
3. Save is disabled with "Add at least one tag, collection or film." until a pick is added. Then
   Save calls `createSeason` with a body whose `picks[0].tmdb_id` is the picked id and `rule` is
   `{kind:"nth",month:11,nth:4,weekday:3,...}`, and `onSaved` receives the returned slug.
4. A preview whose total is 26 for a per-person 15-film row shows "People's rows will be much alike —
   works best in a shared row".
5. Opened from a preset, the name, emoji, rule and tags are pre-filled, and `preset` is sent on save.
6. Delete when `used_by` names another row shows "Remove “Thanksgiving” from Family picks and delete
   it?". A 409 response shows its message.
7. A `rule_error` from the preview renders under the date selects.

`row-seasons-field.test.tsx` cases, in addition to the existing ones, which are updated to the new
catalogue shape:
1. A custom season lists with the "Yours" badge and Edit.
2. The presets grid shows cards from `getSeasonPresets`, and Add opens the dialog pre-filled.
3. Ticking a season adds it in calendar order.
4. The timing line is hidden when only custom seasons are ticked.

`season-year-strip.test.tsx`: Thanksgiving (US) plus Christmas with a 30-day lead renders the
overlap sentence naming both.

- [ ] **Step 2: Run them and make sure they fail.** Run: `pnpm -C web exec vitest run src/components/rows src/lib/season-verdict.test.ts`
- [ ] **Step 3: Implement.** Regenerate types with `pnpm -C web gen:api`, using the snapshot from Task 4.
- [ ] **Step 4: Run the same command.** Expect PASS. Then run `pnpm -C web exec tsc -b --force` and `pnpm -C web exec eslint src`.

---

### Task 6: e2e: create a season from the Seasonal row editor

**Files:**
- Modify: `tests/fakes/fake_plex.py` (honour `title=` on `/library/sections/{id}/all`, matching the recorded fixture shape from Task 2; make sure `/library/collections/{rk}/children` items carry `<Guid id="tmdb://…">`)
- Modify: `tests/e2e/conftest.py` (fake TMDB: `/search/keyword`, `/discover/movie`, `/discover/tv`, paged like `tmdb_discover_paged.json`)
- Create: `tests/e2e/test_custom_seasons_e2e.py`

- [ ] **Step 1: Write the test.** Follow the existing e2e login and helper pattern, and click by coordinate where the existing tests do.
  1. Open Rows, Add a row, Seasonal. The season list shows the three built-ins.
  2. Open "Add more seasons" and click Add on "Thanksgiving (US)". The dialog opens pre-filled.
  3. Wait for the summary to show a number. Add a hand-picked film found by typing part of a title the
     fake library holds. Save.
  4. The list shows "Thanksgiving (US)" ticked with "Yours". Save the row.
  5. `GET /api/collections/{id}` has `seasons` containing `thanksgiving-us`. `GET /api/seasons` lists it
     with `used_by` naming the row.
- [ ] **Step 2: Build the SPA, then run only this test.** `pnpm -C web build && pytest -m e2e tests/e2e/test_custom_seasons_e2e.py -q`. Expect PASS. The memory note "e2e errors on a stale web/dist" is why it builds first.

---

### Task 7: Docs

**Files:**
- Modify:
  - `docs/guides/rows.md`, "Seasonal rows" (~323-370): how seasons work, adding a ready-made season,
    creating your own, the four sources, a Kometa collection that's missing out of season, the
    built-in vs custom timing, and the counts and verdicts.
  - `docs/reference/api.md` (~170): replace the seasons-list line with the `/api/seasons` endpoints.
  - `docs/reference/settings.md` (~312): the `seasons` table and that `collections.seasons` may hold
    custom slugs.
  - `README.md` (~139): "Halloween, Christmas & Valentine's, plus any season you add".
  - `docs/_data/templates.yml:30`.
  - `web/src/lib/row-templates.ts` (~190-201, `PROPER_NOUNS` stays) and `row-kind-meta.ts:59`, so the
    blurbs mention adding your own.
- Regenerate: `python scripts/build_llms_full.py`

- [ ] **Step 1:** Write the docs in the voice and style of the surrounding sections. Add no environment-specific details.
- [ ] **Step 2:** Run `python scripts/build_llms_full.py && pytest tests/unit/test_llms_full.py -q`. Expect PASS.

---

### Task 8: Verify everything

- [ ] **Step 1: Full suites, through the `verifier` agent.**
  - `ruff check . && ruff format --check .`
  - `pytest`
  - `pnpm -C web test`
  - `pnpm -C web exec tsc -b --force`
  - `pnpm -C web exec eslint .`
  - `pnpm -C web build`
  - `pytest -m e2e`

  These are CI's exact commands.
- [ ] **Step 2: Architecture Review.** Dispatch the agent on the whole diff, because it includes a
  migration and reads foreign Plex collections. Fix every HIGH finding.
- [ ] **Step 3: Live proof, read-only.** Pipe the worktree's `seasons.py` and the new client methods
  into the production container, using the embedded-module recipe in memory
  `live-write-probes-must-survive-a-redeploy`, and run `seasons.preview` against SFLIX:
  - The Thanksgiving (US) preset gives total 15, or 26 with family reunion and family gathering added.
  - St Patrick's gives 58 before Horror is left out, and fewer after.
  - A real collection's `per_collection` count equals its `childCount`.

  Counts only. Nothing is written.
- [ ] **Step 3b: Check the recipe hasn't changed.** Run the built-in recipe string against a stored row
  from production, read-only, to prove no existing row rebuilds.
- [ ] **Step 4: Check it in a browser.** Boot the real app on :5960 against `fake_plex`, per memory
  `browser-verify-with-fake-plex`. At 320, 1024 and 1280px wide, walk through: add a preset, create a
  Father's Day season with no tag via a hand pick, edit it, try to delete it while it's another row's
  only season, then delete it. Take screenshots.
