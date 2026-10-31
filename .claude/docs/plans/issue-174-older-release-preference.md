# #174 — let release age lean older, not only newer

Status: design 2026-10-30. Not implemented. Needs an owner go/no-go.

## Verdict

A feature request, and the claim is accurate:

- `recency` is validated to 0..1 in `settings_validation.py:363` and `row_editing.py:225`.
- `recency_factor` (`shortlist/engine/ranking.py:37`) returns 1.0 for any value <= 0.
- So today you can push titles newer or ignore age. You cannot lean older.

## Shape: one signed dial, not a second setting

`recency` becomes -1..1:

- 0 is neutral and returns exactly 1.0, as today.
- 0..1 is today's newer curve, unchanged. Every saved value keeps its meaning.
- -1..0 is a bounded older boost.

The global setting `recommendations.recency` keeps its 0.5 default. `collections.recency` is already a
nullable Float. **No migration** — only the bounds change.

A separate "older boost" field would let both dials be set at once. Neither the owner nor the trace
could say which one won.

## The older-side curve

It follows the existing bounded boosts (`franchise_factor`, `cast_factor`, `ranking.py:140`):

```
OLDER_BOOST_MAX = 0.5          # same ceiling as FRANCHISE_BOOST_MAX / CAST_BOOST_MAX
OLDER_BOOST_YEARS = 25.0       # time constant; needs the eval below before it is final
age = max(0, year_now - year)
factor = 1 + OLDER_BOOST_MAX * s * (1 - exp(-age / OLDER_BOOST_YEARS))   # s = -recency, 0..1
```

At full strength:

| Title age | Boost |
| --- | --- |
| 10 years | +16% |
| 25 years | +32% |
| 40 years | +40% |
| 60 years | +45% |

The boost never exceeds +50%. That is the "a dampener shades the ranking, it does not decide it" rule
the other positive signals follow. An uncapped mirror of the newer curve does not work: at a half-life
of 8 years it gives a 1990 film 22x, or it hits any cap within 5 years and goes flat.

Same "no opinion" cases as today: an unknown year, a future year (age clamped to 0) or `year_now<=0`
all return 1.0. `score()` already multiplies `recency_factor` in, and `_sort_key`, `pre_rank`,
`cut_for_recency` and the shared-pool cut all go through `score()`. So the pool cut and the final order
agree with no extra plumbing. That was the reporter's ask.

## Surfaces to change

- Engine: `ranking.recency_factor` gets a negative branch. `RowSpec.recency` and `EngineConfig.recency`
  docstrings get the new range.
- Validation: `settings_validation.py:363`, `row_editing.py:225`, the MCP `row_adapter` schema text,
  and the API model in `api/collections.py:207`. Regenerate the OpenAPI snapshot and the TS types.
- UI: `web/src/components/settings/recency-slider.tsx` becomes -100..100 with a centre detent labelled
  "Neutral", ends labelled "Older" and "Newer". `recencyEras`/`recencyDescription` in
  `web/src/lib/constants.ts` draw the older side. Bars are normalised to the largest weight, and the
  label shows "+32%" rather than "this year". `test_recency_curve_matches_the_engine` pins both curves
  between Python and TS.
- Trace: `run-user-trace.tsx` shows the factor as a boost when it is above 1.
- Docs: settings reference and guides. Run `scripts/build_llms_full.py`.

## Limits to state in the UI and docs

- It is a preference, not a filter. Hard cut-offs remain `min_year`/`max_year`.
- It can only reorder what the sources found. It cannot bring in an old title that TMDB similar or
  discover never proposed. #168 (candidate discovery) is the complement.
- For TV, age is first-air year, as on the newer side. A series running since 1999 counts as old.

## Evaluate before choosing the constants

Use the existing leave-one-out replay (`shortlist/engine/eval/replay.py`, `scripts/replay_eval.py`) on
a copy of the production DB. Run TMDB-only arms: no AI, no token spend.

- Arms: recency 0, -0.25, -0.5, -1.0, plus T = 15/25/40.
- Measure delivered release-year distribution against the unwatched library's.
- Measure the hit rate on discoveries. That is the guard against trading relevance for era coverage.
- Also measure the pool's year distribution before the cut. If the pool itself holds few pre-2000
  titles, no ranking weight will help much, and #168 matters more than this feature.

## Tests (write first)

- Neutral (0) and every positive value score bit-for-bit as before.
- The older factor rises with age, never exceeds `1 + OLDER_BOOST_MAX`, and is 1.0 for unknown and
  future years.
- Validation: -1..1 is accepted, below -1 is rejected. A per-row override inherits from the global
  setting when unset.
- `pre_rank` and the final order agree under a negative value.
- TS/Python curve parity.

## Review

Ranking plus UI, with no Plex writes, no migration and no identity data. This is not an Architecture
Review trigger.
