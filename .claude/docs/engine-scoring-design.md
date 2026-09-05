# Engine scoring changes design (Wave 3)

From the September 2026 audit — see `.claude/docs/audit-2026-09-programme.md`. Five scoring changes,
grounded in a full read of `ranking.py`, `candidates.py`, `history.py`, `models.py`,
`clients/tmdb.py`, `picker.py`, and `rows.py`'s `_candidate_pool` call site.

**Gate:** none of these ship without the replay harness (`.claude/docs/eval-harness-design.md`) —
every constant below is reasoned, not validated.

## Cross-cutting: one consolidated TMDB detail call

Changes 1, 4 and 5 all need per-title TMDB data beyond what list responses carry (genres ride free on
list responses via `genre_ids`; franchise membership and cast do not). Today `genre_ids_for`
(`clients/tmdb.py:148`) makes a bare detail call just to read `genres`. TMDB's `append_to_response`
folds `/credits` into that same request, and `belongs_to_collection` already rides on the bare detail
payload. So **Change 1 introduces one consolidated method** and 4/5 reuse it, rather than three
divergent per-title fetches.

```python
# clients/tmdb.py
def details(self, tmdb_id: int, media_type: MediaType) -> dict:
    """One title's genres, franchise collection and top cast, in a single cached call.

    append_to_response=credits folds the /credits sub-resource into the request TMDB would
    otherwise need a second round-trip for; genres and belongs_to_collection already ride on the
    bare detail response. One cache entry serves genre, franchise and cast lookups alike.
    """
    kind = "movie" if media_type is MediaType.MOVIE else "tv"
    return self._get(f"/{kind}/{tmdb_id}", params={"append_to_response": "credits"})


def genre_ids_for(self, tmdb_id: int, media_type: MediaType) -> list[int]:
    data = self.details(tmdb_id, media_type)
    return [g["id"] for g in data.get("genres", []) if isinstance(g, dict) and "id" in g]
```

`_get`'s cache key is `path + urlencode(sorted(params))` (`clients/tmdb.py:51`), so adding
`append_to_response` changes the key — old `/movie/{id}` rows age out on the existing 7-day TTL
rather than colliding. No migration; it is an HTTP cache, not a DB table.

---

## Change 1 — Measured genre avoidance

**Where:** new functions in `shortlist/engine/candidates.py`, beside the existing `genre_coherence`
(line 515) — that file already owns "resolve genre ids for a seed via TMDB, turn them into a shade".

### Shrinkage formula

Empirical-Bayes shrinkage toward the pool's own share, strength scaling automatically with sample
size — **not** a flat additive constant:

```
w(n) = n / (n + K)                          # how much to trust the user's OWN data
user_share_shrunk(g) = w(n) * user_raw_share(g) + (1 - w(n)) * pool_share(g)
log_ratio(g) = clamp(log2(user_share_shrunk(g) / pool_share(g)), -CLAMP, +CLAMP)
```

`n` = the number of the user's genre-tagged titles the profile is built from.

**Why not the flat constant.** A flat additive term (`log2((userShare + eps)/(poolShare + eps))`, or
a fixed pseudo-count added regardless of `n`) either barely touches a noisy 3-watch estimate
(under-smooths) or, tuned large enough to fix that, swamps a genuinely-informative 30-watch estimate
(over-smooths) — because it does not know how much real evidence it is up against. `w(n) = n/(n+K)`
does: at `n=3` it trusts the user for `3/13 ≈ 23%`; at `n=30` (the seed cap) for `30/40 = 75%`. One
constant, automatically adaptive.

