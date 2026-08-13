# Per-Row Run Cost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record and display what each ROW cost a person in a run, instead of repeating that
person's whole-run duration and token total identically under every row.

**Architecture:** The engine gains three additive fields on `UserRunReport` — `setup_s` (the shared
history-fetch + candidate-gather span), `row_timing` (per-row wall clock and write-lock wait), and
`pool_costs` (per-pool tokens with the row slugs that shared each pool). These persist to one new
nullable `run_users.cost` JSON column (migration 0069), surface as `RunUserOut.cost`, and let the
Rows tab show a row's own time next to an honestly-labelled shared-setup line. No token figure is
ever divided between rows.

**Tech Stack:** Python 3.12, SQLAlchemy 2 + Alembic, FastAPI, Pydantic v2, pytest; React 19 +
TypeScript + vitest.

**Spec:** `.claude/docs/plans/per-row-run-cost.md`

## Global Constraints

- `shortlist/engine/` must NOT import from `shortlist/server/` — the engine is a pure library.
- `from loguru import logger`; never stdlib `logging`.
- Type hints on all params and returns; modern annotations (`list[str]`, not `typing.List`).
- 120-char lines, 4-space indent; `ruff format` / `ruff check .` clean.
- TypeScript strict; `any` is banned. API types are generated (`pnpm -C web gen:api`) — never
  hand-write request/response types.
- `duration_ms` is **wall clock INCLUDING `blocked_ms`**. Work time = `duration_ms − blocked_ms`.
  This convention is fixed; do not invert it in any task.
- The engine measures in **seconds** (`_s`, float); `run_persistence` converts to integer
  **milliseconds** (`_ms`) on write, as it already does for `duration_ms`.
- `cost = NULL` means **"not recorded"** (a legacy run) and must never render as `0s`. This mirrors
  the existing `rows_considered` rule.
- No privacy, share-filter, delivery-ordering or promote code is touched by this plan.
- Architecture Review is REQUIRED before the final commit (adds a migration; touches the engine).

---

### Task 1: Engine report fields and timing helpers

**Files:**

- Modify: `shortlist/engine/models.py:915` (end of `UserRunReport`)
- Modify: `shortlist/engine/rows.py:10-16` (imports), `shortlist/engine/rows.py:844` (new helpers)
- Test: `tests/unit/test_pipeline.py`

**Interfaces:**

- Consumes: nothing.
- Produces: `UserRunReport.setup_s: float`, `UserRunReport.row_timing: dict[str, dict[str, float]]`,
  `UserRunReport.pool_costs: list[dict]`, `UserRunReport.lock_bucket: str | None`;
  `rows._row_timer(report: UserRunReport, slug: str)` and
  `rows._timed_lock(ctx: EngineContext, report: UserRunReport)`, both context managers returning
  `Iterator[None]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pipeline.py
import time

from shortlist.engine import rows as rows_mod
from shortlist.engine.models import UserRunReport


class TestRowTiming:
    def test_row_timer_records_duration_when_body_completes(self):
        report = UserRunReport(username="alex", slug="alex")
        with rows_mod._row_timer(report, "picked-for-you"):
            time.sleep(0.01)
        assert report.row_timing["picked-for-you"]["duration_s"] >= 0.01
        assert report.row_timing["picked-for-you"]["blocked_s"] == 0.0
        assert report.lock_bucket is None

    def test_row_timer_records_duration_when_body_breaks_early(self):
        """The delivery loop `break`s on cancel — an interrupted row still cost the time it spent."""
        report = UserRunReport(username="alex", slug="alex")
        for _ in range(1):
            with rows_mod._row_timer(report, "because-you-watched"):
                time.sleep(0.01)
                break
        assert report.row_timing["because-you-watched"]["duration_s"] >= 0.01

    def test_row_timer_records_duration_when_body_raises(self):
        report = UserRunReport(username="alex", slug="alex")
        try:
            with rows_mod._row_timer(report, "picked-for-you"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert report.row_timing["picked-for-you"]["duration_s"] > 0
        assert report.lock_bucket is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_pipeline.py::TestRowTiming -v`
Expected: FAIL — `AttributeError: module 'shortlist.engine.rows' has no attribute '_row_timer'`

- [ ] **Step 3: Add the report fields**

In `shortlist/engine/models.py`, immediately after `rows_considered` (line 915):

```python
    # Seconds spent on work EVERY row shares — the watch-history fetch and the candidate gather.
    # All AI spend happens here (see `pool_costs`), so on a typical person this dwarfs the rows.
    # Reported as its own line rather than divided between rows, which would invent a split.
    setup_s: float = 0.0
    # Per-row cost keyed by row slug: {"duration_s": wall clock, "blocked_s": of which, waiting on
    # the shared Plex write lock}. duration_s INCLUDES blocked_s; work time is the difference.
    # At concurrency 1 blocked_s is always ~0; at 8 it is what explains a row that looks slow.
    row_timing: dict[str, dict[str, float]] = field(default_factory=dict)
    # One entry per candidate-pool COMPUTATION: {"label", "tokens", "exa_searches", "duration_s",
    # "rows": [slug, ...]}. Pools are memoised per `pool_key` and usually shared by every row, so
    # `rows` is what lets the UI say "one pool, used by both rows" instead of splitting the tokens.
    pool_costs: list[dict] = field(default_factory=list)
    # INTERNAL cursor, never persisted: which row `_timed_lock` charges write-lock waits to.
    # None means setup, whose wait is already inside `setup_s`.
    lock_bucket: str | None = None
```

