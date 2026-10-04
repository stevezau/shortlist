# AI row (fixed theme) Implementation Plan — #138 phase 3

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A new row kind "AI row": the owner describes a row in words, the AI builds a theme once, and every run fills the row per person from that theme with no AI call.

**Architecture:** A theme is a dateless custom season plus hard rules (RowLimits from phase 2) plus AI-named titles. It is stored in a `themes` table; a row points at it with `Collection.theme_id`. The engine loads a theme through the same `seasons.load_titles` path (adapter builds a `Season`), applies rules via `limits.apply_limits`, and ranks per person with the `season` source. The AI runs only on build/refine (server service `theme_author`), never in a nightly run.

**Tech Stack:** FastAPI, SQLAlchemy 2 + Alembic (next revision after `0097_row_limits`), pure-Python engine, React 19 + TanStack Query + shadcn/ui, vitest, pytest + hypothesis.

**Spec:** `.claude/docs/discussion-138-ai-custom-rows.md` (Decisions 3, 6, 7, 10 and "Design (approach 1)"). Phase 4 (explore, cooldown, avoid-rows) is NOT in this plan; its columns are added in phase 4's migration.

## Global Constraints

- Every new setting defaults to today's behaviour; no existing row's recipe or `pool_key` changes (pinned-recipe tests stay green byte for byte).
- Engine imports nothing from `shortlist/server/`.
- No AI call in a nightly run for an AI row. AI only on build / refine.
- Hard rules (runtime, year, rating, votes) are enforced by code from TMDB data, never trusted from the AI.
- Library-only: titles not on the server are counted in the preview, never requested.
- AI row title names pass the duplicate-title check (`collections.py` `_reject_duplicate_name`).
- No new Plex write path, no delete path. New AI rows are created disabled until switched on.
- No token cap. Show usage per row; "Pause AI for this row" blocks build/refine for that row.
- Copy: names "AI row", templates "Describe a row" and "AI Picks"; plain English, never raw error codes.
- Conventional Commits, explicit `git add <path>`, attribution trailer. Python: `PYTHONPATH=. /home/data/workspace/shortlist/.venv/bin/python`, ruff from the same venv.

## Review Focus

- AI returns invalid JSON / empty string (provider "none", error) → build fails with a plain message, existing theme untouched.
- AI names titles that don't resolve on TMDB or aren't in the library → counted in preview, dropped, never an error.
- AI claims "under two hours" but TMDB runtime says otherwise → code drops it (rules applied after resolve).
- Theme with zero in-library members after rules → row keeps what it has tonight (like a failed season load), preview warns.
- Editing a theme changes `content_hash` → recipe changes → row rebuilds; unchanged theme → byte-identical recipe, no rebuild.
- AI-written reason text containing placeholders/markup/over-long → sanitised and truncated.
- Per-person row with taste summary: names never sent to the AI, only recent titles.
- Pause AI → build/refine endpoints return 409 with plain message; nightly run unaffected.

---

### Task 1: Migration 0098 + models

**Files:**
- Create: `shortlist/server/db/alembic/versions/0098_ai_row_themes.py`
- Modify: `shortlist/server/db/models.py` (add `Theme`; add Collection columns)
- Test: `tests/unit/test_migration_0098.py` (pattern: look at the existing 0097 migration test)

**Interfaces:**
- Produces: `Theme` model — `id` int PK, `slug` str unique, `name` str, `emoji` str|None, `brief` text default "", `origin` str (`"ai"|"manual"`), `media` JSON list[str], `tags` JSON list[{id,name}], `genres` JSON list[str], `excluded_genres` JSON list[str], `collections` JSON list[str], `picks` JSON list[{tmdb_id:int, media:str, origin:str, reason:str|None, title:str, year:int|None}], `rules` JSON dict (`max_runtime,min_year,max_year,min_rating,min_votes`, nullable values), `content_hash` str, `created_at` datetime, `ai_tokens` int default 0, `stats` JSON (named/resolved/in_library/after_rules counts).
- Produces: Collection columns `theme_id` int|None FK themes.id ON DELETE SET NULL, `ai_paused` bool default False, `ai_tokens` int default 0.
- Produces: Collection `kind`-equivalent: the row kind is already derived from sources; add `"theme"` as a valid candidate source string (Task 3).

- [ ] **Step 1:** Write failing test: upgrade from 0097 → 0098 on an existing DB with a collection row; assert table `themes` exists, new Collection columns exist with defaults (`theme_id` NULL, `ai_paused` 0, `ai_tokens` 0), existing row untouched; downgrade round-trips.
- [ ] **Step 2:** Run `pytest tests/unit/test_migration_0098.py -q` → FAIL.
- [ ] **Step 3:** Write migration (`down_revision = "0097"`; use `op.batch_alter_table` for SQLite) and models.
- [ ] **Step 4:** Run test → PASS. Run `tests/unit/test_models*.py` (whatever exists for schema drift) → PASS.
- [ ] **Step 5:** Commit `feat(db): themes table and AI-row columns (#138)`.