```python
GENRE_SHRINK_K = 10.0
GENRE_LOG_CLAMP = 2.0  # ±4x ratio — beyond this a sparse genre stops adding real signal


def genre_avoidance_profile(user_counts: dict[str, int], pool_counts: dict[str, int]) -> dict[str, float]:
    """Per-genre shrunk log2(userShare/poolShare), clamped. Positive = over-represented in the
    user's history, negative = avoided. Only the negative half is used downstream (Change 2) — this
    function stays neutral about whether liking should ever be a bonus."""
    n = sum(user_counts.values())
    pool_total = sum(pool_counts.values())
    if n == 0 or pool_total == 0:
        return {}
    w = n / (n + GENRE_SHRINK_K)
    out = {}
    for genre, pool_n in pool_counts.items():
        pool_share = pool_n / pool_total
        if pool_share <= 0:
            continue
        user_share = user_counts.get(genre, 0) / n
        shrunk = w * user_share + (1 - w) * pool_share
        out[genre] = max(-GENRE_LOG_CLAMP, min(GENRE_LOG_CLAMP, math.log2(shrunk / pool_share)))
    return out


def candidate_genre_penalty(genres: list[str], profile: dict[str, float]) -> float:
    """This candidate's own avoidance signal — the mean of its genres' log ratios, negative half
    only. Genres absent from the profile (unknown to us) are neutral (0.0), never penalised."""
    if not genres or not profile:
        return 0.0
    values = [min(0.0, profile.get(g, 0.0)) for g in genres]
    return sum(values) / len(values)
```

`user_counts` is built from the row's **seeds** (already TMDB-genre-resolved for
`tmdb_similar`/`tmdb_discover` via `_seed_genre_ids`/`_dominant_genre_ids`), not raw `WatchedItem`
history — `WatchedItem` carries no genre field, and seeds are already this codebase's operational
proxy for "this person's taste". This also caps `n` at `max_seeds` (default 30), so a heavy
watcher's `n` saturates at the seed cap, not their raw watch count.

### The pool baseline — reasoned, not the candidate pool

**Not the pre-ranked candidate pool.** It is built FROM this user's own seeds
(`/recommendations`, `/similar`, discover-genres widened from their history) — comparing a user's
taste against a population that echoes their taste back is circular, and dilutes exactly the signal
being measured (an avoided genre is also under-represented in candidates seeded from that avoidance).

**Not the whole server's library either.** On a multi-library server different people see different
libraries (kids vs adult sections). Comparing a kid's genre share against a catalogue containing
horror libraries they cannot see manufactures a spurious "avoids horror" signal for every such user.

**Use this user's own accessible library sections** — the same `library_index` already built per row
in `rows.py` (`_candidate_pool`, `_media_filter`). It is the real, independent "what is actually on
offer to this person" population, already computed for `filter_candidates`, so reusing it costs no
new plumbing — only a new aggregation over it.

**Open question that cannot be settled from the code** (must be resolved before implementation, per
plex-safety rule 11): whether plexapi's `section.all()` items carry `.genres` inline (free, same scan
`build_library_index` already does) or need a per-item re-read the way `.labels` does. No fixture in
`tests/fixtures/` records a bulk listing with `<Genre>` tags. Design for both:

- **Genres free on the listing:** extend `PlexClient.build_library_index` to also return a
  `Counter[str]` of genre tags in the same scan — zero extra PMS calls.
- **Not free:** compute the library genre profile via `tmdb.genre_ids_for` per `tmdb_id` in the
  index, cached as an **aggregate** per section key (`library_genre_profile:{section_key}`, TTL ~7
  days matching `CACHE_TTL_S`), computed once and shared across every user's run — never recomputed
  per user per row.

### Constant

`GENRE_SHRINK_K = 10.0` — on the order of half of TMDB's ~19 movie genres. A couple of watches move
the estimate meaningfully without one title dominating it; a full 30-seed history is trusted at 75%.
**Not validated against live data** — check against the real roster before shipping the default.

### Tests required first

1. `test_shrinkage_pulls_a_low_watch_count_user_toward_the_pool` — n=1–3, shrunk share close to
   `pool_share`, far from the raw noisy share.
2. `test_shrinkage_trusts_a_high_watch_count_users_own_data` — n=30, shrunk close to raw.
3. `test_log_ratio_is_zero_when_shares_match`.
4. `test_log_ratio_is_clamped` — near-zero pool share cannot blow past `GENRE_LOG_CLAMP`.
5. `test_only_negative_ratios_count_as_avoidance` — `candidate_genre_penalty` never positive.
6. `test_a_genre_absent_from_the_profile_is_neutral` — no `KeyError`, penalty 0.0.
7. Hypothesis property: for any `n >= 0`, `shrunk_share` always lies between `raw_share` and
   `pool_share` (monotonic interpolation invariant).
