# Phase 2 — Length, year and rating limits on every row: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Models: `sonnet` for implementers and per-task reviews, `haiku` for running suites, `opus` only for the final whole-branch review and the Architecture Review.

**Goal:** Every row can optionally limit candidates by max length (minutes), release year range and minimum rating. All default OFF.

**Architecture:** Four nullable per-row columns on `collections` flow through the same layers as `max_seeds` (DB → API → `RowSpec` → `row_recipe`). A pure engine function drops candidates outside the limits after the pool is gathered. Year and rating come from discover data already on `Candidate`; runtime comes from `TmdbClient.details` (cached 7 days), fetched only when `max_runtime` is set and only for candidates that survived the year/rating checks.

**Tech Stack:** Python 3.12, SQLAlchemy 2 + Alembic, FastAPI/Pydantic v2, React 19 + TS, vitest, pytest.

**Spec:** `.claude/docs/discussion-138-ai-custom-rows.md` (Decision 5; limits design ~lines 149–158). Format template: `.claude/docs/plans/2026-10-03-ai-instructions-phase1.md`.

## Global Constraints

- Every new setting defaults to today's behaviour: all four columns `NULL` = off. Nothing rebuilds on upgrade night.
- `row_recipe` gains a part ONLY when a limit is set; with all limits off the recipe is byte-identical to today's (pinned by test).
- `shortlist/engine/` must not import from `shortlist/server/`.
- Tests: no network; assert the kwargs the SUT controls; cover the matrix (movie/tv × limit set/unset × detail present/missing/failing).
- One pytest at a time (hook enforced); during edits run only the task's test files.
- Python: `PYTHONPATH=. /home/data/workspace/shortlist/.venv/bin/python`, ruff at `.../.venv/bin/ruff`.
- Commits: Conventional Commits, staged by explicit path, ending with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Ask-before-commit applies: the owner has directed phase work to be committed; no pushes until the phase-1 live check is clean.
- Never edit an existing test to match new behaviour.

## Decisions made by this plan (the design doc is silent)

- **Unknown is kept.** A candidate whose year, rating or runtime cannot be determined (None, or `details` failed) is NOT dropped — a limit must not silently empty a row on a TMDB hiccup. Dropped/kept-unknown counts are logged per row at info level (silent drops hide bugs).
- **Rating 0 is a real value.** `min_rating` compares against `Candidate.rating` (TMDB `vote_average`); titles with no votes (rating 0) are dropped when a minimum is set.
- **TV runtime** = first of `episode_run_time`, else `last_episode_to_air.runtime`, else unknown. Movie runtime = `runtime` (0/None = unknown).
- **Per-row only**, no server-wide default (YAGNI; Decision 5 says "on every row").
- Validation: `max_runtime` 1–600, years 1870–2100, `min_year <= max_year` when both set, `min_rating` 0–10.

## Review Focus

- `min_year > max_year` → 422 naming both fields.
- `details()` raising or returning `{}` for some candidates → kept, counted unknown, row still fills.
- Limits so tight the pool empties → row follows the existing "too few picks" path, never crashes.
- Limits set then cleared → recipe returns to the byte-identical form (no spurious rebuild beyond the one for the change itself).
- TV with no `episode_run_time` → kept (unknown), not dropped as 0 minutes.

---

### Task 0: Housekeeping commit

**Files:**
- Modify: `.claude/docs/discussion-138-ai-custom-rows.md:3` (Status line)
- Add: `.claude/docs/plans/2026-10-04-discussion-138-handoff.md` (currently untracked; owner asked it be committed here)
- Add: this plan file

- [ ] **Step 1:** Replace the Status line so it reads: `**Status:** phase 1 (AI instructions) shipped to dev at 43d9d26b and proven live 2026-10-04; phase 2 (limits) in progress; phases 3–4 not started.`
- [ ] **Step 2:** `git add` the three paths explicitly; commit `docs: #138 phase 2 plan, handoff, and status line`.

### Task 1: Engine — limits model, filter, recipe