- [ ] **Step 4: Add the helpers**

In `shortlist/engine/rows.py`, extend the imports at lines 10-16:

```python
from collections.abc import Callable, Iterator
from contextlib import contextmanager
```

Then add after `_add_step_tokens` (line 844):

```python
def _blank_row_cost() -> dict[str, float]:
    return {"duration_s": 0.0, "blocked_s": 0.0}


@contextmanager
def _row_timer(report: UserRunReport, slug: str) -> Iterator[None]:
    """Time one row's own work, charging any write-lock wait inside it to that row.

    Stamps on EVERY exit. The delivery loop `break`s both on a cancel and on a row whose write was
    stopped, and an interrupted row still cost the time it spent — leaving it unstamped would make
    it indistinguishable from a row that was never recorded at all.
    """
    entry = report.row_timing.setdefault(slug, _blank_row_cost())
    report.lock_bucket = slug
    started = time.monotonic()
    try:
        yield
    finally:
        entry["duration_s"] += round(time.monotonic() - started, 3)
        report.lock_bucket = None


@contextmanager
def _timed_lock(ctx: EngineContext, report: UserRunReport) -> Iterator[None]:
    """Take the run's write lock, charging the WAIT to the report's current row bucket.

    At concurrency > 1 every Plex write is serialized, so a row's wall clock silently absorbs time
    spent waiting on OTHER people's writes — time that is not this row's work, and that makes two
    rows on a busy run incomparable. Recording it separately is what keeps the comparison honest.

    The cursor lives on the REPORT, never on ``ctx``: ctx is shared by the whole user pool, so an
    accumulator there would bill one person's wait to another person's row.
    """
    started = time.monotonic()
    with ctx.write_lock:
        bucket = report.lock_bucket
        if bucket is not None:
            report.row_timing.setdefault(bucket, _blank_row_cost())["blocked_s"] += round(time.monotonic() - started, 3)
        yield
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_pipeline.py::TestRowTiming -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add shortlist/engine/models.py shortlist/engine/rows.py tests/unit/test_pipeline.py
git commit -m "feat(runs): record per-row timing fields on the user run report"
```

---

### Task 2: Charge write-lock wait at the delivery site

**Files:**

- Modify: `shortlist/engine/rows.py:1989`
- Test: `tests/unit/test_pipeline.py`

**Interfaces:**

- Consumes: `rows._timed_lock`, `rows._row_timer` (Task 1).
- Produces: nothing new; `report.row_timing[slug]["blocked_s"]` becomes non-zero under contention.

Only ONE of the four `ctx.write_lock` sites needs this. `_drop_cold_skipped_rows` (1146),
`_remove_muted_and_retired` (1185) and `_record_demand` (1683) all run during SETUP, before the row
loop, so their wait is already inside `setup_s` and `lock_bucket` is `None` there. The delivery
write inside `_deliver_row`'s `_deliver_locked` (1989) is the only one that runs per row.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pipeline.py — add to TestRowTiming
import threading


def test_timed_lock_charges_wait_to_the_current_row(self):
    from shortlist.engine.context import EngineContext

    ctx = EngineContext.__new__(EngineContext)
    ctx.write_lock = threading.Lock()
    report = UserRunReport(username="alex", slug="alex")

    holder_has_lock = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with ctx.write_lock:
            holder_has_lock.set()
            release.wait(timeout=2)

    t = threading.Thread(target=hold)
    t.start()
    holder_has_lock.wait(timeout=2)
    with rows_mod._row_timer(report, "picked-for-you"):
        release.set()
        with rows_mod._timed_lock(ctx, report):
            pass
    t.join(timeout=2)

    assert report.row_timing["picked-for-you"]["blocked_s"] > 0


def test_timed_lock_charges_nothing_during_setup(self):
    """lock_bucket is None before the row loop — that wait belongs to setup_s, not to a row."""
    from shortlist.engine.context import EngineContext

    ctx = EngineContext.__new__(EngineContext)
    ctx.write_lock = threading.Lock()
    report = UserRunReport(username="alex", slug="alex")
    with rows_mod._timed_lock(ctx, report):
        pass
    assert report.row_timing == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_pipeline.py::TestRowTiming -v -k timed_lock`
Expected: `test_timed_lock_charges_wait_to_the_current_row` FAILS — `blocked_s` is `0.0` because
`_deliver_locked` still uses the bare lock. (`test_timed_lock_charges_nothing_during_setup` may
already pass; keep it — it pins the setup contract.)

- [ ] **Step 3: Swap the delivery lock**

In `shortlist/engine/rows.py`, inside `_deliver_row`'s nested `_deliver_locked` (line 1989), change:

```python
        with ctx.write_lock:
```

to:

```python
        # Timed, not bare: this is the ONLY write lock taken inside the per-row loop, so it is the
        # only one whose wait can make one row look slower than its sibling. The three setup-time
        # locks stay bare — their wait is already inside `setup_s`.
        with _timed_lock(ctx, policy.report):
```

Verify `ctx` and `policy` are both in scope at that point (they are: `_deliver_row(policy, ...)` at
line 1947 binds `policy`, and `ctx` is taken from it in the enclosing body). If the local is named
differently, use `policy.report` regardless — the report must be the per-user one.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_pipeline.py::TestRowTiming -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add shortlist/engine/rows.py tests/unit/test_pipeline.py
git commit -m "feat(runs): charge Plex write-lock wait to the row that waited"
```

---

### Task 3: Time setup and each row in `_run_user`

**Files:**