8. `test_gather_candidates_never_reads_the_candidate_pool_as_the_baseline` — asserts the wiring (per
   testing.md's "assert the kwargs the SUT controls"), since the whole design leans on it.

### What could regress

A wrongly-tuned `K` or clamp suppresses a nascent-but-real interest before there is evidence —
caught by test 1. A circularity bug (baseline sourced from the candidate pool) silently weakens every
future genre signal — caught by test 8. Cost regression (recomputing the library baseline per user
per run) needs a cache-hit assertion test.

### Settings / migration / UI

No migration. New setting `recommendations.genre_avoidance` (float 0..1, dial matching `recency`'s
UX), **default 0.0 in both `EngineConfig` and the `settings_store` seed**. Per the "my taste is not
the default" memory this does NOT get `recency`'s existing-installs-default-to-0.5 treatment — that
was an explicit prior product decision, not a precedent to presume onto a new signal. UI: one slider
under Settings → Recommendations.

---

## Change 2 — A single negative multiplier, floored once

**Where:** `shortlist/engine/ranking.py`, beside `recency_factor`, consumed by `score()` (line 70).

**Current:**
```python
def score(candidate: Candidate, *, recency: float = 0.0, year_now: int = 0) -> float:
    seed_weight = max((s.weight for s in candidate.seeds), default=0.0)
    rating = candidate.rating or 5.0
    base = (1 + candidate.seed_frequency) * rating * (1.0 + seed_weight) * candidate.affinity
    return base * recency_factor(candidate.year, year_now, recency)
```

### The trap, and how this avoids it

Three independent dampeners each floored at 0.25 and multiplied give `0.25³ = 0.0156` — a 1.6% floor
nobody chose. The fix: **combine every dampener in log space, then apply exactly one floor to the
combined total** — not one floor per dampener.

```python
NEGATIVE_MULTIPLIER_FLOOR = 0.5  # matches genre_coherence's existing 0.5 floor (candidates.py:515):
# "shades the ranking rather than dominating it" — same number,
# same reason, same file family.


def negative_multiplier(*log2_penalties: float) -> float:
    """Combine every negative-affinity signal into ONE floored multiplier.

    Each penalty is a log2-domain adjustment <= 0 (0 = no penalty). Summed in log space — the
    log-domain equivalent of multiplying the raw ratios — then floored ONCE, after combining. Three
    dampeners therefore never compound below NEGATIVE_MULTIPLIER_FLOOR the way three separately
    floored factors would; the floor is one explicit auditable clamp, not an emergent property of
    how many signals happen to be stacked.

    RULE: a future dampener is ANOTHER ARGUMENT to this function, never its own floored multiplier.
    """
    total = sum(min(0.0, p) for p in log2_penalties)
    return 2 ** max(total, math.log2(NEGATIVE_MULTIPLIER_FLOOR))
```

New line in `score()`:
```python
base *= negative_multiplier(candidate.genre_penalty * genre_avoidance_strength)
```

At `genre_avoidance_strength=0.0` (every install's default) the argument is always `0.0` and
`negative_multiplier(0.0) == 2**0 == 1.0` **exactly** — `score()` is byte-identical to today.
`rating`, `seed_weight`, `affinity` and `recency_factor` are untouched; the new term occupies exactly
the shape `recency_factor` already does.

### Tests required first

1. `test_negative_multiplier_is_one_with_no_penalties_or_a_zero_penalty`.
2. `test_multiple_dampeners_combine_before_the_floor_not_after` — **the trap test**: three penalties
   that would each independently floor to 0.25, asserted to combine to `>= 0.5`, not `0.25**3`.
3. `test_the_floor_is_the_named_constant_however_many_penalties_pile_on`.
4. `test_score_with_no_penalty_matches_todays_formula_exactly` — pins bit-for-bit equality against
   pre-change output for the existing `TestScore`/`TestAffinityBeatsRating` fixtures.
5. `test_a_penalised_candidate_never_reaches_zero` — mirrors the file's own
   `test_a_seedless_candidate_is_not_worthless`.

### What could regress

A future third dampener bolted on as its own separately-floored multiplier instead of another
argument reintroduces the 1.6%-floor bug silently. Caught only if test 2's pattern is copied for every
addition — which is why the docstring states it as a rule.

### Settings / migration / UI

None beyond Change 1 — pure ranking plumbing, gated by the same strength.

---

## Change 3 — Per-signal attribution

**Where:** new dataclass in `shortlist/engine/models.py` (beside `Seed`/`Candidate`); consumed in
`picker.py`'s `reason_for` (line 34).

`Candidate.top_seed` (models.py:210) and `reason_for`'s single-cause wording must **not change** —
`Pick.seed_tmdb_id`/`seed_title` and the `{top_seed}` row-name template both depend on it, and
`test_picker.py::TestReasonFor` pins the exact wording.

```python
@dataclass(frozen=True)
class Attribution:
    """One signal's strongest evidence for a candidate — which watched title, and why."""

    signal: str  # "similarity" | "franchise" | "cast"
    seed_title: str
    seed_tmdb_id: int
    detail: str = ""  # the shared actor's name, or the franchise name


# Candidate gains:
attributions: list[Attribution] = field(default_factory=list)
```

Each signal appends its own `Attribution` where it is computed: `tmdb_similar`'s seed match in
`gather_candidates`'s `add()`; Change 4's franchise pass appends
`Attribution("franchise", seed.title, seed.tmdb_id, collection_name)`; Change 5 appends
`Attribution("cast", …, actor_name)` for the single highest-IDF shared actor.

`reason_for` keeps every existing genre/bare-seed/seedless branch untouched, and only when
`len(candidate.attributions) > 1` composes an extra clause — e.g. *"Because you watched sci-fi like
Dune — also part of the Dune saga, and shares Timothée Chalamet with Wonka"* — capped at 3 causes and
a length ceiling (truncate to "and N more").

### Persistence — no migration for v1

`Pick.reason` (models.py:229) is already a plain `str`, and it is the field that survives a
carried-forward row. Change 3 renders the richer string into that same field with **zero schema
change**; the structured `Attribution` list stays ephemeral for one run, exactly as `top_seed` is
Candidate-only today. A migration is needed **only if** product wants structured per-cause UI badges
— an open product decision, not a technical requirement.

### Tests required first

1. `test_a_candidate_can_carry_attributions_from_multiple_signals`.
2. `test_reason_for_with_one_attribution_matches_todays_exact_wording` — regression pin against every
   existing `TestReasonFor` case.
3. `test_reason_for_composes_up_to_three_causes_and_no_more`.
4. `test_reason_line_has_a_length_ceiling`.
5. The full existing `TestReasonFor` suite must pass unmodified.

### Settings / migration / UI

No setting (attribution is presentation, always-on once the signals that populate it exist). No
migration for the string-only design. No frontend change if scoped to the existing free-text `reason`;
a structured-badges follow-up would need `pnpm gen:api`.

---

## Change 4 — Franchise awareness

**Where:** `clients/tmdb.py` (new method), `candidates.py` (post-gather pass), `ranking.py`.

TMDB's `belongs_to_collection` is **movie-only** — TV has no equivalent field. The whole feature is
inert for `MediaType.SHOW`, and that must be an explicit no-op, not a silent lookup against a
namespace that does not exist.

### Seed-side only, to avoid a per-candidate cost blowup

A naive design checks every CANDIDATE's collection membership (one detail call each — hundreds per
pool). Instead: check only the SEEDS' collections (already paid for by `details()`), fetch that
collection's member list in ONE call, and match against the existing pool by id — no per-candidate
detail call at all.

```python
# clients/tmdb.py
def collection_members(self, collection_id: int) -> set[int]:
    """Every movie tmdb_id in a TMDB collection ('franchise'), from ONE cached call."""
    data = self._get(f"/collection/{collection_id}")
    return {p["id"] for p in data.get("parts", []) if isinstance(p, dict) and "id" in p}
```

```python
# candidates.py — after every source has populated the pool, before pre_rank
def mark_franchise_members(pool: dict[tuple[int, MediaType], Candidate], tmdb: TmdbClient) -> None:
    """Flag any pooled MOVIE candidate sharing a TMDB collection with one of its own seeds.
    TV has no `belongs_to_collection` in TMDB's schema — this is a no-op for shows."""
    seed_collections: dict[int, tuple[int, str]] = {}
    for candidate in pool.values():
        if candidate.media_type is not MediaType.MOVIE:
            continue
        for seed in candidate.seeds:
            if seed.tmdb_id in seed_collections:
                continue
            coll = tmdb.details(seed.tmdb_id, MediaType.MOVIE).get("belongs_to_collection")
            if coll:
                seed_collections[seed.tmdb_id] = (coll["id"], coll["name"])
    members_by_collection = {cid: tmdb.collection_members(cid) for cid, _ in seed_collections.values()}
    for candidate in pool.values():
        if candidate.media_type is not MediaType.MOVIE:
            continue
        for seed in candidate.seeds:
            entry = seed_collections.get(seed.tmdb_id)
            if entry and candidate.tmdb_id in members_by_collection[entry[0]]:
                candidate.in_seed_franchise = True
                candidate.attributions.append(Attribution("franchise", seed.title, seed.tmdb_id, entry[1]))
                break
```

`Candidate` gains `in_seed_franchise: bool = False`.

```python
# ranking.py
FRANCHISE_BOOST_MAX = 0.5  # smaller than a full extra seed match (+100% via seed_frequency) —
# "continues the same story" is strong evidence, not equivalent to
# having actually re-sought this seed. Caps the boost so a detected
# sequel cannot out-rank a well-seeded, high-affinity title on its own.


def franchise_factor(is_member: bool, strength: float) -> float:
    return 1.0 + (FRANCHISE_BOOST_MAX * max(0.0, min(1.0, strength)) if is_member else 0.0)
```

### Extra TMDB calls

One `details()` per **distinct seed** (already paid for by Change 1 on the consolidated endpoint —
zero marginal cost) plus one `collection_members()` per seed that actually belongs to a collection (a
minority of movies; cached 7 days, shared server-wide). **Zero calls for show-only rows.**

### Tests required first

1. `test_franchise_factor_is_neutral_when_off`.
2. `test_franchise_factor_boosts_a_sequel_candidate` — exact value at strength=1.0.
3. `test_franchise_membership_is_movie_only` — a SHOW never sets `in_seed_franchise`, even with a
   coincidentally-matching id (the movie/TV namespace-collision class this codebase is chronically
   careful about).
4. `test_gather_candidates_marks_a_sequel_found_via_the_seeds_collection`.
5. `test_a_seed_with_no_collection_costs_no_extra_call` — `collection_members` asserted never called.
6. `test_score_with_franchise_strength_zero_matches_todays_formula_exactly`.

### What could regress

An unbounded boost lets a mediocre sequel outrank a much better-matched title — bounded by
`FRANCHISE_BOOST_MAX=0.5`, plus a test asserting a low-rated sequel still loses to a much
higher-rated, higher-affinity non-franchise candidate.

### Settings / migration / UI

New `recommendations.franchise_strength` (float 0..1, default 0.0 both layers), plus
`RowSpec.franchise_strength: float | None = None` — **appended at the end of the dataclass**
(models.py documents the positional-argument hazard for `RowSpec`/`Pick`). No migration. One more
slider.

---

## Change 5 — A cast signal, IDF down-weighted

**Where:** `clients/tmdb.py` (reuses `details()`), a new enrichment pass called from
`rows.py::_candidate_pool` right after `ranking.cut_for_recency` (line 982), `ranking.py`.

Confirmed by grep: no cast/credits/`belongs_to_collection` handling exists anywhere in `shortlist/`.

### Why IDF, and why pool-scoped is correct here

The failure mode: a prolific actor in 40 unrelated films makes all 40 look similar. IDF down-weights
an actor by how often they recur **within the same set being compared** — textbook TF-IDF, where
"documents" = titles and the "corpus" = the pool being ranked. Unlike Change 1 (which compares a user
against an external population and cannot use the candidate pool without circularity), Change 5's
corpus **is** the thing being compared — the current row's pool is the textbook-correct scope, not a
compromise.

```python
TOP_CAST_N = 5  # billing-order head — leads + notable co-stars, excludes ensemble
CAST_NORMALIZER = 3.0  # saturation point for summed IDF overlap
CAST_BOOST_MAX = 0.5  # same ceiling as FRANCHISE_BOOST_MAX, same reason


def cast_idf(pool_cast_lists: list[set[str]]) -> dict[str, float]:
    """Standard smoothed IDF over the CURRENT POOL's top-billed cast lists — down-weights an actor
    in proportion to how many of THIS pool's titles carry them, so a prolific actor's presence in 40
    unrelated films no longer makes all 40 look similar to each other."""
    n = len(pool_cast_lists)
    df: Counter[str] = Counter()
    for cast in pool_cast_lists:
        df.update(cast)
    return {actor: math.log((1 + n) / (1 + count)) + 1 for actor, count in df.items()}


def cast_overlap_score(seed_cast: set[str], candidate_cast: set[str], idf: dict[str, float]) -> float:
    shared = seed_cast & candidate_cast
    return min(1.0, sum(idf.get(a, 0.0) for a in shared) / CAST_NORMALIZER)
```

```python
# ranking.py
def cast_factor(overlap: float, strength: float) -> float:
    return 1.0 + CAST_BOOST_MAX * max(0.0, min(1.0, strength)) * overlap
```

`Candidate` gains `cast_overlap: float = 0.0`.

**Constant reasoning (needs live validation).** With this smoothed IDF, an actor in 1 of 50 pool
titles scores `≈ 4.24`; in 10 of 50, `≈ 2.53`; in 40 of 50 (the failure mode), `≈ 1.22`.
`CAST_NORMALIZER = 3.0` means one rare shared lead alone reaches full boost, while the prolific-actor
case contributes under half the boost from a single shared credit — real, measurable down-weighting.

### Cost control — the real trade-off

Cast overlap needs **both sides'** cast lists, unlike franchise (seed-side only). Fetching `details()`
for every pooled candidate (hundreds, pre-cut) would be a real new cost. So it runs **after** the pool
is bounded — `rows.py::_candidate_pool` already produces exactly that:

```python
ranked = ranking.cut_for_recency(in_library, kinds, cap, recency, _run_year(ctx.run_day))
# NEW — cast enrichment on the already-bounded (<= candidates_pre_rank per media type) pool:
if cast_strength > 0:
    ranked = candidates_mod.enrich_cast_affinity(ranked, ctx.tmdb, cast_strength)
```

That bounds the extra cost to `candidates_pre_rank` (default `2 × row_size`, ~30) detail calls per
row, not the raw pool size — the same "cheap cut, then a bounded expensive re-rank" layering
`cut_for_recency` already uses.

**Architectural constraint:** the codebase has a dedicated test class (`TestRankAgainstPoolOrdersOnly`
in `test_ranking.py`) enforcing this discipline for a similar step: *it orders; it must never decide
membership.* `enrich_cast_affinity` must re-**sort** the already-cut list, never add or drop members.
This needs its own mirroring test.

### Tests required first

1. `test_cast_idf_down_weights_a_prolific_actor` — an actor in 40 of 50 gets a much lower idf than
   one in 2 of 50.
2. `test_cast_overlap_is_zero_with_no_shared_actors`.
3. `test_cast_factor_is_neutral_when_off`.
4. `test_enrich_cast_affinity_re_ranks_but_never_drops_a_member` — mirrors
   `TestRankAgainstPoolOrdersOnly`.
5. `test_enrich_cast_affinity_only_fetches_the_bounded_pool_not_the_raw_gather` — cost-control guard.
6. `test_score_with_cast_strength_zero_matches_todays_formula_exactly`.

### What could regress

IDF computed over the wrong scope (a global cross-run actor table instead of this pool) wrongly
suppresses an actor rare here but common server-wide — caught by asserting the idf table is rebuilt
per-pool. The enrichment wired before the membership cut balloons cost silently — caught by test 5.
A 5-title pool has too little data for IDF to discriminate: a dedicated test should assert
`cast_overlap` degrades gracefully (near-zero effect) on a small pool.

### Settings / migration / UI

New `recommendations.cast_strength` (float 0..1, default 0.0 both layers),
`RowSpec.cast_strength: float | None = None` (appended last). No migration.

---

## Ordering: what genuinely depends on what

```
1 (genre avoidance) ──▶ 2 (negative multiplier)
        │
        ▼
   [consolidated TmdbClient.details() — introduced by 1, reused by 4 and 5]
        │
        ├──▶ 4 (franchise) ─┐
        │                    ├──▶ 3 (attribution) becomes user-visible
        └──▶ 5 (cast) ───────┘
```

- **1 before 2** is hard: `negative_multiplier` has nothing meaningful to combine without a real
  penalty source (its own unit tests can use synthetic values in isolation).
- **1 introduces `TmdbClient.details()`**, which 4 and 5 must both reuse rather than each adding a
  second per-title fetch path with its own cache-key shape. A real implementation dependency.
- **3 has no hard technical dependency on 4/5**, but little practical value until they exist — genre
  avoidance is a suppressor, not a positive "why you're seeing this" cause, so the only pre-existing
  positive signal to attribute is the seed match `top_seed` already covers. Build 3's scaffolding
  early so 4 and 5 each just append an `Attribution` as part of their own change; expect its visible
  payoff to land with 4/5.
- **4 and 5 are independent** and can ship in either order or in parallel.

The plumbing this implies (threading 3 new float kwargs through `score()`, `_sort_key()`,
`pre_rank()`, `cut_for_recency()`, `_candidate_pool()` — the same shape `recency` is threaded today)
is mechanical and a good candidate for a cheaper model once the formulas and constants are settled.

## The complete new scoring formula

```python
def score(
    candidate: Candidate,
    *,
    recency: float = 0.0,
    year_now: int = 0,
    genre_avoidance_strength: float = 0.0,
    franchise_strength: float = 0.0,
    cast_strength: float = 0.0,
) -> float:
    seed_weight = max((s.weight for s in candidate.seeds), default=0.0)
    rating = candidate.rating or 5.0
    base = (1 + candidate.seed_frequency) * rating * (1.0 + seed_weight) * candidate.affinity
    base *= negative_multiplier(candidate.genre_penalty * genre_avoidance_strength)
    base *= franchise_factor(candidate.in_seed_franchise, franchise_strength)
    base *= cast_factor(candidate.cast_overlap, cast_strength)
    return base * recency_factor(candidate.year, year_now, recency)
```

Every new term is a bounded multiplier that is **exactly 1.0** at its default strength — the same
mechanism across all three, which is what makes the composition backward-compatible by construction
rather than by individually-verified accident.

## Backwards compatibility

All three strengths default to **0.0 in both the `EngineConfig` dataclass and the `settings_store`
seed** — unlike `recency`, which the codebase deliberately turned on for every existing install
(dataclass 0.0, `settings_store` seeds 0.5 "for every install, existing servers included", an
explicit prior product decision recorded in `models.py`). Per "my taste is not the default", none of
these get that treatment without the owner explicitly deciding so. Today's exact scores are
reproduced until someone turns a dial. Change 3 needs no setting at all.

## What might make picks WORSE, and how we would notice

- **Genre avoidance mis-fires on a small sample** (2 avoided-horror watches were coincidence, not
  aversion) → the shrinkage tests catch under-shrinkage before ship, but a live false positive shows
  as a genuinely-liked genre quietly sinking. Visible via the run trace (`GatherStats.trace` already
  logs per-source contribution) if extended to log the applied `genre_penalty` per demoted candidate,
  the way `_stamp_disposition` already explains drops.
- **Franchise/cast over-weighting a mediocre sequel or ensemble pairing** — bounded by the shared 0.5
  ceiling and the "still loses to a much better match" regression tests; a real-world miss shows as a
  low-rated franchise entry above better candidates in the "How we picked" trace.
- **Cast IDF constants wrong for a small library** — a 5-title pool has too little data to
  discriminate; assert graceful degradation and watch a real small-library server.
- **General:** every dial defaults off, so nothing can regress anyone until an owner turns a slider.
  The detection mechanism is the rollout pattern `recency` already established: turn a dial on, watch
  your own row for a few nights, turn it down if it gets worse — now backed by the replay harness.

## Could not determine from the code — flagged rather than guessed

1. Whether plexapi's `section.all()` items carry `.genres` inline or need a per-item re-read (like
   `.labels` does). No fixture records it; must be verified against a real PMS response before
   choosing Change 1's cheap library-baseline path over the TMDB-lookup fallback.
2. The numeric sweet spots for `GENRE_SHRINK_K`, `CAST_NORMALIZER`, `FRANCHISE_BOOST_MAX` /
   `CAST_BOOST_MAX` — reasoned from TMDB's genre-count scale and standard IDF behaviour, not
   validated against the real watch-history distribution.
3. Whether product wants structured per-cause UI badges for Change 3 (needs a migration) or a single
   rendered string suffices (no migration).