---

### Task 2: Engine — `themes.py` (spec, hash, adapter, rules)

**Files:**
- Create: `shortlist/engine/themes.py`
- Test: `tests/unit/test_themes.py`

**Interfaces:**
- Consumes: `seasons.Season`, `seasons.CollectionRef`, `seasons.load_titles`, `models.RowLimits`, `limits.apply_limits(candidates, limits, tmdb)`.
- Produces:
  - `@dataclass(frozen=True) class ThemeSpec: slug:str; name:str; emoji:str|None; media:tuple[MediaType,...]; tags:tuple[int,...]; genres:tuple[str,...]; excluded_genres:tuple[str,...]; collections:tuple[str,...]; picks:tuple[ThemePick,...]; rules:RowLimits; min_votes:int|None`
  - `@dataclass(frozen=True) class ThemePick: tmdb_id:int; media:MediaType; origin:str; reason:str|None`
  - `theme_content_hash(spec: ThemeSpec) -> str` — stable sha1 over name-independent content (tags, genres, excluded, collections, picks ids, rules, media); name/emoji/reasons excluded so a rename alone does not rebuild… **except** name is included in recipe separately via `{theme}` naming.
  - `theme_as_season(spec: ThemeSpec) -> Season` — dateless Season (no `DateRule`) usable with `load_titles`. If `Season` requires a rule, build with the existing "custom, undated" form; check `Season` fields in `seasons.py:157` and adapt.
  - `load_theme(tmdb, plex, spec, library_index) -> ThemeTitles` — `ThemeTitles` wraps `SeasonTitles` plus `reasons: dict[(MediaType,int), str]`; applies `spec.rules` to `in_library` and `ids` using `apply_limits`; applies `min_votes` from item `vote_count`.