- Modify: `shortlist/engine/rows.py:2116-2270` (inside `_run_user`)
- Test: `tests/unit/test_pipeline.py`

**Interfaces:**

- Consumes: `rows._row_timer` (Task 1), `UserRunReport.setup_s` (Task 1).
- Produces: `report.setup_s` populated; `report.row_timing` has one entry per row the loop entered.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pipeline.py — new class
class TestRunUserCost:
    def test_setup_and_every_entered_row_are_timed(self, engine_config, mock_plex, mock_tmdb):
        """Every row the loop ENTERS gets an entry — including one whose sources were down, which
        `continue`s. Without it the UI cannot tell 'finished fast' from 'not recorded'."""
        report = _run_two_row_user(engine_config, mock_plex, mock_tmdb)  # helper below
        assert report.setup_s > 0
        assert set(report.row_timing) == {"picked-for-you", "because-you-watched"}
        for cost in report.row_timing.values():
            assert cost["duration_s"] >= 0.0
            assert cost["blocked_s"] >= 0.0
```

Build `_run_two_row_user` as a module-level helper in the test file that configures two per-person
rows on `engine_config`, gives the user enough history to avoid a cold start, and calls
`rows._run_user(...)` with the existing `mock_plex` / `mock_tmdb` fixtures. Follow the arrangement
already used by the nearest existing `_run_user` test in this file — reuse its fixture setup rather
than inventing a second one.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_pipeline.py::TestRunUserCost -v`
Expected: FAIL — `assert report.setup_s > 0` (it is `0.0`; nothing sets it yet)

- [ ] **Step 3: Time the setup span**

In `_run_user`, capture the start immediately before the history fetch (line 2116, the
`_emit(ctx, user.slug, "history", {})` call) and close it after the warm/cold start completes.

Add before `_emit(ctx, user.slug, "history", {})`:

```python
    setup_started = time.monotonic()
```

Then immediately after the `if cold: ... else: _warm_start(...)` block and before the
`if not ctx.plex.sections_by_type():` guard, add:

```python
    # Everything above is shared by every row this person has — the history read and the candidate
    # gather, which is where all AI spend happens. Closed here, before the first row is touched.
    user_report.setup_s = round(time.monotonic() - setup_started, 3)
```

The early `return False` paths above this point (no specs, cold-skipped-every-row) leave `setup_s`
at `0.0`, which is correct: those people never reached the shared gather.

- [ ] **Step 4: Time each row**

Wrap the body of `for spec in specs:` in the row timer. The loop begins at line ~2189
(`for spec in specs:`) and ends at `delivered_any = delivered_any or bool(picks)`. Change:

```python
    for spec in specs:
        override = user.row_overrides.get(spec.slug)
```

to:

```python
for spec in specs:
    with _row_timer(user_report, spec.slug):
        override = user.row_overrides.get(spec.slug)
```

and re-indent the whole loop body one level under the `with`. Do NOT change any `continue` or
`break` inside it — `_row_timer` stamps in a `finally`, so every exit is recorded.

Run `ruff format shortlist/engine/rows.py` afterwards to normalise the indentation to 4 spaces.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_pipeline.py::TestRunUserCost -v`
Expected: PASS

- [ ] **Step 6: Run the surrounding suite for regressions**

Run: `pytest tests/unit/test_pipeline.py tests/unit/test_delivery.py -q`
Expected: PASS — the re-indent is the risky part of this task.

- [ ] **Step 7: Commit**

```bash
git add shortlist/engine/rows.py tests/unit/test_pipeline.py
git commit -m "feat(runs): time the shared setup span and each row in _run_user"
```

---

### Task 4: Record pool costs and which rows shared each pool

**Files:**

- Modify: `shortlist/engine/rows.py:846` (`_record_gather`), `shortlist/engine/rows.py:1495`
  (`pools_for`)
- Test: `tests/unit/test_pipeline.py`

**Interfaces:**

- Consumes: `UserRunReport.pool_costs` (Task 1).
- Produces: `report.pool_costs` entries shaped
  `{"label": str, "tokens": int, "exa_searches": int, "duration_s": float, "rows": list[str]}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pipeline.py — new class
class TestPoolCosts:
    def test_two_rows_sharing_a_pool_record_one_entry_naming_both(self, engine_config, mock_plex, mock_tmdb):
        """The whole point of the honest split: one gather, one token figure, both rows named.
        A cache HIT must still attribute its row, or the pool reads as belonging to one row."""
        report = _run_two_row_user(engine_config, mock_plex, mock_tmdb)
        assert len(report.pool_costs) == 1
        entry = report.pool_costs[0]
        assert sorted(entry["rows"]) == ["because-you-watched", "picked-for-you"]
        assert entry["tokens"] == report.llm_tokens
        assert entry["label"]

    def test_cold_start_user_records_no_pools(self, engine_config, mock_plex, mock_tmdb):
        """Cold start never builds a pool. `[]` is the true answer, not missing data."""
        report = _run_cold_user(engine_config, mock_plex, mock_tmdb)
        assert report.pool_costs == []
```

Add `_run_cold_user` alongside `_run_two_row_user`, giving the user fewer watches than
`engine_config.min_history`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_pipeline.py::TestPoolCosts -v`
Expected: FAIL — `assert len(report.pool_costs) == 1` gets `0`

- [ ] **Step 3: Record the pool cost in `_record_gather`**

Change the signature and body of `_record_gather` (line 846) to take the pool's own elapsed time and
append an entry. Replace the `for source, tokens in ...` block's surroundings with:

