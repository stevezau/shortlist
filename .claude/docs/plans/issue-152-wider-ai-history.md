# Issue #152: give AI discovery a wider view of each person's history

Owner approved the whole design on 2026-10-11 (mockup: claude.ai/artifact/Bo8RpAnUcXq9qyZWgKq1jg).
Work in three phases. Nothing user-visible ships until the replay (phase 2) says which variants win.

## Evidence that shaped this (production, read-only, 2026-10-11)

- Production runs `llm_web` via Exa (structured extraction) + Anthropic picking ~35-40 of up to 300 extracted
  titles. The pick prompt's taste text is `taste_summary(profile)`: the 20 newest distinct titles of the WHOLE
  history (all libraries/media), labelled "recently enjoyed" in the system prompt.
- 56 first watches over 6-10 Oct: Exa's extraction for their 20 newest titles held 9; the AI picked 2. 9 more
  appeared only in extractions of older titles (only those with a cached search). 4 of those came from the 20
  most-rewatched older titles.
- `scripts/replay_eval.py` never ran the AI (EngineConfig() = tmdb_similar only; no curator, no profile). Of its
  230 held-out cases, 99 were shows already started (dated from `watch_events`) and 17 movie rewatches.
- Ratings: 16 trusted-rating accounts, max 8 hand-set ratings each. Cheap to include, will not move numbers here.
- Today's `taste_summary` also lists "Don't seed" titles and 1-star titles (it filters nothing).

## Phase 1: engine + replay (no user-visible change; defaults reproduce today)

### 1. `shortlist/engine/taste.py` (new, pure)

```python
@dataclass(frozen=True)
class TasteProfile:
    recent: list[WatchedItem]      # newest first, <= recent_limit
    favourites: list[WatchedItem]  # strongest first, <= favourite_limit
    rated_high: list[WatchedItem]  # trusted human rating >= 8 (4 stars), <= rated_limit together with rated_low
    rated_low: list[WatchedItem]   # trusted human rating <= the dislike threshold
    def render(self) -> str: ...
    def search_candidates(self) -> list[WatchedItem]: ...  # rated_high first, then favourites (dedup)

def build_taste(history, *, blocked: set[int], ratings: RatingsPolicy | None,
                recent_limit=12, favourite_limit=12, rated_limit=6) -> TasteProfile
```

Rules:
- One entry per (title, media_type), as `distinct_recent`. A title held in two libraries: watched_at = newest;
  movie play count = sum of `watch_count`; show progress = the copy with the highest `viewed_leaf_count`.
- `blocked` ("Don't seed", tmdb ids) are dropped from every list.
- Ratings count only when `ratings` is given, `ratings.enabled` and `ratings.trusted`, and the item
  `is_human_rating`. `rated_low` = rating <= `ratings.threshold`; `rated_high` = rating >= 8. A rated title appears
  ONLY in its rated list (never also in recent/favourites). `rated_low` newest first; `rated_high` highest rating
  then newest. Together capped at `rated_limit` (low first, they are the rarer and more useful signal — cap low at
  half when both overflow).
- `recent` = newest-first of what remains, up to `recent_limit`.
- `favourites` from what remains after `recent`: a movie with play count >= 2, or a show that `is_finished` or
  has >= 20 episodes watched. Ordered per media type (movies: plays desc then newest; shows: finished first, then
  episodes watched desc, then newest), then interleaved movie/show so neither scale dominates.
- `render()` (exact text; empty sections omitted; titles truncated at 80 chars):

```
What this person has watched. Watching a title does not mean they liked it; their own ratings, where given, do.

Watched recently (newest first):
- Slow Horses (2022) - show, 6 of 30 episodes so far
- Anora (2024) - film

Long-time favourites (rewatched, finished, or watched at length):
- Heat (1995) - film, watched 3 times
- The Expanse (2015) - show, finished

Their own Plex ratings:
- Rated highly: Arrival (2016), 5 stars
- Rated low: Conclave (2024), 1 star
```

  Detail strings: film -> `film` / `film, watched N times` (N>=2, "twice" for 2). Show -> `show, finished` /
  `show, N of M episodes so far` / `show, N episodes` (total unknown). Stars = rating/2, "4.5 stars", "1 star".
  Year omitted when None. Empty history -> `"- (no history yet — recommend broadly popular titles)"` under the
  intro line.

### 2. Prompts (`shortlist/engine/curator/base.py`)

- `_WebPrompt` gains `guide_wide`: the same text as `guide` with "what this person recently enjoyed/watched"
  replaced by "this person's viewing history". `_web_system(..., wide: bool)` picks it. `builtin_guidance` and
  `builtin_template` show the WIDE guide (what most rows send). Owner `replace`/`extra` text is untouched.
