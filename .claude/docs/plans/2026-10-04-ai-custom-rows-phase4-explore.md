# AI Custom Rows — Phase 4: Explore mode + over-time controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An AI row can pick a new theme for each person on a schedule (Explore), with the next theme built a day early and editable; and every AI row gains three opt-in controls for how it changes over time: how much changes each refresh, no repeats for N days, and keep out titles already in other rows.

**Architecture:** One migration adds per-row columns and a `theme_history` table. A small pure engine module (`over_time.py`) holds the exclusion filter and the pick-history protocol; `_build_section_picks` consults it, and the person's own theme is resolved (`RowSpec.for_person`) where a row's spec first enters the per-user loop. A new `themes.rotate` job (settings-backed cron) promotes "Up next" when due and authors the following theme with the phase-3 `author_theme`, saving it through a `theme_store.py` service extracted from the themes API. One UI task, one docs task, one verification task.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, APScheduler + the durable job queue, pytest + hypothesis, React 19 + TS + TanStack Query + vitest.

**Spec:** `.claude/docs/discussion-138-ai-custom-rows.md` (Decision 4, "The row", "The controls"); handoff `.claude/docs/plans/2026-10-04-discussion-138-handoff.md` (Phase 4 bullet). Builds on `.claude/docs/plans/2026-10-04-ai-custom-rows-phase3-ai-row.md` (themes table, `themes.py`, `theme_author.py`, `ThemeSpec`, migration 0098) and follows the shape of `…phase2-limits.md`.

**Precondition:** phase 3 is on `dev` and merged into this branch (0098 applied, `AI row` kind and `/api/themes*` working). Do not start before that. Phase-1/2 post-deploy checks in the handoff are not this plan's job.

## Global Constraints

- **Explore is per-person only.** Phase 3 ruled that an AI row is never a shared row (`_validate_theme` in `shortlist/server/api/collections.py` returns 422 unless `build == "per_person"`). This plan adds no shared-row authoring, no `user_id IS NULL` targets and no shared rotation. On the server, "AI row" means `Collection.theme_id is not None`; every validation below keys on that, and clearing `theme_id` resets `theme_mode` to `"fixed"` (and nulls the over-time controls, which exist only on AI rows in v1).
- Every new setting defaults to today's behaviour: new columns NULL / `fixed` / empty = off. Nothing rebuilds on upgrade.
- `row_recipe` gains a part ONLY when a control is non-default; with every control at default the recipe is byte-identical to today's (pinned by test, captured BEFORE editing, as in phases 1–2).
- `shortlist/engine/` must not import from `shortlist/server/`. The engine sees history only through the `PickHistory` protocol.
- Explore never empties a row: a failed or paused theme write keeps the current theme and logs an event. A failed author call never changes what is delivered.
- AI is called only when authoring a theme (save/refine/next-theme). Picking per person stays AI-free. `ai_paused` stops explore authoring for that row.
- Dry runs are excluded from every "shown" history. A dry run writes no `PickRow` (its picks live in `RunUser.trace["picks"]`), and the history reader additionally joins `Run` and requires `Run.dry_run is False`; a `PickRow` with `run_id IS NULL` (nullable column) is not from a real run and does not count either.
- This phase writes no new Plex/plex.tv state and changes no share filter; rows still go through the existing leak-safe delivery. Say so in the Architecture Review prompt; §12 of `jobs-and-runs-design.md` is not touched.
- No deployment-specific clock times in code or docs. The rotation job's schedule is a settings-backed cron (`themes.rotate_cron`) and correctness never depends on when it fires (Decision 4).
- Tests: no network; assert the kwargs the SUT controls; cover the matrix (theme mode fixed/explore × control set/unset × person A/person B).
- One pytest at a time (hook enforced). During the edit loop run only the named test file; the full suite runs once, in Task 6.
- Python style: `loguru` logger, type hints, Google docstrings on public APIs, 120 cols. Commits: Conventional Commits, end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Stage files explicitly, never `git add -A`. Ask Steve before committing.
- Local python: `PYTHONPATH=. /home/data/workspace/shortlist/.venv/bin/python` (no `python` on PATH in the worktree).

## Decisions made by this plan

1. **History table:** one table `theme_history(id, collection_id, user_id, theme_id, theme_name, state, started_at, due_at)`. `user_id` is NOT NULL (explore is per-person). `state` is `current | next | past`. "Avoid the last 6" reads the 6 newest rows for that (row, person). This is the spec's `themes_history` with `state` and `due_at` added.
2. **Whose taste** (Decision 4): each person's theme is authored with that person's `UserProfile` (one AI call per person per interval). A blank `explore_brief` means "choose a theme this person would love from what they watch".
3. **Rotation timing is the job's business, not the engine's.** `themes.rotate` is a daily job on a settings-backed cron (`themes.rotate_cron`, default in `DEFAULT_CRONS`; the owner can move it or blank it). A (row, person) target is promoted when `now >= current.started_at + theme_days` and a `next` exists; its next theme is authored when `now >= current.started_at + theme_days − NEXT_LEAD_DAYS` and none exists; `NEXT_LEAD_DAYS = 1` is a documented constant in `theme_rotation.py` ("built a day early"). No current theme for a person: author and make it current immediately. Rows have their own crons, so the engine never reads the clock for this: at a row's run it uses whichever theme the DB says is `current` for that person (a row that runs before the job promotes simply builds with the old theme and rebuilds on the next run, because the `theme=<slug>#<hash>` recipe part changes). A person with no `current` row uses the row's own theme (`Collection.theme_id`, the starter theme phase 3 saved with the row).
4. **`refresh_share`** (0 < x ≤ 1) is the share of picks swapped on a refresh night; `keep_n = round((1 − share) × k)`. NULL keeps `_KEEP_FRACTION = 2 / 3` (`shortlist/engine/rows.py:617`).
5. **Exclusions apply to NEW candidates only**, never to prior picks kept on a refresh night (otherwise cooldown would rotate out everything every night). Cooldown is scoped to the same row for the same person; cross-row overlap is `avoid_rows`.
6. **"Shown within N days" (cooldown) ruling.** A title counts as shown within N days by the FIRST time it ever appeared for that person in that row: the earliest `PickRow.created_at` for `(user, row, title)` over real runs only. Why: a kept pick is carried forward and restamped by every refresh night, so a plain `created_at >= since` filter would count a kept pick as "shown today" every night. Titles currently kept in the row are SPARED (never dropped by cooldown, and never listed as excluded). Accepted consequence: a title first shown 40 days ago, kept for weeks and dropped yesterday is not in cooldown (its first sighting is older than N days) and may return at once.
7. **Starvation is soft:** if exclusions would leave zero new candidates for a person, that person's exclusions are skipped for the run and a `row.exclusions_skipped` audit event is written (by the persistence layer, from a report field — engine code cannot write events). Fewer-but-nonzero is delivered as is.
8. **Recipe parts** (non-default only): `share=<x>`, `cooldown=<n>`, `avoid=<slug,slug>` (sorted). Changing one rebuilds that row next run — the owner changed something, so that is intended. Explore needs no recipe part: the person's theme enters through the existing `theme=<slug>#<hash>` part, so a promotion rebuilds exactly when it should.
9. **`avoid_rows` ordering ruling.** Avoid reads the OTHER rows' latest stored picks (`PickHistory.latest`, real runs only) plus this run's picks for rows already built (`ctx.built_this_run`). Which of two avoiding rows builds first within a run can therefore change what each avoids; this is accepted, and the engine must NOT reorder rows to make explore rows build in a new order.
10. **"Up next" editing** reuses phase 3's `diff_themes`/`ThemeDraft` editing flow and `PUT /api/themes/{id}` (with `collection_id` and `tokens`) to save edits; this phase adds only the routes that read the rotation state and point `next` at a theme.
11. **Save logic is shared, not copied.** Phase 3 left the theme save logic private in `shortlist/server/api/themes.py` (`_write`, `_audit`, `_reject_title_clashes`, `_spend_on`). Task 3 extracts it into `shortlist/server/services/theme_store.py`; the API and the rotation job both call it, so slug dedupe, hashing (`engine/themes.theme_content_hash`), the `ai_paused` check, token charging to `Collection.ai_tokens` and the audit event behave identically on both paths.

