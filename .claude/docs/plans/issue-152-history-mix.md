# Issue #152 phase 2: the "history mix" — favourites + older watches in AI web search

Owner approved 2026-10-11 (workflow page: claude.ai/artifact/RihWpPUFFrfoeXeoLpSNEH, v3). Builds on
`.claude/docs/plans/issue-152-wider-ai-history.md` (already shipped in 202a803e: `shortlist/engine/taste.py`,
`TastePrompt`, `taste_and_favourites`, `recent_taste`, favourites plumbing in `candidates._web_via_search`).

## What the owner sees

Three groups feed a row's AI web search. A title sits in ONE group only:

- **Recent** — newest watches, `recent_count` (exists today, unchanged).
- **Long-time favourites** (new count, 0–10) — `TasteProfile.search_candidates()`: rated ≥4 stars (trusted human
  ratings), then movies played ≥2, shows finished or ≥20 episodes. Strongest first. Fixed week to week.
- **Older watches** (new count, 0–10) — everything else before the recent ones, not a favourite, not rated low,
  not "Don't seed", not a DROPPED show (show, not finished, fewer than 3 episodes watched), and watched within
  the look-back window. Sampled evenly across their history, rotating weekly (algorithm below).

Both counts default 0 = byte-identical to today. Look-back: `recommendations.older_lookback_years`, 0 = any time
(server level only), allowed values 0, 1, 3, 5.

UI tunes it with presets that only set the two new counts (recent_count stays its own field):
Recent only 0/0 (default) · A little older 3/3 · Balanced 6/6 · Deep 10/10 · Custom (two number fields).
Levels: server setting → row (`collections`) → person-on-row override (`person_row_overrides`), each nullable =
inherit, exactly like `recent_count`.

## Engine

### taste.py

- `build_taste(..., older_limit=0, now: datetime | None = None, lookback_years: int = 0)` adds
  `TasteProfile.older: list[WatchedItem]`. Pool = merged titles left after recent + rated + ALL favourite
  candidates (not just the rendered 12) — so a title can never be both. Excludes dropped shows
  (`media_type is SHOW and not is_finished and _episodes_watched < 3`) and, when `lookback_years > 0`,
  items with `watched_at < now - lookback_years*365 days`.
- Sampling (`_spread_sample(items, n, week)`): sort pool oldest→newest; split into `n` contiguous chunks of
  near-equal size (`chunk i = items[i*len//n : (i+1)*len//n]`, skip empty); from each take
  `chunk[(week + i) % len(chunk)]`. If `len(pool) <= n`, take all. `week = int(now.timestamp() // (7*86400))`.
  Result ordered newest first.
- `render()` adds section `"Also watched over the years (a sample):"` after favourites, same line format.
- `search_candidates()` unchanged (favourites). New `older_candidates()` returns `older`.
- `taste_and_favourites(...)` → rename/extend to `history_mix(history, *, blocked, ratings, resolve, favourites:
  int, older: int, now, lookback_years) -> HistoryMix` where
  `HistoryMix(NamedTuple): taste: TastePrompt; favourite_seeds: list[Seed]; older_seeds: list[Seed]`.
  `favourite_limit` for rendering = max(12, favourites)… keep the rendered favourites list = the first
  `max(favourites, 6)` candidates; rendered older = the sampled `older`. Keep a thin `taste_and_favourites` only
  if the replay still needs it; otherwise migrate the replay to `history_mix`.

### candidates.py

- Add `older_seeds: list[Seed] | None`, `older_count: int = 0` beside the favourite params through
  `gather_candidates` → `web_recommendations` → `_web_via_search`.
- `searched` = recent, then favourites (dedup vs searched), then older (dedup vs searched). Trace `kind`:
  `"recent" | "favourite" | "older"`. `_drop_seed_titles` drops favourite + older titles too.
- **Fair share (structured path only, i.e. Exa + AI):** track each candidate's group = the group of the FIRST
  search kind that returned it, with priority recent > favourite > older (a title any recent search found is
  "recent"). When any non-recent search ran, `build_web_pick_prompt` gets `groups: list[tuple[str, list[TitleCandidate]]]`
  instead of one flat list, and prints them under headings:
  `Titles recommended for their recent watches:` / `...for their long-time favourites:` /
  `...for things they watched further back:` and appends
  `Take roughly N from the recent list, F from the favourites list, O from the further-back list, unless a list
  has fewer that suit them.` where shares are `round(k * count/total_searches)` per non-recent group and recent
  gets the remainder; omit a group with 0 searches or 0 candidates (its share goes to recent). The 300 cap
  (`_WEB_PICK_CAP`) applies per group proportionally (each group keeps at least its share of the cap) —
  simplest correct form: cap each group at `ceil(300 * searches_g / total)`.
  With no non-recent searches the prompt is byte-identical to today (existing tests pin this).
- RAG (SearXNG) and no-AI (`_titles_as_proposals`) paths: no prompt change; interleaving already gives each
  search its turn. Native path: unchanged except the wide taste text (already wired).