- `build_web_prompt`, `build_web_rag_prompt`, `build_web_pick_prompt` take `taste: str | None = None`.
  None -> byte-identical to today (system and user). A string -> wide guide, and the taste text replaces
  `taste_summary(...)` (pick/RAG) or the `They recently enjoyed:` block (native). Native keeps its trailing
  "Search the web ... Favour things released in ..." sentence unchanged.
- The three providers' `recommend_web(..., taste: str | None = None)` pass it through. `taste_summary` stays
  (theme rows #138 and the None path use it).

### 3. Gather plumbing (`shortlist/engine/candidates.py`)

- `gather_candidates(..., taste: str | None = None, favourite_seeds: list[Seed] | None = None,
  favourite_count: int = 0)` -> `web_recommendations` -> `_web_via_search` / `recommend_web`.
- External path: `searched = seeds[:recent_count]` then append up to `favourite_count` of `favourite_seeds`
  whose (tmdb_id, media_type) is not already searched. Same cache key/shape. Each trace query gets
  `"kind": "recent" | "favourite"`. `_drop_seed_titles` also drops favourite titles.
- `GatherStats.web_found: list[TitleCandidate]` — the full deduped extraction (not persisted, not in trace), so
  the replay can tell "found by search" from "picked by the AI".
- Native path ignores favourites (the model chooses its own searches) but gets `taste`.

### 4. Rows (`shortlist/engine/rows.py`)

- `EngineConfig.taste_mode: str = "recent"` ("recent" = today, "wide" = TasteProfile) and
  `EngineConfig.favourite_count: int = 0`. Phase 3 flips/wires these from settings.
- Per row, when `"llm_web"` is in its sources: single-seed row (`effective_max_seeds(spec, cfg) == 1`) or
  `taste_mode == "recent"` -> `taste=None`, no favourites (today's behaviour exactly). Otherwise
  `build_taste(_history_for_row(...), blocked=user.blocked_seeds, ratings=self.ratings or None for SHARED users)`
  -> `taste=profile.render()`, `favourite_seeds` = `search_candidates()` resolved to Seeds (skip unresolvable).
  Memoise per seeds_for key.
- `pool_key` adds `(taste_is_wide, favourite_count)` only for llm_web rows (0/False otherwise, so no key changes
  for anyone tonight).

### 5. Replay (`shortlist/engine/eval/replay.py`, `scripts/replay_eval.py`)

- `holdout_cases(..., keep: Callable[[WatchedItem], bool] | None = None)` filters BEFORE taking the newest N.
  Movie rewatches (`watch_count > 1`) are excluded in the engine; the script supplies "show started > 1 day before
  this watch" from `watch_events` (first `viewed_at` per account + show_rating_key).
- `replay_case` builds `UserProfile(history=history_before, ...)` (never the full history: a test asserts the
  held-out title is absent from every prompt), passes profile, curator, search, web_search_mode,
  web_search_cache, recent_count, taste/favourites built with the SAME helper rows use.
- `ReplayOutcome` gains `found_by_search` (held-out normalised title in `stats.web_found`), `ai_picked`
  (held-out key in `trace["web"]["proposals"]`), `tokens`, `new_searches`.
- Script arms: A (recent, F=0), B (wide, F=0), C (wide, F=8), D (wide, F=0, curator wrapped with
  `can_complete=False` so code ranks everything Exa found). A/B/C run twice. Report: per-case table of the
  furthest stage reached, per-arm stage counts, tokens, new searches, paired better/worse/same on "AI picked"
  (B vs A, C vs B) with agreement vs `TRUST_THRESHOLD`, and the A-vs-A' rerun gap as the noise floor.
  Parallel over users (ThreadPoolExecutor, 4).

### Tests (write first)

taste: size caps, show collapses to one line with progress, two-library copy merged, blocked dropped, rated only
when trusted+enabled+human, low-rated never in favourites/recent, interleave, render exact text, empty history.
prompts: taste=None byte-identical for all three shapes; wide guide used with taste; owner replace untouched.
candidates: favourites appended after recent, deduped, capped, trace kind, web_found populated.
rows: single-seed row sends taste=None; recent mode sends None; pool_key unchanged for non-web rows.
replay: held-out title never in the prompt; keep filter applied before the cap; rewatches skipped.

## Phase 2: measure (owner approved ~2.6M tokens + up to ~900 Exa searches)

Run the branch image as a one-off container on the plex host against a COPY of /config (sqlite backup +
secret.key, kept on that host, deleted afterwards). Nothing touches the live DB or Plex.

## Phase 3: ship what won

- B wins (AI picked up vs A, agreement >= 70%, in-row not worse) -> `taste_mode` default "wide", no setting.
- C beats B the same way -> `recommendations.favourite_count` setting (default 0), row + per-person override,
  migration, API/OpenAPI, Settings field under "Watches the AI web search looks up", row editor, user row card,
  trace chips ("recent"/"favourite"), docs (settings.md, guides), assistant tools that list recent_count.
- D -> numbers in the issue only.
- Architecture Review before pushing (reads watch history + ratings). Issue comment with the numbers.