## Review Focus

Each line below has its test in the owning task.

1. Cooldown must not evict the picks a refresh night keeps (Task 2: `test_cooldown_spares_kept_picks`).
2. Exclusions that leave nothing: the row keeps delivering (Task 2: `test_exclusions_skipped_when_they_would_empty_the_row`).
3. Author failure / `ai_paused` / provider `none` mid-rotation: current theme stays, event logged, no history row corrupted (Task 3).
4. Two people rotating independently: person A's promotion never changes person B's theme (Task 2 `for_person` and `test_person_theme_resolved_per_person`, Task 3 rotation).
5. Theme deleted while it is `next` or `current` (`ON DELETE SET NULL`): the row keeps delivering from the last valid theme; rotation re-authors (Task 3).
6. `avoid_rows` naming a row that is deleted, disabled or the row itself: ignored, not an error at run time; rejected on save (Task 3).
7. Upgrade with all controls default: recipe byte-identical, no rebuild (Task 2 pin).
8. Dry runs never count toward cooldown; carried-forward picks never reset a title's first-shown date (Task 3 history reader).
9. **Person theme TMDB cost.** Each distinct person theme is read from TMDB once per run (`load_theme`). Bound: at most one theme per person per row, each slug loaded once, and at most `_MAX_PERSON_THEMES = 100` distinct person themes per run; any beyond the cap are recorded in `ctx.theme_failures` (their rows keep their prior picks) with one warning. A theme is read only for rows that build tonight (`should_build`) (Task 2: `test_person_themes_beyond_the_cap_keep_prior_picks`).
10. **A rotation failure keeps the current theme.** An authoring, TMDB, save or token-charge failure leaves the person's `current` untouched, writes no half-saved `Theme` or `ThemeHistory` row (one transaction per target), and logs one error event; the next daily pass retries (Task 3: `test_failed_write_keeps_current_theme`).

---

### Task 1: Migration 0099 + models

**Files:**
- Create: `shortlist/server/db/alembic/versions/0099_explore_over_time.py`
- Modify: `shortlist/server/db/models.py` (`Collection` near `theme_id` ~:339; new `ThemeHistory` class beside `Theme` ~:348)
- Test: `tests/unit/test_migration_0099.py` (model on the class-based `tests/unit/test_migration_0098.py`)

**Interfaces:**
- Consumes: revision `0098` (down_revision), `themes.id`, `collections.id`, `users.id`.
- Produces — new `Collection` columns, all nullable or defaulted so upgrade changes nothing:
  - `theme_mode: str` (`"fixed"` default, server_default `'fixed'`)
  - `explore_brief: str` (`""` default, server_default `''`)
  - `theme_days: int | None` (NULL = 7)
  - `refresh_share: float | None`
  - `repeat_cooldown_days: int | None`
  - `avoid_rows: list[str] | None` (JSON of collection slugs)
- Produces — `class ThemeHistory(Base)`, table `theme_history`: `id` PK, `collection_id` FK collections.id ON DELETE CASCADE NOT NULL (indexed), `user_id` FK users.id ON DELETE CASCADE NOT NULL, `theme_id` FK themes.id ON DELETE SET NULL NULL, `theme_name: str` (copied at write time so "avoid the last 6" survives a deleted theme), `state: str`, `started_at: datetime`, `due_at: datetime | None`. Index `(collection_id, user_id, state)`.

- [ ] **Step 1: Write the failing migration test** in the class-based style of `test_migration_0098.py`: `run_migrations(tmp_path)` from `shortlist.server.db.session`, `command.upgrade(_alembic(tmp_path), "0098")` to seed an old database, PRAGMA reads through `closing(sqlite3.connect(tmp_path / "shortlist.db"))`.

```python
"""0099 adds the explore / over-time columns on `collections` and the `theme_history` table (#138 phase 4)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command

from shortlist.server.db.session import run_migrations
from tests.unit.test_migrations import _alembic

_NEW = frozenset({"theme_mode", "explore_brief", "theme_days", "refresh_share", "repeat_cooldown_days", "avoid_rows"})


def _collection_columns(config_dir: Path) -> dict[str, tuple[bool, str | None]]:
    with closing(sqlite3.connect(config_dir / "shortlist.db")) as con:
        return {r[1]: (bool(r[3]), r[4]) for r in con.execute("PRAGMA table_info(collections)")}


class TestExploreOverTime0099:
    def test_upgrade_adds_the_columns_with_off_defaults(self, tmp_path: Path):
        run_migrations(tmp_path)
        columns = _collection_columns(tmp_path)
        assert set(columns) >= _NEW
        assert columns["theme_mode"][1] == "'fixed'"
        assert columns["explore_brief"][1] == "''"
        for nullable in ("theme_days", "refresh_share", "repeat_cooldown_days", "avoid_rows"):
            assert columns[nullable] == (False, None)

    def test_existing_collection_keeps_today_s_behaviour(self, tmp_path: Path):
        command.upgrade(_alembic(tmp_path), "0098")
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            before = con.execute("SELECT id, slug, name FROM collections").fetchall()
        assert before

        run_migrations(tmp_path)

        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            after = con.execute(
                "SELECT id, slug, name, theme_mode, explore_brief, theme_days, refresh_share,"
                " repeat_cooldown_days, avoid_rows FROM collections"
            ).fetchall()
            assert con.execute("SELECT COUNT(*) FROM theme_history").fetchone()[0] == 0
        assert [row[:3] for row in after] == before
        assert all(row[3:] == ("fixed", "", None, None, None, None) for row in after)

    def test_theme_history_table_shape(self, tmp_path: Path):
        run_migrations(tmp_path)
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            columns = {r[1]: bool(r[3]) for r in con.execute("PRAGMA table_info(theme_history)")}
        assert columns >= {"id": False, "collection_id": True, "user_id": True, "state": True, "started_at": True}
        assert {"theme_id", "theme_name", "due_at"} <= set(columns)

    def test_downgrade_drops_them_and_upgrade_is_repeatable(self, tmp_path: Path):
        run_migrations(tmp_path)
        command.downgrade(_alembic(tmp_path), "0098")
        assert not (set(_collection_columns(tmp_path)) & _NEW)
        run_migrations(tmp_path)
        assert set(_collection_columns(tmp_path)) >= _NEW
```