```python
def _record_gather(
    report: UserRunReport,
    stats: candidates_mod.GatherStats,
    *,
    pool_label: str | None = None,
    duration_s: float = 0.0,
) -> None:
```

and append, immediately after the existing `report.exa_cache_hits += stats.exa_cache_hits` line:

```python
    # One entry per pool COMPUTATION. `rows` is filled by `pools_for` on every call, hit or miss —
    # a cache hit is exactly the case that proves a second row shared this gather, and it is what
    # stops the UI dividing one token figure between two rows.
    report.pool_costs.append(
        {
            "label": pool_label or "",
            "tokens": sum(stats.tokens_by_source.values()),
            "exa_searches": stats.exa_searches,
            "duration_s": round(duration_s, 3),
            "rows": [],
        }
    )
```

- [ ] **Step 4: Attribute rows in `pools_for`**

In `pools_for` (line 1495), keep a slug→entry map so both a miss and a hit attribute the row. Add an
instance dict — initialise `self.pool_rows: dict[tuple, dict] = {}` wherever `pool_cache` is
initialised in `RowPolicy` — then:

Time the gather by wrapping the `_candidate_pool(...)` call:

```python
            gather_started = time.monotonic()
            try:
                self.pool_cache[key], gather_stats = _candidate_pool(
                    ...unchanged...
                )
            except Exception as e:
                ...unchanged...
```

and change the `_record_gather(self.report, gather_stats, pool_label=pool_label)` call to:

```python
            _record_gather(
                self.report,
                gather_stats,
                pool_label=pool_label,
                duration_s=time.monotonic() - gather_started,
            )
            self.pool_rows[key] = self.report.pool_costs[-1]
```

Then, immediately before the final `return self.pool_cache[key]`, attribute this row whether the
pool was just built or served from cache:

```python
        # Hit or miss: a cache hit is the case that proves this row SHARED an existing gather.
        entry = self.pool_rows.get(key)
        if entry is not None and spec.slug not in entry["rows"]:
            entry["rows"].append(spec.slug)
        return self.pool_cache[key]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_pipeline.py::TestPoolCosts -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add shortlist/engine/rows.py tests/unit/test_pipeline.py
git commit -m "feat(runs): record per-pool AI cost and the rows that shared each pool"
```

---

### Task 5: Migration 0069 and the `run_users.cost` column

**Files:**

- Create: `shortlist/server/db/alembic/versions/0069_run_user_cost.py`
- Modify: `shortlist/server/db/models.py:332` (after `rows_considered`)
- Test: `tests/unit/test_migrations.py`, `tests/integration/test_migration_recovery.py`

**Interfaces:**