- [ ] **Step 1:** Failing tests, each its own function:
  - `test_theme_content_hash_is_stable_when_only_name_changes`
  - `test_theme_content_hash_changes_when_a_pick_is_added`
  - `test_load_theme_drops_titles_over_max_runtime` (fake tmdb `details` runtime 130 vs rule 120)
  - `test_load_theme_applies_year_rating_votes_rules`
  - `test_load_theme_keeps_ai_pick_only_if_in_library_for_in_library_set_but_keeps_id_in_ids`
  - `test_load_theme_raises_when_tmdb_fails` (so callers keep tonight's row)
  - hypothesis `test_theme_hash_order_independent` (tag/pick order shuffled → same hash).
- [ ] **Step 2:** Run `pytest tests/unit/test_themes.py -q` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(engine): themes load through the season path with hard rules (#138)`.

---

### Task 3: Engine — `theme` source, pool, recipe, naming, reasons

**Files:**
- Modify: `shortlist/engine/models.py` (`RowSpec.theme: ThemeSpec | None`, `RowSpec.ai_row`), `shortlist/engine/candidates.py` (~1044-1093 — a `theme` branch mirroring `season`), `shortlist/engine/rows.py` (`pool_key` ~2478, `row_recipe` ~980-1060, `_gather_pool` ~1325, naming placeholders), the placeholders module (`{theme}`, `{theme_emoji}` treated like `{season}`).
- Test: `tests/unit/test_themes_rows.py`; extend the pinned-recipe test module (the one covering phase 2 limits) with an "unchanged for non-AI rows" assertion.

**Interfaces:**
- Consumes: Task 2 (`ThemeSpec`, `load_theme`, `theme_content_hash`).
- Produces: `RowSpec.theme`; recipe fragment appended ONLY when `spec.theme` set: `theme=<slug>#<content_hash>`; `pool_key` includes `theme.slug, content_hash` only when set; reason text: seedless `Fits {name}, in genres you watch`; seeded `Fits {name} — like {seed}, which you watched`; when a theme pick carries an AI `reason`, the pick's reason is `"{ai_reason} · {personal hook}"` truncated to 160 chars.

- [ ] **Step 1:** Failing tests:
  - `test_recipe_unchanged_for_rows_without_a_theme` (pinned string from a current row)
  - `test_recipe_changes_when_theme_content_changes` / `..._not_when_only_name_changes`
  - `test_pool_key_differs_for_two_themes`
  - `test_theme_row_pool_limited_to_theme_members_and_ranked_by_genre_coherence`
  - `test_theme_row_fills_with_no_curator_calls` (curator mock `complete` never called)
  - `test_theme_row_keeps_current_picks_when_theme_load_raises`
  - `test_theme_pick_reason_includes_ai_line_and_hook`, `..._truncates_long_ai_reason`
  - `test_theme_title_placeholder_needs_a_run_to_render`
- [ ] **Step 2:** Run `pytest tests/unit/test_themes_rows.py -q` → FAIL.
- [ ] **Step 3:** Implement by copying the `season` source's integration points one for one; assert call kwargs for `load_theme` in tests.
- [ ] **Step 4:** PASS; also run the existing season + recipe test files to prove no regression.
- [ ] **Step 5:** Commit `feat(engine): the theme source fills an AI row per person with no AI (#138)`.

---

### Task 4: Theme authoring service

**Files:**
- Create: `shortlist/server/services/theme_author.py`
- Test: `tests/unit/test_theme_author.py`

**Interfaces:**
- Consumes: `curator.complete(system, user) -> str`, `taste_summary(profile, max_titles)`, `TmdbClient.search`, `search_keywords`, `genre_names`, `details`; Task 2 `theme_content_hash`, `load_theme`.
- Produces:
  - `ThemeDraft` dataclass: `spec: ThemeSpec`, `brief: str`, `stats: ThemeStats(named, resolved, in_library, after_rules, unwatched_median|None)`, `tokens: int`, `ai_reasons: dict[str,str]`.
  - `author_theme(*, brief, media, curator, tmdb, plex, library_index, profile=None, current: ThemeSpec|None=None, guidance: str="") -> ThemeDraft` — raises `ThemeAuthorError(message)` (plain-English) on empty/invalid AI output or provider none.
  - `diff_themes(old: ThemeSpec, new: ThemeSpec) -> ThemeDiff` — rules changed, `added: list[title]`, `removed: list[title]`, counts.
  - Prompt pieces: `BUILD_SYSTEM_GUIDANCE` (editable default guidance) and `BUILD_SYSTEM_MECHANICS` (locked: JSON-only schema `{name, emoji, rules:{max_runtime,min_year,max_year,min_rating}, tags:[str], genres:[str], titles:[{title,year,reason}]}`, ~60 titles, real titles only, reason ≤ 12 words). Final system = guidance + mechanics.

- [ ] **Step 1:** Failing tests with a fake curator returning canned JSON (including fenced ```json blocks):
  - `test_author_theme_resolves_titles_tags_genres`
  - `test_author_theme_drops_unresolvable_titles_and_counts_them`
  - `test_author_theme_enforces_runtime_rule_from_tmdb_not_from_ai`
  - `test_author_theme_raises_plain_error_on_invalid_json` / `..._on_empty` / `..._when_provider_none`
  - `test_author_theme_never_sends_account_names` (assert user message lacks `profile.display_name`)
  - `test_author_theme_sanitises_and_truncates_reason`
  - `test_refine_sends_current_theme_and_diff_lists_added_removed`
  - `test_shared_theme_sends_no_watch_history`
  - assert `curator.complete` call kwargs/positional: system contains mechanics text, user contains brief.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(server): author a theme from a brief with one AI call (#138)`.

---

### Task 5: API — themes, AI-row collection fields, usage, pause

**Files:**
- Create: `shortlist/server/api/themes.py` (register in the app router the same way `seasons.py` is)
- Modify: `shortlist/server/api/collections.py` (schema in/out: `theme_id`, `ai_paused`, `ai_tokens`; validate theme exists; duplicate-title check on the theme name; AI rows are created `enabled=False`), `shortlist/server/services/context_builder.py` (build `RowSpec.theme` from the Theme row), `shortlist/server/api/schemas.py` as needed.
- Modify: `web/openapi.snapshot.json` (regenerate; command in `tests/unit/test_openapi_snapshot.py`).
- Test: `tests/unit/test_api_themes.py`

**Interfaces:**
- Consumes: Task 4 `author_theme`, `diff_themes`; Task 1 models.
- Produces (owner-only, same auth as seasons):
  - `POST /api/themes/preview {brief, media, current_theme_id?, collection_id?, person_id?}` → `{draft: ThemeOut, stats, diff|null, tokens}`; does NOT save. 409 when the row's `ai_paused`; 422 when provider none.
  - `POST /api/themes {draft: ThemeIn}` → saved `ThemeOut` (hash recomputed server-side from content, never trusted from client).
  - `PUT /api/themes/{id}` hand edits (tags, genres, rules, picks add/remove) → recomputes hash.
  - `GET /api/themes/{id}`.
  - `POST /api/collections/{id}/ai-pause {paused: bool}`.
  - Token usage added to `Collection.ai_tokens` and recorded in an `events` row (`theme.build`, diff + tokens, never keys) on save.
  - Provider-none: `GET /api/themes/capabilities` → `{ai: bool}` so the UI hides the AI half.

- [ ] **Step 1:** Failing tests: preview returns draft and persists nothing; save recomputes hash; paused → 409 with plain message; provider none → 422; collection create with theme sets `enabled=False`; duplicate theme name vs. existing collection title rejected with the existing validator's message; event row written with tokens; owner-only (non-owner 403).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Regenerate snapshot, run `tests/unit/test_openapi_snapshot.py`. Commit `feat(api): themes, preview, AI pause and usage (#138)`.

---

### Task 6: Frontend — kind, templates, editor, "Try it"

**Files:**
- Modify: `web/src/lib/row-kind-meta.ts` (add `"ai"` kind), `web/src/lib/row-templates.ts` (templates "Describe a row", "AI Picks" under an "AI" group/chip; "AI Picks" is the same fixed-theme kind in this phase and is marked "Explores themes — coming next" ONLY if phase 4 is not shipped; otherwise omit the template until phase 4), `web/src/components/rows/row-template-gallery.tsx`, `web/src/components/rows/row-editor.tsx` (+ new `ai-row-section.tsx`, `ai-prompts-section.tsx`), `web/src/lib/collections.ts`, `web/src/lib/themes.ts` (query hooks).
- Regenerate API types from the snapshot (`pnpm -C web gen:api` against the snapshot, not localhost:5959).
- Test: `web/src/lib/__tests__/themes.test.ts`, `web/src/components/rows/__tests__/ai-row-section.test.tsx`

**Interfaces:**
- Consumes: Task 5 endpoints and generated types.
- Produces: What goes in → "Describe it" textarea, "Build the list" outline button, preview card (counts, sample with origin badges, missing-from-server count), "Change it" box + diff (rules diff, +/− titles, new count) with Keep / Discard, hand-edit fields when no AI provider; "Try it" jump section = scoped dry run (`POST /api/runs {dry_run:true,user_ids:[p],collection_ids:[id]}`) showing picks + reasons; "AI prompts" section (editable guidance for Build/Change it, locked mechanics shown read-only; "Next theme" listed as phase 4 only if shipped); usage line "Used N tokens on this row"; "Pause AI for this row" switch. Save changes / Add row remains the single amber control. New AI rows start disabled with a note.

- [ ] **Step 1:** Failing vitest: hook builds preview and caches by brief; section renders loading / error (message + retry) / empty (explains what to do) / success; diff renders added/removed; AI half hidden when `capabilities.ai` false; paused disables Build/Change with the plain reason; template creates a disabled AI row; `rowOverrides` badge logic includes the AI badge only when AI instructions apply (fold in phase 1 leftover: "AI instructions" badge only while web search on).
- [ ] **Step 2:** Run `pnpm -C web test -- ai-row-section themes` → FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** `pnpm -C web exec tsc -b --force` clean. Commit `feat(web): AI row editor, templates and Try it (#138)`.

---

### Task 7: Docs, changelog, design-doc status, e2e

**Files:**
- Modify: `.claude/docs/discussion-138-ai-custom-rows.md` (Status line), `CHANGELOG.md` (Unreleased), `docs/guides.md` + `docs/reference.md` (AI row, API endpoints), `README.md` features list; regenerate `docs/llms-full.txt` (`python scripts/build_llms_full.py`).
- Create: `tests/e2e/test_ai_row.py` (fake_plex + a fake curator returning canned JSON): add AI row from the gallery, build, preview, save, Try it shows picks.

- [ ] **Step 1:** Write the e2e test; run `pytest -m e2e tests/e2e/test_ai_row.py` after `pnpm -C web build` → FAIL then PASS after fixes.
- [ ] **Step 2:** Docs per `.claude/rules/docs.md`; run `tests/unit/test_llms_full.py`.
- [ ] **Step 3:** Commit `docs: AI row (#138)`.

---

### Task 8: Whole-phase verification (once)

- [ ] `pytest`, `pnpm -C web test`, `pnpm -C web exec tsc -b --force`, `pnpm -C web exec eslint .`, `pnpm -C web build`, `ruff check . && ruff format --check .`, `pytest -m e2e`. One pytest at a time.
- [ ] Final whole-branch review (opus), then Architecture Review (opus) — migration + Plex-adjacent. Fix HIGH findings.
- [ ] Merge `origin/dev`, regenerate OpenAPI snapshot + `docs/llms-full.txt` if conflicted, re-verify, push `HEAD:dev`, wait for CI, upgrade sflix, prove live with scoped real run on a throwaway MooHouse row (snapshot every account's share filters before/after, byte-compare, delete row).