- [ ] **Step 2: Run it, expect FAIL** — `pytest tests/unit/test_migration_0099.py -q` (no revision 0099).
- [ ] **Step 3: Write the migration.** Idempotent like the repo's other migrations: read the existing columns once and guard every add, so a database that already has some of them is not an error.

```python
def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("collections")}
    with op.batch_alter_table("collections") as batch:
        if "theme_mode" not in existing:
            batch.add_column(sa.Column("theme_mode", sa.String(16), nullable=False, server_default="fixed"))
        if "explore_brief" not in existing:
            batch.add_column(sa.Column("explore_brief", sa.String(500), nullable=False, server_default=""))
        if "theme_days" not in existing:
            batch.add_column(sa.Column("theme_days", sa.Integer(), nullable=True))
        if "refresh_share" not in existing:
            batch.add_column(sa.Column("refresh_share", sa.Float(), nullable=True))
        if "repeat_cooldown_days" not in existing:
            batch.add_column(sa.Column("repeat_cooldown_days", sa.Integer(), nullable=True))
        if "avoid_rows" not in existing:
            batch.add_column(sa.Column("avoid_rows", sa.JSON(), nullable=True))
    if "theme_history" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "theme_history",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "collection_id", sa.Integer(), sa.ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("theme_id", sa.Integer(), sa.ForeignKey("themes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("theme_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("due_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_theme_history_target", "theme_history", ["collection_id", "user_id", "state"])
```

`downgrade` drops the index and table, then the six columns through `batch_alter_table`. Add the same columns and `ThemeHistory` to `models.py`; `ThemeHistory.state` carries a one-line comment `# current | next | past — a closed set the DB does not enforce`. Match the existing `0098` file for `revision`/`down_revision` header style.
- [ ] **Step 4: Run it, expect PASS.** Also run `pytest tests/unit/test_migration_0098.py -q` (chain still intact).
- [ ] **Step 5: Commit** — stage the three files; `feat(db): explore mode and over-time controls columns (#138)`.

---

### Task 2: Engine — over-time controls, per-person theme, recipe, theme loading

**Files:**
- Create: `shortlist/engine/over_time.py`
- Modify: `shortlist/engine/models.py` (new `OverTime` dataclass; `RowSpec` (non-frozen dataclass, `:479`) gains `over_time`/`person_themes`/`for_person`, beside `theme: ThemeSpec | None` ~`:666`; `UserRunReport` (`:1502`) gains `exclusions_skipped`), `shortlist/engine/context.py` (`EngineContext`, `:38`: add `pick_history` and `built_this_run`), `shortlist/engine/rows.py` (`_KEEP_FRACTION` constant `:617`; `row_recipe` `:984`–`1063`; `_build_section_picks` `:2938`, keep step at `:3259`; the per-user spec list at `:3592`), `shortlist/engine/pipeline.py` (`_load_theme_titles` `:451`)
- Test: `tests/unit/test_over_time.py` (new), `tests/unit/test_themes_rows.py` (row-build cases, recipe pin, `for_person`, person-theme loading — its fixtures `theme_row`/`theme_spec` and the `ctx` pattern live there, NOT in `test_pipeline.py`)

(`RowSpec` has no `ai_row` field — phase 3 removed it; an AI row is `spec.theme is not None`.)

**Interfaces:**
- Consumes: `RowSpec`, `ThemeSpec` (`shortlist/engine/themes.py`), `MediaType`, `Candidate`, `_KEEP_FRACTION`, `_build_section_picks(policy, spec, targets, k, *, cold, base_cold, pool_for_row, taste)` internals (`prior_valid`, `k`, the candidate list `sub`).
- Produces. `OverTime` lives in `engine/models.py` (so `RowSpec` can hold it without a circular import); `over_time.py` imports it:

```python
# engine/models.py
TitleKey = tuple[MediaType, int]  # (media, tmdb_id)


@dataclass(frozen=True)
class OverTime:
    refresh_share: float | None = None
    repeat_cooldown_days: int | None = None
    avoid_rows: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.refresh_share is not None or self.repeat_cooldown_days is not None or bool(self.avoid_rows)

    def fingerprint(self) -> str:
        """Set parts only, fixed order, avoid sorted: "share=0.5;cooldown=30;avoid=a,b"."""
        parts = []
        if self.refresh_share is not None:
            parts.append(f"share={self.refresh_share:g}")
        if self.repeat_cooldown_days is not None:
            parts.append(f"cooldown={self.repeat_cooldown_days}")
        if self.avoid_rows:
            parts.append(f"avoid={','.join(sorted(self.avoid_rows))}")
        return ";".join(parts)

    def keep_fraction(self) -> float:
        """The share of a row kept on a refresh night; the pre-existing two-thirds when unset."""
        return _KEEP_FRACTION if self.refresh_share is None else 1 - self.refresh_share
```