- Consumes: nothing.
- Produces: `RunUser.cost: Mapped[dict | None]` (JSON, nullable, default `None`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_migrations.py
def test_0069_adds_nullable_cost_to_run_users(migrated_engine):
    """Nullable with NO backfill: a legacy run has no per-row cost and must read 'not recorded'.
    Backfilling 0 would claim every historical row took no time — the same class of confidently
    wrong answer this feature exists to remove."""
    import sqlalchemy as sa

    with migrated_engine.connect() as conn:
        cols = {row[1]: row for row in conn.execute(sa.text("PRAGMA table_info(run_users)"))}
    assert "cost" in cols
    assert cols["cost"][3] == 0  # notnull == 0 -> nullable
```

Use whatever fixture the neighbouring tests in `tests/unit/test_migrations.py` already use to get a
fully-migrated engine; do not add a new one.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_migrations.py -k 0069 -v`
Expected: FAIL — `assert "cost" in cols`

- [ ] **Step 3: Write the migration**

Create `shortlist/server/db/alembic/versions/0069_run_user_cost.py`:

```python
"""Record what each ROW cost a person, not just what the whole person cost.

`run_users` carries one `duration_ms` and one `llm_tokens` per person per run, so the rows-first run
view printed that same pair under every row the person was in — on run #7 (SFLIX, 2026-08-13) Alex
Mastroianni read "7m 22s · 15,917 AI tokens" identically under both his rows, which looks like two
rows each independently costing 7m 22s. Neither was true.

NULL means "not recorded", exactly as `rows_considered` uses `{}`, and is deliberately NOT
backfilled: a legacy run has no per-row measurement, and writing zeros would claim every historical
row took no time at all — the same confidently-wrong answer, pointed at the whole archive.

Re-runnable, per `tests/integration/test_migration_recovery.py`: a crash between the DDL and the
version stamp replays this revision, and a bare `add_column` would then fail with "duplicate column
name" and wedge every later upgrade.

Revision ID: 0069
Revises: 0068
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {row[1] for row in bind.execute(sa.text("PRAGMA table_info(run_users)"))}


def upgrade() -> None:
    bind = op.get_bind()
    if "cost" not in _columns(bind):
        op.add_column("run_users", sa.Column("cost", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "cost" in _columns(bind):
        op.drop_column("run_users", "cost")
```

- [ ] **Step 4: Add the model column**

In `shortlist/server/db/models.py`, after `rows_considered` (line 332):

```python
    # What each ROW cost this person, and what the shared setup cost:
    # {"setup_ms": int, "rows": {slug: {"duration_ms": int, "blocked_ms": int}},
    #  "pools": [{"label", "tokens", "exa_searches", "duration_ms", "rows": [slug, ...]}]}.
    #
    # NULL on a legacy run — "not recorded", which the UI must never render as 0s. `duration_ms` is
    # wall clock INCLUDING `blocked_ms` (time waiting on the shared Plex write lock); work time is
    # the difference. Tokens live on the POOL, never on a row: pools are shared between rows, so a
    # per-row token figure would be an allocation invented by the UI rather than a measurement.
    cost: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_migrations.py tests/integration/test_migration_recovery.py -q`
Expected: PASS — including the re-runnability replay.

- [ ] **Step 6: Commit**

```bash
git add shortlist/server/db/alembic/versions/0069_run_user_cost.py shortlist/server/db/models.py tests/unit/test_migrations.py
git commit -m "feat(runs): add nullable run_users.cost for per-row measurement"
```

---

### Task 6: Persist the cost blob

**Files:**

- Modify: `shortlist/server/services/run_persistence.py:443-461` (`_persist_user_report`)
- Test: `tests/unit/test_run_persistence.py`

**Interfaces:**

- Consumes: `UserRunReport.setup_s` / `.row_timing` / `.pool_costs` (Tasks 1, 3, 4);
  `RunUser.cost` (Task 5).
- Produces: `run_persistence._cost_blob(user_report) -> dict | None`.

Only `_persist_user_report` needs changing — both the live path (`persist_user_live`, line 207) and
the end-of-run backstop (`persist_report`) route through it.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_run_persistence.py
from shortlist.server.services.run_persistence import _cost_blob


class TestCostBlob:
    def test_seconds_become_integer_milliseconds(self):
        report = UserRunReport(username="alex", slug="alex")
        report.setup_s = 421.0
        report.row_timing = {"picked-for-you": {"duration_s": 12.04, "blocked_s": 0.31}}
        report.pool_costs = [
            {
                "label": "movie · tmdb, llm_web",
                "tokens": 15917,
                "exa_searches": 3,
                "duration_s": 398.0,
                "rows": ["picked-for-you"],
            }
        ]
        blob = _cost_blob(report)
        assert blob["setup_ms"] == 421000
        assert blob["rows"]["picked-for-you"] == {"duration_ms": 12040, "blocked_ms": 310}
        assert blob["pools"][0]["duration_ms"] == 398000
        assert blob["pools"][0]["tokens"] == 15917
        assert "duration_s" not in blob["pools"][0]

    def test_a_report_that_measured_nothing_persists_null(self):
        """Not `{}`: an empty blob would render as a real measurement of zero. A user who never
        reached the gather (no rows due) genuinely has nothing recorded."""
        assert _cost_blob(UserRunReport(username="alex", slug="alex")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_run_persistence.py::TestCostBlob -v`
Expected: FAIL — `ImportError: cannot import name '_cost_blob'`

- [ ] **Step 3: Write `_cost_blob`**

Add to `shortlist/server/services/run_persistence.py`, above `_persist_user_report`:

```python
def _cost_blob(user_report) -> dict | None:
    """The per-row cost record for `RunUser.cost`, in integer milliseconds, or None when nothing
    was measured.

    None rather than `{}` on the empty case: an empty blob is indistinguishable from a real
    measurement of zero, and a person who never reached the shared gather (no rows due for them)
    has nothing recorded rather than a zero cost.
    """
    if not user_report.row_timing and not user_report.pool_costs and not user_report.setup_s:
        return None
    return {
        "setup_ms": int(user_report.setup_s * 1000),
        "rows": {
            slug: {"duration_ms": int(cost["duration_s"] * 1000), "blocked_ms": int(cost["blocked_s"] * 1000)}
            for slug, cost in user_report.row_timing.items()
        },
        "pools": [
            {
                "label": pool["label"],
                "tokens": pool["tokens"],
                "exa_searches": pool["exa_searches"],
                "duration_ms": int(pool["duration_s"] * 1000),
                "rows": list(pool["rows"]),
            }
            for pool in user_report.pool_costs
        ],
    }
```

- [ ] **Step 4: Wire it into the RunUser insert**

In `_persist_user_report`, add to the `RunUser(...)` constructor after `rows_considered`:

```python
cost = (_cost_blob(user_report),)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_run_persistence.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add shortlist/server/services/run_persistence.py tests/unit/test_run_persistence.py
git commit -m "feat(runs): persist the per-row cost blob on RunUser"
```

---

### Task 7: Expose `cost` on the run detail API

**Files:**

- Modify: `shortlist/server/api/schemas_runs.py:73` (`RunUserOut`),
  `shortlist/server/api/runs.py:238` and `runs.py:280`
- Test: `tests/integration/test_api_runs.py`, `tests/unit/test_openapi_snapshot.py`

**Interfaces:**

- Consumes: `RunUser.cost` (Task 5).
- Produces: `RunUserOut.cost: dict[str, Any] | None` in `GET /api/runs/{id}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_api_runs.py
def test_run_detail_carries_per_row_cost(client, seeded_run_with_cost):
    body = client.get(f"/api/runs/{seeded_run_with_cost}").json()
    user = body["users"][0]
    assert user["cost"]["rows"]["picked-for-you"]["duration_ms"] == 12040
    assert user["cost"]["pools"][0]["rows"] == ["picked-for-you"]


def test_run_detail_reports_null_cost_for_a_legacy_run(client, seeded_legacy_run):
    """A run recorded before 0069 must say 'not recorded', never zero."""
    body = client.get(f"/api/runs/{seeded_legacy_run}").json()
    assert body["users"][0]["cost"] is None


def test_pending_users_report_null_cost(client, running_run):
    body = client.get(f"/api/runs/{running_run}").json()
    pending = [u for u in body["users"] if u["status"] == "pending"]
    assert pending and all(u["cost"] is None for u in pending)
```

Build `seeded_run_with_cost` / `seeded_legacy_run` on the fixtures already in this file — a legacy
run is simply a `RunUser` row left with `cost=None`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_api_runs.py -k cost -v`
Expected: FAIL — `KeyError: 'cost'`

- [ ] **Step 3: Add the schema field**

In `shortlist/server/api/schemas_runs.py`, after `rows_considered` in `RunUserOut`:

```python
    #: What each ROW cost this person: `{"setup_ms", "rows": {slug: {"duration_ms", "blocked_ms"}},
    #: "pools": [...]}`. `null` on a run recorded before this existed — which the UI must render as
    #: "not recorded", never as 0s. `duration_ms` includes `blocked_ms`; work time is the difference.
    #: Tokens are reported per POOL, never per row: pools are shared between rows, so a per-row
    #: token figure would be invented rather than measured.
    cost: dict[str, Any] | None
```

- [ ] **Step 4: Serialize it**

In `shortlist/server/api/runs.py`, add to the completed-user dict after `rows_considered`
(line ~265):

```python
                    "cost": run_user.cost,
```

and to the synthesised pending-user dict after `rows_considered` (line ~288):

```python
                "cost": None,
```

- [ ] **Step 5: Refresh the OpenAPI snapshot and generated types**

Run: `pytest tests/unit/test_openapi_snapshot.py -q`
If it fails on a changed schema, regenerate the snapshot the way that test's failure message
instructs, then: `pnpm -C web gen:api`

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/integration/test_api_runs.py tests/unit/test_openapi_snapshot.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add shortlist/server/api/schemas_runs.py shortlist/server/api/runs.py tests/integration/test_api_runs.py web/src/lib/api-types.ts
git commit -m "feat(runs): expose per-row cost on the run detail endpoint"
```

---

### Task 8: Narrow the cost per row in `groupRunByRow`

**Files:**

- Modify: `web/src/lib/run-rows.ts:10-23` (`RunRowPerson`), `run-rows.ts:140-158`,
  `run-rows.ts:229-249`
- Test: `web/src/test/run-rows.test.ts`

**Interfaces:**

- Consumes: `RunUserResult["cost"]` (Task 7).
- Produces: `RunRowPerson.cost: { duration_ms: number; blocked_ms: number } | null`, and
  `RunRowPerson.setup: { setup_ms: number; pools: RunPoolCost[] } | null`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/test/run-rows.test.ts
describe("per-row cost", () => {
  it("gives each row its own duration instead of the person's whole-run total", () => {
    const run = makeRun({
      users: [
        makeUser({
          slug: "alex",
          duration_ms: 442000,
          rows_considered: {
            "picked-for-you": "due",
            "because-you-watched": "due",
          },
          breakdown: [
            {
              row_slug: "picked-for-you",
              library_key: "1",
              library_title: "Movies",
              added: [],
              removed: [],
              kept: [],
              picks: [],
            },
            {
              row_slug: "because-you-watched",
              library_key: "1",
              library_title: "Movies",
              added: [],
              removed: [],
              kept: [],
              picks: [],
            },
          ],
          cost: {
            setup_ms: 421000,
            rows: {
              "picked-for-you": { duration_ms: 12040, blocked_ms: 310 },
              "because-you-watched": { duration_ms: 9120, blocked_ms: 880 },
            },
            pools: [
              {
                label: "movie · tmdb, llm_web",
                tokens: 15917,
                exa_searches: 3,
                duration_ms: 398000,
                rows: ["picked-for-you", "because-you-watched"],
              },
            ],
          },
        }),
      ],
    });
    const { groups } = groupRunByRow(run);
    const picked = groups.find((g) => g.slug === "picked-for-you")!;
    const because = groups.find((g) => g.slug === "because-you-watched")!;
    expect(picked.people[0].cost?.duration_ms).toBe(12040);
    expect(because.people[0].cost?.duration_ms).toBe(9120);
  });

  it("reports null cost for a legacy run rather than zero", () => {
    const run = makeRun({
      users: [
        makeUser({
          slug: "alex",
          cost: null,
          rows_considered: { "picked-for-you": "due" },
        }),
      ],
    });
    const { groups } = groupRunByRow(run);
    expect(groups[0].people[0].cost).toBeNull();
  });
});
```

Reuse the `makeRun` / `makeUser` builders already in this test file; add `cost` to them defaulting
to `null`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm -C web test run-rows`
Expected: FAIL — `picked.people[0].cost` is `undefined`

- [ ] **Step 3: Extend the type**

In `web/src/lib/run-rows.ts`, add to `RunRowPerson`:

```ts
  /** THIS row's own cost, or null on a run recorded before it was measured — which must render as
   *  "not recorded", never as 0s. `duration_ms` includes `blocked_ms`; work time is the difference. */
  cost: { duration_ms: number; blocked_ms: number } | null;
  /** The person's SHARED setup, repeated on each of their rows because it belongs to none of them.
   *  All AI spend lives here, attributed per pool — never divided between rows. */
  setup: { setup_ms: number; pools: RunPoolCost[] } | null;
```

and export the pool type:

```ts
export type RunPoolCost = {
  label: string;
  tokens: number;
  exa_searches: number;
  duration_ms: number;
  /** Every row that drew from this pool. Length > 1 is why its tokens are never split per row. */
  rows: string[];
};
```

- [ ] **Step 4: Populate it**

In the `for (const slug of slugs)` loop (line ~140), change the `people.push({...})` call to include:

```ts
        cost: user.cost?.rows?.[slug] ?? null,
        setup: user.cost
          ? { setup_ms: user.cost.setup_ms ?? 0, pools: user.cost.pools ?? [] }
          : null,
```

In the synthesised pending-person block (line ~229), add:

```ts
        cost: null,
        setup: null,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pnpm -C web test run-rows`
Expected: PASS

- [ ] **Step 6: Typecheck**

Run: `pnpm -C web exec tsc -b`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add web/src/lib/run-rows.ts web/src/test/run-rows.test.ts
git commit -m "feat(runs): narrow per-row cost onto each row's people"
```

---

### Task 9: Show the row's own cost and the shared setup

**Files:**

- Modify: `web/src/components/runs/user-panel.tsx:270-320` (header),
  `user-panel.tsx:185-210` (remove the vestigial per-row token block),
  `web/src/components/runs/run-rows-tab.tsx:281-288` (pass the cost down)
- Test: `web/src/test/run-rows-tab.test.tsx`

**Interfaces:**

- Consumes: `RunRowPerson.cost` / `.setup` (Task 8).
- Produces: `UserPanel` accepts optional `cost` and `setup` props.

`RowSection`'s `rowTokens` block (`user-panel.tsx:194-209`) reads `active?.llm_tokens` from a
breakdown entry. **Nothing in the engine has written that field since curation moved in-code** —
`rows.py` only ever writes `report.llm_tokens` / `report.llm_tokens_by_step`. It is dead, and it is
a per-row token figure of exactly the kind this work exists to stop showing. Delete it here rather
than shipping two competing stories.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/test/run-rows-tab.test.tsx
it("shows this row's own time, not the person's whole-run total", async () => {
  render(
    <RunRowsTab run={runWithPerRowCost} titles={{}} idBySlug={new Map()} />,
  );
  await userEvent.click(screen.getByRole("button", { name: /Picked for You/ }));
  expect(screen.getByText(/12s/)).toBeInTheDocument();
  expect(screen.queryByText(/7m 22s/)).not.toBeInTheDocument();
});

it("attributes AI tokens to the shared setup, naming both rows that used the pool", async () => {
  render(
    <RunRowsTab run={runWithPerRowCost} titles={{}} idBySlug={new Map()} />,
  );
  await userEvent.click(screen.getByRole("button", { name: /Picked for You/ }));
  expect(screen.getByText(/shared setup/i)).toBeInTheDocument();
  expect(screen.getByText(/15,917/)).toBeInTheDocument();
});

it("says timing was not recorded for a legacy run instead of showing 0s", async () => {
  render(<RunRowsTab run={legacyRun} titles={{}} idBySlug={new Map()} />);
  await userEvent.click(screen.getByRole("button", { name: /Picked for You/ }));
  expect(screen.getByText(/not recorded/i)).toBeInTheDocument();
  expect(screen.queryByText(/^0s$/)).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm -C web test run-rows-tab`
Expected: FAIL — `7m 22s` is present

- [ ] **Step 3: Accept the props in `UserPanel`**

Add to `UserPanel`'s props type:

```tsx
  /** THIS row's cost when the panel is rendered inside a row. Omitted on a whole-person view. */
  cost?: { duration_ms: number; blocked_ms: number } | null;
  setup?: { setup_ms: number; pools: RunPoolCost[] } | null;
```

Replace the metrics paragraph (`user-panel.tsx:314-320`) with:

```tsx
{
  cost !== undefined ? (
    cost === null ? (
      <p className="text-right text-sm text-muted-foreground">
        Timing not recorded for this run
      </p>
    ) : (
      <p className="text-right text-sm text-muted-foreground">
        {formatDuration(cost.duration_ms - cost.blocked_ms)}
        {cost.blocked_ms >= cost.duration_ms * 0.1 &&
          ` · ${formatDuration(cost.blocked_ms)} waiting`}
        {setup && setup.setup_ms > 0 && (
          <>
            {" · shared setup "}
            {formatDuration(setup.setup_ms)}
            {setup.pools.length > 0 &&
              ` · ${setup.pools
                .reduce((n, p) => n + p.tokens, 0)
                .toLocaleString()} AI tokens`}
            {setup.pools.some((p) => p.rows.length > 1) &&
              ` (one pool, shared by ${setup.pools.find((p) => p.rows.length > 1)!.rows.length} rows)`}
          </>
        )}
      </p>
    )
  ) : (
    (result.duration_ms ?? 0) > 0 && (
      <p className="text-right text-sm text-muted-foreground">
        {formatDuration(result.duration_ms)}
        {tokens}
        {webSearchSummary(result.exa_searches)}
      </p>
    )
  );
}
```

- [ ] **Step 4: Delete the dead per-row token block**

In `RowSection` (`user-panel.tsx:185-210`), remove the `rowTokens` const and the `{rowTokens > 0 && ...}`
span entirely. Leave `added` and the title untouched.

- [ ] **Step 5: Pass the cost down**

In `run-rows-tab.tsx`, the `RowCard` already resolves `decision` from
`group.people.find((person) => person.result.slug === chosen?.slug)`. Reuse that same lookup for the
person, then pass:

```tsx
<UserPanel
  run={run}
  result={chosen}
  liveLog={liveLog}
  userId={idBySlug.get(chosen.slug) ?? null}
  cost={chosenPerson?.cost ?? null}
  setup={chosenPerson?.setup ?? null}
/>
```

where `chosenPerson` is that same `group.people.find(...)` result, hoisted to a const beside
`decision` so the array is walked once.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pnpm -C web test run-rows-tab user-panel`
Expected: PASS

- [ ] **Step 7: Typecheck**

Run: `pnpm -C web exec tsc -b`
Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add web/src/components/runs/user-panel.tsx web/src/components/runs/run-rows-tab.tsx web/src/test/run-rows-tab.test.tsx
git commit -m "feat(runs): show a row's own cost and its shared setup, not the person's total"
```

---

### Task 10: Say "people" in the row progress line

**Files:**

- Modify: `web/src/lib/run-rows.ts:318-321` (`rowSummary`)
- Test: `web/src/test/run-rows.test.ts`

**Interfaces:**

- Consumes: nothing.
- Produces: no signature change; the string changes.

Both row cards showing `building — 4 of 46 done` is not two rows racing: a person's result lands only
once they have finished ALL their rows, so every per-person row necessarily shows the same count.
The mechanism is right; the wording invites the wrong reading.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/test/run-rows.test.ts
it("counts PEOPLE in the building line, since a person's rows all land together", () => {
  const group = {
    kind: "per_person",
    pending: 42,
    people: new Array(46).fill(null).map(() => ({})),
  } as never;
  expect(rowSummary(group)).toBe("building — 4 of 46 people done");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm -C web test run-rows`
Expected: FAIL — received `"building — 4 of 46 done"`

- [ ] **Step 3: Change the string**

In `rowSummary` (line ~320):

```ts
return `building — ${total - group.pending} of ${total} people done`;
```

and update the comment above it to say why every row shows the same count:

```ts
// Mid-run the row is still working through PEOPLE, and who is LEFT is the thing you are watching.
// Every per-person row shows the same count on purpose: a person's rows are built in one pass and
// all land together, so there is no per-row progress to report. Saying "people" stops that reading
// as two rows racing each other.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm -C web test run-rows`
Expected: PASS

- [ ] **Step 5: Fix the stale comment in `run-rows-tab.tsx`**

`run-rows-tab.tsx:271` says `// The card header two lines above already says "10 of 46 done".`
Update it to `"10 of 46 people done"` so it matches what the code now emits.

- [ ] **Step 6: Commit**

```bash
git add web/src/lib/run-rows.ts web/src/components/runs/run-rows-tab.tsx web/src/test/run-rows.test.ts
git commit -m "fix(runs): the row progress line counts people, not rows"
```

---

### Task 11: Docs, review, and full verification

**Files:**

- Modify: `docs/reference.md` (the `GET /api/runs/{id}` response fields)
- Modify: `.claude/docs/jobs-and-runs-design.md` (per-row cost recording)
- Modify: `.claude/docs/plans/per-row-run-cost.md` (mark status)

- [ ] **Step 1: Document the new API field**

In `docs/reference.md`, find the `GET /api/runs/{id}` response documentation and add `cost` to the
per-user fields, stating: nullable, `null` means not recorded, `duration_ms` includes `blocked_ms`,
and tokens are per-pool because pools are shared between rows. Keep it environment-agnostic — no
hostnames, IPs or personal paths.

- [ ] **Step 2: Update the runs design doc**

In `.claude/docs/jobs-and-runs-design.md`, add a short subsection recording that per-row cost exists,
that all AI spend is pool-scoped and never divided per row, and that `cost = NULL` is "not recorded".

- [ ] **Step 3: Mark the spec DONE**

In `.claude/docs/plans/per-row-run-cost.md`, change the Status line to
`**Status:** DONE — shipped <date>.`

- [ ] **Step 4: Run the FULL suite**

```bash
pytest -q
pnpm -C web test
pnpm -C web exec tsc -b
pnpm -C web build
ruff check . --fix && ruff format .
```

Expected: all green. The run view is a UI flow, so also run:

```bash
pytest -m e2e -q
```

- [ ] **Step 5: Architecture Review**

REQUIRED — this diff adds an Alembic migration and touches the engine's per-user hot path, which is
on the dispatch list in `.claude/CLAUDE.md`. Dispatch the Architecture Review agent on the staged
diff and block on any HIGH finding.

- [ ] **Step 6: Commit**

```bash
git add docs/reference.md .claude/docs/jobs-and-runs-design.md .claude/docs/plans/per-row-run-cost.md
git commit -m "docs(runs): record per-row run cost in the reference and design docs"
```

---

## Self-Review

**Spec coverage.** Every section of `.claude/docs/plans/per-row-run-cost.md` maps to a task: storage
→ Task 5; engine `setup_s`/per-row/`blocked_ms`/`pool_costs` → Tasks 1-4; API → Task 7; UI narrowing
→ Task 8; UI rendering and the `rowSummary` wording → Tasks 9-10; testing and docs → spread through
each task plus Task 11. The spec's two named edge cases — a row whose sources are down, and a
cold-start user with no pools — are covered by Task 3 Step 1 and Task 4 Step 1.

**One simplification found while planning.** The spec anticipated instrumenting all four
`ctx.write_lock` sites. Only one (`_deliver_locked`, `rows.py:1989`) runs inside the row loop; the
other three run during setup, where the wait is already inside `setup_s`. Task 2 instruments one
site, which materially reduces the hot-path risk flagged in the design.

**One addition not in the spec.** Task 9 Step 4 deletes the `rowTokens` block at
`user-panel.tsx:194-209`. It reads `breakdown[].llm_tokens`, which no engine code has written since
curation moved in-code — it is dead, and it is a per-row token display of exactly the kind this work
removes. Shipping the new shared-setup line beside it would leave two competing stories on one page.