- `GatherStats` unchanged; `web_trace["shares"] = {"recent": n, "favourite": f, "older": o}` when fair share used.

### rows.py

- `RowSpec.favourite_count`, `RowSpec.older_count` (`int | None`, inherit); `PersonRowOverride` (whatever the
  engine-side override dataclass is called — same place `recent_count` lives) gets both too.
- `EngineConfig`: drop `taste_mode`; keep `favourite_count: int = 0`; add `older_count: int = 0`,
  `older_lookback_years: int = 0`. Update the comment (now real settings).
- `RowPolicy.effective_favourite_count(spec)` / `effective_older_count(spec)`: override → spec → cfg, like
  `effective_recent_count`.
- `uses_wide_taste(spec)` = (fav > 0 or older > 0) and llm_web on and `effective_max_seeds != 1`. Shared rows:
  check how a shared row builds its policy; it must NOT widen (no favourites, no older, no ratings). Add a test.
- `taste_for` returns a `HistoryMix`-like triple; memo key adds (fav, older). `now` = the run's clock if the
  context has one, else `datetime.now(UTC)`; lookback from cfg.
- `pool_key`: append `("mix", fav, older)` only when wide (non-wide keys unchanged).

### replay

- `scripts/replay_eval.py` / `engine/eval/replay.py`: arms configurable by (fav, older). Keep A (0/0). Add
  `found_by_group` per case (which search kind's extraction held the held-out title) so one Exa-only run with
  10/10 answers "would F favourites / O older have found it" for every F,O ≤ 10. An `EXA_ONLY=True` knob that
  skips the AI (uses `_NoAi`) for this step. Budget for this session: ≤ 500 new Exa searches (cap via
  `MAX_CASES`).

## Server

- Migration `0112_history_mix.py`: nullable Integer `favourite_count`, `older_count` on `collections` and
  `person_row_overrides`. Stamp with `scripts/check_migration_freeze.py` per its instructions.
- Settings: `recommendations.favourite_count` 0, `recommendations.older_count` 0,
  `recommendations.older_lookback_years` 0 — `settings_store` defaults, `catalogs/settings.py` (labels/help,
  next to recent_count), `settings_validation` (`_bounded_int(0, 10)`; lookback ∈ {0,1,3,5}).
- Engine config wiring wherever `recommendations.recent_count` → `EngineConfig.recent_count` happens.
- Row editing (`services/row_editing.py` field `ge=0, le=10`, None = inherit; serialisers), `api/collections.py`,
  `api/user_rows.py` (effective + override values, as recent_count), `services/person_row_overrides.py`
  (non-default when not None), catalogs/templates.py (None for both).
- Assistant tools: wherever recent_count is exposed (`assistant/tools.py`, `people_seasons.py`,
  `row_adapter.py`) expose the two new fields the same way.
- New endpoint `GET /api/users/{user_id}/history-mix?collection_id=` → the person's mix for that row this week:
  `{recent: [title…], favourites: [...], older: [...], favourite_count, older_count}` — builds the row's
  `history_mix` from a live history read (`profile_with_history`) the same way a run does, items as
  `{title, year, media_type, tmdb_id}`. 404 unknown user/collection; 502 redacted on Plex error (copy `/history`).
  Lazy: the UI fetches it only on demand.
- OpenAPI snapshot + `web/src/lib/api-schema.d.ts` regenerated (find the repo's generator script).

## Web

- `HistoryMixField` component (next to `recent-count-field.tsx`): segmented presets + Custom revealing two
  number inputs; an `inherit` variant ("Server default (Balanced)" / "Row default (…)") for row editor and person
  card. Used in Settings → Recommendations (under "Watches the AI web search looks up", with a mode line naming
  the configured search setup: native / Exa+AI / Exa no AI / SearXNG), row editor contents fields, user row card.
- Settings: "Older watches from" select (Any time / Last 5 years / Last 3 years / Last year).
- User row card: "Show this week's mix" button → three lists (recent first 5 + "+N more", favourites, older)
  each non-recent title with "Don't use" = existing blocked-seeds POST (undo = DELETE).
- How we picked (trace view): search chips show the kind (recent / favourite / older); show the shares line.
- Rules: `.claude/rules/frontend.md`. Test at 320/1024/1280.

## Docs

`docs/reference/settings.md` (three settings), `docs/reference/api.md` (endpoint + fields), guides where
recent_count is explained, CHANGELOG Unreleased → Added. `python scripts/build_llms_full.py`.

## Tests (write first)

taste: older excludes favourites/recent/rated-low/blocked/dropped shows; lookback; spread sample covers the span
and rotates by week; render section. candidates: three kinds ordered + deduped + trace kind; fair-share prompt
groups and numbers; 0/0 byte-identical. rows: effective counts precedence; single-seed and shared rows never
widen; pool_key unchanged at 0/0. server: settings validation, row edit round-trip, override round-trip,
migration up/down, history-mix endpoint (fake plex). web: preset ↔ numbers, inherit, mix view.