`_KEEP_FRACTION` moves to `engine/models.py` and `rows.py` imports it (one definition; update the `rows.py:617` comment's pointer accordingly).

```python
# engine/over_time.py
class PickHistory(Protocol):
    def first_shown_since(self, user_slug: str, row_slug: str, since: date) -> set[TitleKey]:
        """Titles whose FIRST real-run appearance for (user, row) is on or after ``since``."""
    def latest(self, user_slug: str, row_slug: str) -> set[TitleKey]:
        """The picks of the newest real run that wrote (user, row); empty when there is none."""

@dataclass(frozen=True)
class ExclusionResult:
    kept: list[Candidate]
    dropped: int
    skipped: bool                          # True when exclusions were ignored to avoid emptying the row

def excluded_titles(over_time: OverTime, *, user_slug: str, row_slug: str, today: date,
                    history: PickHistory | None, built_this_run: Mapping[tuple[str, str], set[TitleKey]],
                    spare: set[TitleKey]) -> set[TitleKey]:
    """Cooldown titles (``history.first_shown_since``) plus avoid-rows titles, minus ``spare`` (the titles kept in this row)."""
def apply_exclusions(candidates: list[Candidate], excluded: set[TitleKey]) -> ExclusionResult
```

- `RowSpec.over_time: OverTime = field(default_factory=OverTime)`; `RowSpec.person_themes: tuple[tuple[str, ThemeSpec], ...] = ()`; `RowSpec.for_person(user_slug: str) -> RowSpec` returns `dataclasses.replace(self, theme=<that person's ThemeSpec>)` when present, else `self`.
- `UserRunReport.exclusions_skipped: list[str]` — row slugs whose exclusions were skipped for that person tonight (persisted as events in Task 3).
- `EngineContext` (`engine/context.py:38`): `pick_history: PickHistory | None = None` and `built_this_run: dict[tuple[str, str], set[TitleKey]] = field(default_factory=dict)` (key `(user_slug, row_slug)`, filled after a person's picks settle). Both are constructed in `shortlist/server/services/context_builder.py` at two sites (`:493` and `:595`) — Task 3 adds the new fields to BOTH.

- [ ] **Step 1: Capture today's recipe BEFORE editing.** In a scratch script, call `row_recipe(policy, spec)` for the two or three specs `test_themes_rows.py` already builds (one plain row, one with `theme`) and paste the literal strings into a new pin test `test_recipe_is_byte_identical_with_controls_off` in that file. Run it now: PASS.
- [ ] **Step 2: Write failing unit tests in `tests/unit/test_over_time.py`:**
  - `OverTime().active is False`; fingerprint empty; `keep_fraction() == 2/3`.
  - `OverTime(refresh_share=0.5).keep_fraction() == 0.5`; `refresh_share=1.0` → 0.0.
  - fingerprint order/sorting: `OverTime(0.5, 30, ("b","a")).fingerprint() == "share=0.5;cooldown=30;avoid=a,b"`.
  - `excluded_titles`: cooldown reads `history.first_shown_since(user, row, today - timedelta(days=n))` (assert the args the fake history received); `avoid_rows` prefers `built_this_run[(user, avoid_slug)]`, falls back to `history.latest(user, avoid_slug)`; an `avoid_rows` entry equal to `row_slug` is ignored; `history=None` → only `built_this_run` titles; titles in `spare` never appear in the result (`test_cooldown_spares_kept_picks_in_excluded_titles`).
  - `apply_exclusions` drops matching keys, counts them; `skipped=True` and the ORIGINAL list returned when dropping would leave nothing (`test_exclusions_skipped_when_they_would_empty_the_row`); empty `excluded` returns the same list object.
  - hypothesis: for any candidate list and any excluded set, `kept` is an order-preserving subsequence and `len(kept)+dropped == len(candidates)` unless `skipped`.
- [ ] **Step 3: Write failing tests in `tests/unit/test_themes_rows.py`** (same file as the recipe pin):
  - `for_person`: person A's theme returned for A, base theme for B and for an unknown slug, and `for_person` leaves `RowSpec` otherwise equal (`dataclasses.replace` check on every other field).
  - Recipe: each single control set changes the recipe (`share=`, `cooldown=`, `avoid=` appear); clearing it restores the pinned string exactly.
  - Row build, with a fake `pick_history` recording its args: `test_refresh_share_changes_how_many_picks_swap` (share 0.5 with k=10 keeps 5; assert the kept titles equal the strongest five by rank), `test_cooldown_drops_recent_titles_from_new_picks`, `test_cooldown_spares_kept_picks` (a pick kept tonight whose title the history reports as first shown yesterday is still in the row), `test_avoid_rows_drops_titles_from_the_named_row`, `test_exclusions_skipped_event_field_is_set` (`user_report.exclusions_skipped == [row_slug]` when starved), `test_person_theme_resolved_per_person` (two people, two themes, assert each pool read `ctx.theme_titles[<that person's slug>]`).
  - Theme loading (`_load_theme_titles`): `test_person_theme_is_loaded` (a row whose only theme for A is in `person_themes` gets `ctx.theme_titles[slug]` and the loader is called once per distinct slug even when two people share it); `test_failing_person_theme_keeps_prior_picks` (loader raises for A's theme: `ctx.theme_failures[slug]` is set, A's row delivers its prior picks unchanged, B's row builds normally); `test_person_themes_beyond_the_cap_keep_prior_picks` (monkeypatch `_MAX_PERSON_THEMES` to 2 with 3 distinct themes: the third is in `theme_failures`, one warning).
- [ ] **Step 4: Run `pytest tests/unit/test_over_time.py -q` and `pytest tests/unit/test_themes_rows.py -q`; expect FAIL** (module and fields missing; the pin test PASSES).
- [ ] **Step 5: Implement `over_time.py`** (plain dataclasses and two small functions; no clever abstractions), the `OverTime`/`TitleKey` additions and `RowSpec`/`EngineContext`/`UserRunReport` fields.
- [ ] **Step 6: Recipe.** In `row_recipe`, next to the theme part (`rows.py:1063`), add a `_RECIPE_OVER_TIME` constant beside `_RECIPE_THEME` and:

```python
(*((f"{_RECIPE_OVER_TIME}{spec.over_time.fingerprint()}",) if spec.over_time.active else ()),)
```

- [ ] **Step 7: Resolve the person's theme where the spec enters the per-user loop.** In `rows.py` at the per-user spec list (`:3592`, `specs = [s for s in owned if not _is_muted(...) ...]`), apply `specs = [s.for_person(user.slug) for s in specs]` immediately after it, so everything downstream sees the person's theme: `row_recipe`, `RowPolicy.pool_key` (`:2501`), `pools_for` (`:2564`) and the theme reads (`:1898`, `:2443`, `:2676`), all of which do `ctx.theme_titles.get(spec.theme.slug)`. Not only inside `_build_section_picks`: `pools_for` and `pool_key` run before it. `rows_considered` and `due` use the same specs, so the recipe sees the person's theme too.
- [ ] **Step 8: Load person themes.** In `pipeline._load_theme_titles` (`:451`) the wanted set is `{spec.theme.slug: spec.theme for spec in ctx.config.rows if spec.theme and ctx.config.should_build(spec)}` and never sees `RowSpec.person_themes`. Replace it with:

```python
_MAX_PERSON_THEMES = 100  # distinct person themes read from TMDB in one run (Review Focus 9)

wanted: dict[str, ThemeSpec] = {}
person_only: dict[str, ThemeSpec] = {}
for spec in ctx.config.rows:
    if not ctx.config.should_build(spec):
        continue
    if spec.theme:
        wanted[spec.theme.slug] = spec.theme
    for _, person_theme in spec.person_themes:
        person_only.setdefault(person_theme.slug, person_theme)
for slug, theme in person_only.items():
    if slug in wanted:
        continue
    if len(wanted) - len({s.theme.slug for s in ctx.config.rows if s.theme}) >= _MAX_PERSON_THEMES:
        ctx.theme_failures[slug] = "too many themes to read in one run"
        logger.warning("person theme {} not read: over the per-run cap of {}", theme.name, _MAX_PERSON_THEMES)
        continue
    wanted[slug] = theme
```

Keep the existing per-theme `for slug, theme in wanted.items(): try/except` body unchanged, so a failing person theme sets `ctx.theme_failures[slug]` exactly as a row theme does (the row's own failure path at `rows.py:2441` then keeps the prior picks). Row-level themes always load; only person themes count against the cap. Compute the row-level slug set once above the loop rather than inside it.
- [ ] **Step 9: Wire `_build_section_picks`.** Read `rows.py` `:3140`–`:3262` and the code that fills new picks after the keep step first. Then: (a) replace `round(_KEEP_FRACTION * k)` at `:3259` with `round(spec.over_time.keep_fraction() * k)`; (b) before ranking NEW candidates call `excluded_titles(spec.over_time, user_slug=user.slug, row_slug=spec.slug, today=ctx.today, history=ctx.pick_history, built_this_run=ctx.built_this_run, spare={(p.media, p.tmdb_id) for p in kept})` then `apply_exclusions(...)` on the new-candidate list only, never touching `prior_valid`/`kept`; (c) on `skipped`, `user_report.exclusions_skipped.append(spec.slug)` (the engine cannot write events; Task 3 persists it); (d) after picks settle, `ctx.built_this_run[(user.slug, spec.slug)] = {(p.media, p.tmdb_id) for p in picks}`. Use the real names the function already has for the report and the clock (`ctx.today` is a placeholder — use whatever the function uses to get "today"; read it first). Do not change the order rows build in (Decision 9).
- [ ] **Step 10: Run `pytest tests/unit/test_over_time.py tests/unit/test_themes_rows.py -q`. Expect PASS.** `ruff check` + `ruff format` on touched files.
- [ ] **Step 11: Commit** — `feat(engine): refresh share, no-repeat and keep-out controls; per-person themes (#138)`.

---

### Task 3: Server — theme store, history reader, context wiring, rotation job, API

**Files:**
- Create: `shortlist/server/services/theme_store.py`, `shortlist/server/services/theme_rotation.py`, `shortlist/server/services/pick_history.py`
- Modify: `shortlist/server/api/themes.py` (call the new `theme_store` functions; `_write` `:381`, `_audit` `:413`, `_reject_title_clashes` `:428`, `_spend_on` `:350` move out), `shortlist/server/services/context_builder.py` (`_row_specs` ~`:1223`–`:1296`, `_theme_spec` `:1306`, BOTH `EngineContext(` sites `:493` and `:595`, new `profile_with_history`), `shortlist/server/services/run_persistence.py` (new `audit_exclusions_skipped`, called beside `audit_sweep` at `:1038`), `shortlist/server/services/jobs.py` (new `@handler("themes.rotate")` next to `maintenance.prune` `:1364`), `shortlist/server/scheduler.py` (`DEFAULT_CRONS` `:54`–`:75`, `MAINTENANCE_PRUNE_JOB_ID` `:33`, new `THEMES_ROTATE_JOB_ID` and `_register_theme_rotation()` modelled on `_register_maintenance_prune` `:401`, call added in `build_scheduler` `:503`–`:510`), `shortlist/server/api/collections.py` (`CollectionIn` `:164`, `CollectionOut` `:428` / response fields ~`:547`, `_serialize`/`_view` `:1133`, `_validate_theme` `:1146`, the PATCH path `:1724` and its field tuple `:1536`–`:1552`, plus the new rotation routes)
- Test: `tests/unit/test_theme_store.py`, `tests/unit/test_pick_history.py`, `tests/unit/test_theme_rotation.py`, `tests/unit/test_api_themes.py` (must stay green after the extraction), `tests/unit/test_api_collections.py` (extend, includes the rotation routes), `tests/unit/test_scheduler.py` (registration), `tests/unit/test_run_persistence.py` (events), `tests/unit/test_themes_rows.py` (context builder wiring)

**Interfaces:**
- Consumes: Task 1 columns/`ThemeHistory`; Task 2 `OverTime`, `PickHistory`, `RowSpec.over_time/person_themes`, `UserRunReport.exclusions_skipped`; phase 3 `author_theme(*, brief, media, curator, tmdb, plex, library_index, profile=None, current=None, guidance="", current_tag_names=None, change="") -> ThemeDraft` (`shortlist/server/services/theme_author.py:114`), `spec_from_row`, `theme_content_hash`, `ThemeSaveIn`, `diff_themes`, `jobs.enqueue(sessions, kind, payload=None)`, `add_audit(session, scope, level, **message)`.
- Produces:

```python
# theme_store.py — the logic extracted from api/themes.py; raises plain exceptions, the API maps them to HTTP
class ThemeStoreError(Exception): ...
class RowPaused(ThemeStoreError): ...          # API: 409 with the existing _PAUSED text
class TitleClash(ThemeStoreError): ...          # API: 422

def write_theme(row: Theme, body: ThemeSaveIn) -> None            # was _write
def audit_theme_build(session: Session, row: Theme, body: ThemeSaveIn, *, diff: ThemeDiff | None) -> None   # was _audit
def reject_title_clashes(session: Session, secrets, theme: Theme) -> None   # was _reject_title_clashes
def charge_tokens(session: Session, body: ThemeSaveIn) -> Collection | None  # was _spend_on: ai_paused + tokens > 0 -> RowPaused
def save_theme(session: Session, secrets, body: ThemeSaveIn, *, existing: Theme | None = None,
               diff: ThemeDiff | None = None) -> Theme
    # new row or update: unique slug, write_theme, reject_title_clashes, charge_tokens
    # (Collection.ai_tokens += body.tokens), audit_theme_build; caller owns the commit

# pick_history.py
class DbPickHistory:                       # implements engine PickHistory
    def __init__(self, session: Session) -> None: ...
    def first_shown_since(self, user_slug: str, row_slug: str, since: date) -> set[TitleKey]: ...
    def latest(self, user_slug: str, row_slug: str) -> set[TitleKey]: ...

# theme_rotation.py
NEXT_LEAD_DAYS = 1   # the next theme is built this many days before it starts
DEFAULT_THEME_DAYS = 7

@dataclass(frozen=True)
class RotationOutcome:
    collection_id: int
    user_id: int
    action: Literal["authored_current", "authored_next", "promoted", "kept", "skipped_paused", "failed"]

def rotate_themes(sessions, *, now: datetime, author=author_theme, curator, tmdb, plex,
                  profile_for) -> list[RotationOutcome]
def recent_theme_names(session: Session, collection_id: int, user_id: int, limit: int = 6) -> list[str]
def promote_next(session: Session, collection_id: int, user_id: int, now: datetime) -> None
def theme_guidance(instructions: AiInstructions | None) -> str
```

- Where each `author_theme` input comes from (rotation job and `up-next/regenerate` both use these):
  - `media`: the row's theme's own media, `spec_from_row(<the row's base theme>).media` (what `_theme_spec` hands the engine for an AI row); falls back to the row's `collection.media` mapped as the themes API maps `body.media`.
  - `library_index`: `shortlist.server.services.library_index.library_index(plex, sessions, media=collection.media or "both", library_keys=collection.library_keys or [])` — the same function the themes preview route calls (`themes.py:229`) and that `row_sections`/`delivery.target_sections` resolve for a run.
  - `profile`: the person's watch history exactly as a run fills it — `ContextBuilder._profile(user, overrides)` for the identity, then `profile.history = ShareTokenWatchSource(plex, plextv, owner_token=plex_token).fetch(profile, min_completion=<the run config's min_completion>)` (`rows.py:3632`; `author_theme`'s `_user_message` feeds `taste_summary(profile, 20)`). Wrapped in one new `ContextBuilder.profile_with_history(session, user_id)` modelled on `user_history` (`context_builder.py:658`–`:690`), which `profile_for` calls.
  - `brief`: `collection.explore_brief` (or, when blank, "Choose a theme this person would love from what they watch.") followed by the line `Avoid these recent theme names: <names from recent_theme_names>.` when there are any.
  - `guidance`: `theme_guidance(AiInstructions.from_stored(collection.prompt))` — the server-side twin of the web's `themeGuidance` (`web/src/lib/themes.ts:135`): mode `own` → the text; `add` → `f"{BUILD_SYSTEM_GUIDANCE.strip()} {text}"`; otherwise `""` so `author_theme` falls back to `BUILD_SYSTEM_GUIDANCE`.
  - `curator`, `tmdb`, `plex`: built by the `themes.rotate` handler the way the themes API builds them (`make_curator(...)` from settings, `state.run_service.build_tmdb_only()`, `_plex(state)` equivalent); `tmdb` None or curator `none` → every target logs a skipped/failed outcome and nothing changes.
- `ContextBuilder._row_specs` additionally fills `over_time=OverTime(refresh_share, repeat_cooldown_days, tuple(avoid_rows or ()))`, and for an explore AI row `person_themes=((user_slug, spec_from_row(theme)), …)` from each person's `state="current"` history row (users in the row's audience, resolved the way `_row_specs` already resolves audience). Both `EngineContext(` constructions get `pick_history=DbPickHistory(session)`; the session must outlive the run exactly as the other per-run readers do — read how `index_cache=DbCache(self._sessions, …)` (`:508`) avoids holding one, and give `DbPickHistory` the sessions factory instead if holding a session is not safe on a worker thread.
- API: `CollectionIn` and the response (`CollectionOut` and `_serialize`/`_view`) gain `theme_mode: Literal["fixed","explore"] = "fixed"`, `explore_brief: str = Field("", max_length=500)`, `theme_days: int | None = Field(None, ge=1, le=90)`, `refresh_share: float | None = Field(None, gt=0, le=1)`, `repeat_cooldown_days: int | None = Field(None, ge=1, le=365)`, `avoid_rows: list[str] | None`. The PATCH path (`:1724`) judges the MERGED row: add the six names to the field tuple at `:1536`–`:1552` so a PATCH writes them, and validate on the merged `theme_id` like `_validate_theme` does. Validation: `theme_mode="explore"` and every over-time control require `theme_id is not None` (422 otherwise); `avoid_rows` slugs must exist, be per-person rows, and not be the row itself (422 naming the slug); clearing `theme_id` (PATCH sends null) resets `theme_mode` to `"fixed"` and nulls `explore_brief`, `theme_days`, `refresh_share`, `repeat_cooldown_days`, `avoid_rows`.
- Routes live in `shortlist/server/api/collections.py` (the themes router has prefix `/themes`; these are `/api/collections/{id}/…`):
  - `GET /api/collections/{id}/theme-rotation` → `{mode, days, targets: [{user_id, name, current: ThemeRef|null, next: ThemeRef|null, started_at, next_due_at, history: [ThemeRef]}]}` (one target per person in the row's audience; 404 when the row is not an AI row).
  - `PUT /api/collections/{id}/up-next {user_id: int, theme_id: int}` — points `next` at a saved theme, replacing any existing next (404 if the theme or person is missing).
  - `POST /api/collections/{id}/up-next/regenerate {user_id: int}` — authors a new next now (409 `ai_paused`, 422 provider none or `theme_mode != "explore"`; returns the new `ThemeRef`).

- [ ] **Step 1: Extract the theme store, behaviour unchanged.** Move the logic out of `api/themes.py` into `services/theme_store.py` per the interface above (`_write` → `write_theme`, `_audit` → `audit_theme_build`, `_reject_title_clashes` → `reject_title_clashes`, `_spend_on` → `charge_tokens`, and a `save_theme` that composes them with the slug-dedupe the create route already does). The routes call these and translate `RowPaused` to the existing 409 and `TitleClash` to 422 with the same texts. Run `pytest tests/unit/test_api_themes.py -q` BEFORE writing any new code: it must stay green with zero edits to that file. Then add `tests/unit/test_theme_store.py` with: `test_paused_row_refuses_a_save_that_spent_tokens` (and a 0-token save passes), `test_tokens_are_added_to_the_collection`, `test_slug_collision_gets_a_unique_slug`, `test_content_hash_comes_from_the_columns`. Run, expect PASS.
- [ ] **Step 2: Failing tests, history reader** (`test_pick_history.py`; build `Run`/`RunUser`/`PickRow` rows directly, `PickRow.user_id` is a `users.id` so the fixture creates `User(slug=…)` and the reader maps `user_slug` → `user_id` itself, returning empty for an unknown slug; `PickRow.collection_slug` is the ROW slug):
  - `test_dry_runs_never_count_toward_cooldown`: a pick whose run has `dry_run=True` is invisible to both methods.
  - `test_pick_without_a_run_never_counts`: `run_id IS NULL`.
  - `test_first_shown_is_the_earliest_appearance`: the same title restamped by three real runs (days 40, 10, 1 ago) is NOT in `first_shown_since(today - 30d)` (first sighting is 40 days ago) but IS in `first_shown_since(today - 60d)`; a title first appearing 3 days ago IS in the 30-day set.
  - `test_carried_forward_picks_do_not_reset_first_shown` (the restamp case above, named for the Review Focus).
  - Scoping: another person's or another row's picks are not returned. `latest` returns the picks of the newest REAL run that wrote that (user, row) only, as `(MediaType, tmdb_id)` keys (`PickRow.media_type` is a string).
  Run, expect FAIL; implement `DbPickHistory`; expect PASS.
- [ ] **Step 3: Failing tests, rotation** (`test_theme_rotation.py`, author injected as a fake that records kwargs; `profile_for` a fake returning a `UserProfile`):
  - no current for a person → authors and inserts `state="current"` (and does not touch `Collection.theme_id`);
  - current younger than `theme_days − NEXT_LEAD_DAYS` → `kept`, author not called;
  - inside the last day and no next → authors `next`; assert the `brief` contains `explore_brief` and the names from `recent_theme_names` (last 6, newest first), that `profile=` is that person's profile, that `guidance=` equals `theme_guidance(...)` for the row's `prompt`, and that `media`/`library_index` are the ones described above;
  - `now >= started_at + theme_days` with a next → `promoted` (`promote_next`): old current becomes `past`, next becomes `current`, `started_at == now`; with no next at that point it authors `current` directly, not a gap;
  - promotion happens in the job when due, and the engine reads whichever `current` the DB holds: a context-builder test (below) builds specs twice, before and after `promote_next`, and sees the theme change with no clock involved;
  - author raises `ThemeAuthorError` → `failed`, current untouched, one `audit` error event, no new history row and no saved `Theme` (`test_failed_write_keeps_current_theme`);
  - `ai_paused` → `skipped_paused`, author never called, current kept, one `audit` event (the check lives in `theme_store.charge_tokens`/`RowPaused`, not only in the loop, so `regenerate` and rotation share it);
  - tokens: a successful authoring adds `draft.tokens` to that row's `Collection.ai_tokens` through `theme_store.save_theme`;
  - current's `theme_id` NULL (theme deleted) → treated as no current, re-authored;
  - two people: promoting one leaves the other's `ThemeHistory` rows identical;
  - `theme_days=None` behaves as 7; `theme_mode="fixed"` collections and rows with `theme_id` NULL are never touched; a disabled row is skipped.
- [ ] **Step 4: Implement `theme_rotation.py`** — a loop over enabled, explore, non-paused AI rows × the people in each row's audience; one function per decision (`_due_to_promote`, `_due_to_author_next`); one transaction per (row, person) so a failure rolls back only that target. The authored `ThemeDraft` is converted to a `ThemeSaveIn` (origin `"ai"`, `collection_id=row.id`, `tokens=draft.tokens`, `brief`) and saved ONLY through `theme_store.save_theme` — no hashing or slug logic here. Register `@handler("themes.rotate")` in `services/jobs.py` next to `maintenance.prune` (`:1364`); it builds the curator/tmdb/plex/`profile_for` and calls `rotate_themes(state.sessions, now=<utc now>, …)`. Scheduler: add `"themes.rotate_cron": "30 1 * * *"` to `DEFAULT_CRONS` with a comment saying only that rows' crons are independent and the job's clock is not load-bearing (no other deployment's times); `THEMES_ROTATE_JOB_ID = "themes-rotate"`; `_register_theme_rotation(scheduler, app)` copying `_register_maintenance_prune` (`_resolve_cron(app, "themes.rotate_cron", DEFAULT_CRONS["themes.rotate_cron"], blank_means_off=True)`; `fire` calls `_queue_and_drain(app, "themes.rotate")`), and add it to `build_scheduler` after `_register_maintenance_prune`. Also add the cron key wherever `maintenance.prune_cron` is exposed as a setting (grep for it in `settings.py`), so the owner can move or turn it off.
- [ ] **Step 5: Run `pytest tests/unit/test_theme_rotation.py tests/unit/test_scheduler.py -q`, expect PASS.** (`test_scheduler.py` gets a registration test following the `maintenance.prune` one: the job id exists with the default cron, a custom cron is honoured, a blank value registers nothing.)
- [ ] **Step 6: Failing tests, wiring and API.**
  - `test_api_collections.py`: each control round-trips through POST, GET and PATCH (a PATCH that sends only `refresh_share` leaves the others, proving the merge path); defaults on create equal today's (`fixed`, `""`, NULLs); `theme_mode="explore"` or any control on a row with no `theme_id` → 422; `avoid_rows` unknown / self / shared-row slug → 422; `refresh_share=0` → 422; PATCH `theme_id: null` on an explore row resets `theme_mode` to `fixed` and clears the controls. Rotation routes: `theme-rotation` payload shape (one target per audience person, `404` on a non-AI row); `up-next` replaces an existing next and returns 404 for a missing theme; `regenerate` returns 409 when paused, 422 when no provider, and passes the recent-names brief to the author fake.
  - `test_themes_rows.py` (context builder): an explore row yields `person_themes` for each person's own current theme and a different theme per person; a person with no `current` falls back to the row's theme; `over_time` is default when columns are NULL; both `EngineContext` construction sites carry `pick_history`.
  - `test_run_persistence.py`: a `UserRunReport` with `exclusions_skipped=["quiet-nights"]` produces one `row.exclusions_skipped` event with the run id, user and row slug (dry-run flag carried), none when the list is empty.
- [ ] **Step 7: Implement** the `CollectionIn`/response/PATCH fields and validation, `_row_specs`/both `EngineContext` constructions, the three routes, and `audit_exclusions_skipped(session, report, *, dry_run, run_id)` in `run_persistence.py` (same shape as `audit_sweep` `:1451`: returns early when nothing to add, one `_add_event(session, "row.exclusions_skipped", "info", run_id, …)` per person×row), called next to `audit_sweep` at `:1038`. Then regenerate the OpenAPI snapshot and types with the command in `tests/unit/test_openapi_snapshot.py` (updates `web/openapi.snapshot.json` and `web/src/lib/api-schema.d.ts`).
- [ ] **Step 8: Run `pytest tests/unit/test_theme_store.py tests/unit/test_pick_history.py tests/unit/test_api_themes.py tests/unit/test_api_collections.py tests/unit/test_run_persistence.py tests/unit/test_openapi_snapshot.py tests/unit/test_themes_rows.py -q` (one invocation), expect PASS.** `ruff check . --fix && ruff format .` on touched files.
- [ ] **Step 9: Commit** — `feat(server): explore rotation job, up-next API and over-time controls wiring (#138)`.

---

### Task 4: UI — all of it

**Files:**
- Create: `web/src/components/rows/over-time-fields.tsx`, `web/src/components/rows/explore-section.tsx`
- Modify: `web/src/components/rows/ai-row-section.tsx` (mount `ExploreSection` and `OverTimeFields`; the over-time fields are for AI rows ONLY — the spec exposes avoid-rows/cooldown/share only on themed rows in v1 — so they mount here, not in the general contents/limits fields used by every row kind), `web/src/lib/collections.ts` (form state ↔ API), `web/src/lib/themes.ts` (`useThemeRotation`, `useSetUpNext`, `useRegenerateUpNext`; the routes are under `/api/collections/{id}/…`), `web/src/lib/api.ts`, `web/src/lib/types.ts`
- Touch `web/src/components/rows/ai-try-it.tsx`, `ai-hand-edit.tsx`, `ai-prompts-section.tsx` only if a failing test or `tsc -b` shows they must change (e.g. a new required prop); do not edit them otherwise.
- Test: `web/src/test/over-time-fields.test.tsx`, `web/src/test/explore-section.test.tsx`, `web/src/test/collections-over-time.test.ts`

**Interfaces:**
- Consumes: regenerated `api-schema.d.ts` types (never hand-write request/response types); phase 3 `ai-row-section.tsx` props and hooks in `lib/themes.ts`, including `diff_themes` output and the `ThemeDraft` editor used by "Change it".
- Produces: form fields `themeMode`, `exploreBrief`, `themeDays`, `refreshShare`, `repeatCooldownDays`, `avoidRows` mapping 1:1 to the API; unchanged defaults serialise to `fixed`/`""`/`null`s. Un-setting the AI row (no theme) sends the defaults, matching the server reset.

Copy (plain English, controls say exactly what happens):
- "Keep the same theme" / "Pick a new theme every [7] days"; helper: "Shortlist asks your AI provider for a new theme for each person, each time. It uses your AI provider once per person per change."
- Textarea: "What kind of themes? (optional)" with placeholder "Leave blank and each person gets a theme chosen from what they watch."
- Up next card, per person: "Up next: <name> — starts <date>", buttons "Change it" (opens the phase 3 editor on that theme, reusing `diff_themes`/`ThemeDraft`; saves via `PUT /api/themes/{id}` with `collection_id` and `tokens`, then `PUT /api/collections/{id}/up-next`) and "Pick another" (regenerate; disabled with reason when AI is paused). Empty state: "Nothing queued yet. The next theme is built a day before it starts."
- Past themes: "Recent themes" list (last 6 names with dates); one line: "Shortlist won't pick these again soon."
- "How much changes each time": select "A little (about a fifth)" 0.2 · "A third (usual)" null · "Half" 0.5 · "Almost everything" 0.8.
- "Don't repeat a title for [N] days" — switch off by default; when on, number input 1–365.
- "Keep out titles already in:" checkbox list of the person's other per-person rows (empty state: "No other rows to compare with.").

- [ ] **Step 1: Write failing tests.**
  - `collections-over-time.test.ts`: a form loaded from a default collection round-trips to an identical payload (defaults stay `fixed`/null); each control maps to its API field; turning the cooldown switch off sends `null`.
  - `over-time-fields.test.tsx`: the four select options map to the values above; cooldown input hidden until switched on; `avoidRows` lists only per-person rows other than this one; empty state text.
  - `ai-row-section.test.tsx` (extend): over-time fields render for an AI row and are absent for a row with no theme (`isAiRow` false / `theme_id` null).
  - `explore-section.test.tsx`: all four data states for the rotation query (loading skeleton, error + retry, empty text above, success with Up next + Recent themes); "Pick another" disabled with a reason when `ai_paused`; "Change it" calls the theme save then the `up-next` mutation (assert request bodies, including `collection_id` and `tokens` on the theme save); switching to explore shows days + brief.
- [ ] **Step 2: Run `pnpm -C web test -- over-time explore-section collections-over-time ai-row-section`, expect FAIL.**
- [ ] **Step 3: Implement** the components and hooks (function components, TanStack Query, shadcn primitives, Tailwind tokens only, real `<label>`/`<button>`, visible focus). Mount per Files above.
- [ ] **Step 4: Run the four test files again, expect PASS.** Then `pnpm -C web exec tsc -b --force` (CI's check; `--noEmit` misses test files) and `pnpm -C web exec eslint src/components/rows src/lib src/test`.
- [ ] **Step 5: Probe the real UI once** with `bash scripts/devrun.sh` against `tests/fakes/fake_plex.py` at 320 / 1024 / 1280 widths: create an explore AI row, see Up next, change the share and cooldown. Click by coordinate, not by ref (refs silently no-op in this SPA). Port 5960, never 5959.
- [ ] **Step 6: Commit** — `feat(web): explore mode, up next and over-time controls (#138)`.

---

### Task 5: Docs

**Files:**
- Modify: `docs/reference.md` (Collection fields + the three new endpoints + `themes.rotate` job and its `themes.rotate_cron` setting), `docs/guides.md` (new "Explore: a new theme every few days" and "How a row changes over time" sections incl. troubleshooting: "my row stopped changing" → cooldown/avoid starvation event), `README.md` (feature list), `CHANGELOG.md` (Unreleased entry), `.claude/docs/discussion-138-ai-custom-rows.md` (status: phase 4 shipped, the Decision 4 table rows now built), `.claude/docs/shortlist-architecture.md` (schema: `theme_history`, new Collection columns; jobs list: `themes.rotate`)
- Regenerate: `docs/llms-full.txt` via `python scripts/build_llms_full.py` (local python as above)

- [ ] **Step 1: Write the docs** per `.claude/rules/docs.md`. Defaults stated for every control; state that Explore is per person (AI rows are never shared rows), uses the AI provider once per theme per person, that the next theme is built a day early by default, that the rotation job's schedule is a setting, and that nothing changes on upgrade. Explain that "no repeats for N days" counts from the first time a title was shown and never removes a title the row is keeping, and that "keep out titles in other rows" depends on build order within a run. No hostnames, personal paths or one deployment's clock times.
- [ ] **Step 2: Regenerate `docs/llms-full.txt`; run `pytest tests/unit/test_llms_full.py -q`, expect PASS.** If `CHANGELOG.md` gets a dated version heading, Steve decides that, not this plan (releases are his call): leave under Unreleased and do not run `build_feed.py`.
- [ ] **Step 3: Commit** — `docs: explore mode and over-time controls (#138)`.

---

### Task 6: Whole-phase verification (once)

- [ ] **Step 1:** Dispatch `verifier` (haiku), one run at a time, for exactly CI's commands: `pytest` (clear stale `.coverage*` first), `pnpm -C web test`, `pnpm -C web exec tsc -b --force`, `pnpm -C web exec eslint .`, `pnpm -C web build`, `ruff check . && ruff format --check .`, then `pytest -m e2e` after rebuilding `web/dist`. Failures only come back.
- [ ] **Step 2:** Fix failures (a failing test's name says whether the test or the code is wrong; never edit a test to match new behaviour).
- [ ] **Step 3:** Dispatch the Architecture Review agent (migration 0099; reads `Run`/`PickRow` history and maps engine user slugs to user ids; writes row contents via the existing delivery; reads watch history to author themes). Prompt it with the Global Constraints line that no Plex/plex.tv state or share filter is newly written. Block on HIGH findings. Then one `opus` whole-branch review.
- [ ] **Step 4:** Merge `origin/dev`, re-verify the touched suites, and **stop**. Do not push, tag or open a PR to master without Steve.
- [ ] **Step 5: Dry-run proof** (no live write): scoped dry run via the API on the throwaway devrun config — an explore row with `theme_days=1`, run `themes.rotate` twice a simulated day apart (fake clock), confirm one promotion, a new `next`, and `recent_theme_names` length ≤ 6. Report using Cause/Fix/Proof only if something broke.

---

## Self-review

- **Spec coverage:** explore mode + `theme_days` (T1,T3,T4); per-person theme from taste (T2 `for_person`/loading, T3 rotation); "Up next" a day early, editable (T3,T4); history avoid last 6 (T3); refresh share default ⅓ (T2); no-repeat N days off by default from real-run `picks`, first-shown semantics (T2,T3); keep-out rows default none (T2,T3); migration → Architecture Review (T6); byte-identical recipe (T2); docs (T5). Shared explore rows are intentionally out (Global Constraints).
- **Placeholders:** none; where a line number is approximate or a local name is unknown (`ctx.today` in T2 Step 9, the `settings.py` cron exposure in T3 Step 4, the `EngineContext` session lifetime in T3), the step says to read the named function first.
- **Type consistency:** `OverTime` and `TitleKey` (in `engine/models.py`), `PickHistory.first_shown_since`/`latest`, `RowSpec.for_person`, `UserRunReport.exclusions_skipped`, `DbPickHistory`, `theme_store.save_theme`/`charge_tokens`, `rotate_themes`, `recent_theme_names`, `promote_next`, `ThemeHistory` and the API field names are used identically across tasks 1–4.
- **Known unknowns for the executor:** the exact "today" source inside `_build_section_picks`; how the row's audience is resolved for rotation (reuse `_audience_maps`/`_subset_audience` in `context_builder.py`); whether `DbPickHistory` may hold a session during a run.