**Files:**
- Create: `shortlist/engine/limits.py`
- Modify: `shortlist/engine/models.py` (RowSpec, near `max_seeds` at ~:526), `shortlist/engine/rows.py` (`row_recipe` ~:958–1034), the call site of `filter_candidates` (`shortlist/engine/candidates.py` ~:1149; confirm where the pipeline calls it)
- Test: `tests/unit/test_limits.py` (new), `tests/unit/test_pipeline.py` (recipe pin)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class RowLimits: max_runtime: int | None = None; min_year: int | None = None; max_year: int | None = None; min_rating: float | None = None` with `.active -> bool` and `.fingerprint() -> str` (e.g. `"rt<=120;y>=1990;y<=2010;r>=7.0"`, only set parts, fixed order).
  - `RowSpec.max_runtime / min_year / max_year / min_rating` (all `... | None = None`) and `RowSpec.limits() -> RowLimits`.
  - `apply_limits(candidates: list[Candidate], limits: RowLimits, tmdb: TmdbClient) -> LimitResult` where `LimitResult` has `.kept: list[Candidate]`, `.dropped: int`, `.unknown: int`. Returns input untouched (same list object) when `not limits.active`.
  - `runtime_minutes(details: dict, media_type: str) -> int | None`.
- Consumes: `Candidate.year/rating/media_type/tmdb_id`, `TmdbClient.details(tmdb_id, media_type) -> dict`.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_limits.py`: `fingerprint` order/omission; `apply_limits` inactive returns same list and makes zero `details` calls; year bounds inclusive; `min_rating` drops 0-rated; year None kept+unknown; `max_runtime` movie uses `runtime`; TV uses `episode_run_time[0]` then `last_episode_to_air.runtime`; `details` raising for one candidate → kept+unknown, others still judged; `details` NOT called for a candidate already dropped by year/rating (assert `mock_tmdb.details.call_args_list` tmdb_ids); `details` not called at all when `max_runtime` is None.
- [ ] **Step 2:** Run `pytest tests/unit/test_limits.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `limits.py` (pure; `from loguru import logger`; catch `Exception` around each `details` call, log at debug with ids only) and the four `RowSpec` fields + `limits()`.
- [ ] **Step 4:** Run the file → PASS.
- [ ] **Step 5: Recipe pin test first** in `tests/unit/test_pipeline.py` next to the existing recipe tests: `row_recipe` for a spec with no limits equals the string produced on the parent commit (capture it by running the existing builder on a fixed spec BEFORE editing `row_recipe`, paste as the expected literal); and with `max_runtime=120` the recipe differs and contains `limits=rt<=120`. Run → the limits case FAILS, the identical case PASSES.
- [ ] **Step 6:** In `row_recipe` append `*((f"limits={spec.limits().fingerprint()}",) if spec.limits().active else ())`, copying phase 1's `guide=` pattern. Run the two tests → PASS.
- [ ] **Step 7:** Wire `apply_limits` into the pipeline directly after the pool is gathered/filtered and before ranking (read the call site; place it where watched/library filtering already happens). Add a pipeline-level test in `tests/unit/test_pipeline.py` using the existing fixtures: a row with `max_year=2000` ends with only ≤2000 picks, and `mock_tmdb`/`details` call args are asserted. Log one info line `limits: kept=K dropped=D unknown=U` per row.
- [ ] **Step 8:** Run `tests/unit/test_limits.py` and the touched test classes with `-k`; ruff check/format the touched files. Commit `feat(engine): per-row length, year and rating limits (#138)`.

### Task 2: Server — migration, model, API

**Files:**
- Create: `shortlist/server/db/alembic/versions/0097_row_limits.py` (confirm 0096 is head: `ls versions | tail -3`)
- Modify: `shortlist/server/db/models.py` (after `max_seeds` ~:221), `shortlist/server/api/collections.py` (CollectionIn ~:220, CollectionOut ~:449, response dict ~:1044, PATCH assign ~:1343, and wherever `RowSpec` is built from a `Collection` — grep `max_seeds=` in `shortlist/server/`)
- Test: `tests/integration/test_api_collections.py` (beside `test_per_row_max_seeds_round_trips_and_reaches_the_spec`), migration test pattern as used by neighbouring migrations (grep `0047` / `0096` in `tests/`)

**Interfaces:**
- Produces: `Collection.max_runtime: int|None`, `.min_year: int|None`, `.max_year: int|None`, `.min_rating: float|None`; same names on CollectionIn/Out; reach `RowSpec` under the same names.
- Consumes: Task 1's `RowSpec` fields.

- [ ] **Step 1: Failing tests:** round-trip PATCH→GET for all four; boundaries (`max_runtime` 0 and 601 → 422; `min_year` 1869/2101 → 422; `min_rating` -0.1/10.1 → 422); `min_year=2010, max_year=2000` → 422 mentioning both field names; PATCH with `null` clears; the built `RowSpec` carries the values (copy the analogue's assertion style); migration test: upgrade adds the four nullable columns, existing rows read NULL, downgrade removes them.
- [ ] **Step 2:** Run the new tests → FAIL.
- [ ] **Step 3:** Migration mirrors `0047_row_max_seeds.py` (`op.add_column`, four columns, `Float` for rating) with a `downgrade` using `batch_alter_table` (SQLite cannot drop columns directly — confirm how other downgrades in this repo do it and copy).
- [ ] **Step 4:** Model columns, Pydantic fields (`Field(default=None, ge=…, le=…)`), a model-level validator for the year pair, response dict, PATCH assignment, RowSpec construction.
- [ ] **Step 5:** Run the test file with `-k "limit or max_seeds"` → PASS. Regenerate `web/openapi.snapshot.json` (command in `tests/unit/test_openapi_snapshot.py`) and run that test.
- [ ] **Step 6:** ruff; commit `feat(server): per-row limit columns, migration 0097 and API (#138)`.

### Task 3: Web — editor fields, overrides badge, phase-1 leftovers

**Files:**
- Modify: `web/src/components/rows/row-contents-fields.tsx` ("What goes in"), `web/src/lib/collections.ts` (`rowOverrides` ~:253 and the AI-instructions badge condition), `web/src/lib/api-schema.d.ts` (generated from the snapshot — never hand-edit)
- Create: `web/src/components/rows/row-limits-fields.tsx` (three inputs with an off-by-default state; copy the pattern in `row-max-seeds-setting.tsx` / `max-seeds-field.tsx`)
- Test: `web/src/lib/collections.test.ts`, a testing-library test for the new component

**Interfaces:** Consumes the Task 2 API field names.

- [ ] **Step 1:** Generate types from the snapshot (`pnpm -C web` gen script pointed at `web/openapi.snapshot.json`, not production).
- [ ] **Step 2: Failing tests:** `rowOverrides` lists "Max length 120 min", "Released 1990–2010", "Rating 7+" only when set, nothing when all null; the "AI instructions" badge appears only when the row actually uses AI web search (phase-1 leftover); component: empty inputs send `null`, a typed value sends a number, `min_year > max_year` shows an inline message before submit.
- [ ] **Step 3:** Implement the component (plain-English labels per the design doc: "Longest it can run (minutes)", "Released between", "Lowest rating (out of 10)"; helper text says leaving blank means no limit), mount it in "What goes in", and fix the badge condition.
- [ ] **Step 4:** `pnpm -C web test -- collections row-limits` (scoped) → PASS; `pnpm -C web exec tsc -b --force`.
- [ ] **Step 5:** Commit `feat(web): length, year and rating limits in the row editor (#138)`.

### Task 4: Phase-1 leftovers (backend copy and preview)

**Files:** located by the implementer via grep; each fix gets its own failing test first.

- [ ] 4a: A row with blank Add/Own text whose web search is then turned off must not 422 on a now-hidden field (`shortlist/server/api/collections.py` validator for `ai_instructions`: only require text when `llm_web` is a candidate source).
- [ ] 4b: Native "Exactly what's sent" footnote includes the user-message recency line on default/add paths.
- [ ] 4c: Settings built-in preview query key includes the backend.
- [ ] 4d: Byte-for-byte test of the built-in native USER message (pin the current output; do not change it).
- [ ] Commit `fix: phase-1 leftovers — hidden-field 422, preview footnote, query key, pinned user message (#138)`. If any item is larger than ~30 lines, stop and report instead of widening scope.

### Task 5: Docs

**Files:** `docs/reference.md`, `docs/guides.md`, `README.md` if it lists row settings, then `python scripts/build_llms_full.py`.

- [ ] Document the three limits (what each does, "unknown is kept", all off by default); regenerate `docs/llms-full.txt`; run `tests/unit/test_llms_full.py`. Commit `docs: row limits (#138)`. No hostnames/paths from CLAUDE.local.md.

### Task 6: Full verification and reviews (end of phase, once)

- [ ] `verifier` (haiku), one at a time: `pytest`, `pnpm -C web test`, `tsc -b --force`, `eslint .`, `pnpm -C web build`, `ruff check . && ruff format --check .`, then rebuild `web/dist` and `pytest -m e2e` (UI changed).
- [ ] **Architecture Review** (migration → dispatch per project CLAUDE.md), then one **opus** whole-branch review. Fix HIGH findings; re-review until a pass finds nothing.
- [ ] Merge `origin/dev` in, re-verify. **Do not push** until the phase-1 live check (no `settings_changed` rebuild on the first post-deploy nightly, ~16:30 UTC 2026-10-04) is clean.
- [ ] After push + CI green: dry-run proof on a throwaway MooHouse row (memory `live-run-proof-recipe`): `max_year=2000` → every pick ≤ 2000; `max_runtime=100` → no pick over 100 min. Ask Steve before any live write beyond that.
