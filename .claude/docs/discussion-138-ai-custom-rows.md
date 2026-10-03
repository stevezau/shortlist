# Discussion #138 — Rows described in your own words

Status: **phase 1 (AI instructions) built 2026-10-03, uncommitted pending the owner's OK; phases 2-4 not started.** Plan: `.claude/docs/plans/2026-10-03-ai-instructions-phase1.md`. As built, owner text may use `{count}`, `{year}` and `{last_year}`; with no AI provider (or a provider that can't search the web itself on the native backend) the instructions are shown as having no effect. Mockups and the decisions board: the "AI Rows Proposal"
canvas, https://claude.ai/artifact/Y8CMBv8UQYbVTgFmE8cnwC. The owner's direction (2026-10-03): rows should be
able to override the AI prompts, AND there should be a fully customisable AI row. So the proposal has two parts:
**A** (AI instructions on any row) and **B** (the AI row, the design below). Every number
was measured on the maintainer's server (read-only: 10,033 films, 45 people with 20+ watched films).

## The ask

`LoveMyslf-1` (Ideas): describe a row in plain language ("Movie Night: 15 psychological thrillers with
surprising endings, in my library, unwatched, under two hours"), refine it ("less horror", "more recent"),
and let one permanent row refresh on its own interval, either keeping its theme or exploring new themes
inside a broader instruction. Controls asked for: rotation interval, how much changes, no repeats for a
period, less overlap with chosen rows, a preview with reasons before going live, privacy and parental
controls kept, AI optional and metered. Also floated: ChatGPT or MCP as the authoring surface, and
placeholders for titles not on the server. Plus a question: "the early beta release notes mention
per-row curation styles/prompts — is some of this already supported?"

It is a feature request. Nothing is broken. The question has a factual answer and exposes one docs defect.

The reading: an **AI row driven by his own prompt**. His saved instruction is what the AI works from, and he
controls how the row behaves over time (fixed or exploring themes, interval, churn, repeats, overlap,
preview). He draws the line himself: the AI suggests "while Shortlist handles matching, filtering and
delivery to Plex". So he owns the instruction and the behaviour; Shortlist keeps the plumbing (output
format, library matching, privacy). "All within my preferences" and "match my taste" mean the AI should
know the person's taste when it writes a theme.

## The question: per-row prompts existed for ten days and were removed

- Built 13–17 Jul 2026 (`aa2efb2a`, `25dfaab7`, `7dda28eb`, `0ae20575`): tone + guidance + full custom
  prompt, global → row → person.
- Advertised in `CHANGELOG.md` under **0.1.0-beta (2026-07-21)**: "curation style/prompt", "optional
  LLM curation", "AI suggests from your library", "the curator … writes the one-line reason".
- Removed 23 Jul 2026 by `ebd14ce6` ("remove LLM curate step, move ranking/reasons to code") and
  migration `0036_drop_curate_settings`. Reasons given: cheaper, deterministic, templated reasons are
  good enough. AI now only *proposes* titles (`llm_web`); ranking and reasons are code
  (`curator/base.py:3-4`). `Collection.prompt` survives as a dead column (`models.py:312`).
- **Docs defect:** no CHANGELOG entry records the removal (there are no entries for beta.6–beta.8), so
  the 0.1.0-beta section still promises it. That is almost certainly what the poster read.
  `.claude/CLAUDE.md` also still says "LLM curate/explain". Neither is fixed yet.

## What exists to build on

| Need | Today |
| --- | --- |
| A row filled from a theme, not from watches | **Custom seasons (#137, on dev, unreleased)**: TMDB tags + one genre + genres to leave out + Plex collections + hand picks, library-only, `POST /api/seasons/preview` counts + sample. A season is a theme with a date. |
| Per-person ranking inside a theme | The `season` source: affinity = `genre_coherence(person's top-3 genres, title)` (`candidates.py:1044-1083`); pool limited to members (`rows.py:1330`). |
| Theme switch keeps the Plex row | Recipe change forces a full rebuild; `{season}` renames via `rename_or_keep`; ratingKey survives (second library gets a new one — known #124 limit). |
| Own interval | Per-row cron `schedule` + per-row `refresh_days` (server default 8). |
| How much changes | Fixed: a refresh keeps the best two-thirds (`_KEEP_FRACTION`, `rows.py:592`). |
| No repeats for a period | Only "not in the previous row" (`rows.py:2962`). `picks` keeps every run's picks forever (prune nulls `run_id` only); the engine loads only the latest. |
| Less overlap with other rows | None; only the requests pass dedupes. Overlap was accepted by the owner 2026-09-23. |
| Preview | No picks preview. `POST /api/runs {dry_run, user_ids, collection_ids}` runs the real engine for one person and row without writing; dry-run picks are not persisted. |
| Hard filters | Watched, unstarted, excluded genres, media, libraries, visibility (`RowPolicy.visible` → `visible_to`, the person's own token). **No runtime, year-range or rating filter.** |
| AI usage | Providers anthropic / openai / openai_compatible (Ollama, local) / google / none. Token accounting per run, person, row, step. **No budgets.** `llm_web` gathers on **every run whatever the cadence** (`rows.py:2940`): a slower refresh saves Plex writes, not tokens. |
| External API | Owner-level bearer token `shl_…` (`auth.py`). No scopes. No MCP. |

## Measurements that shape the design

Can a brief compile to TMDB tags/genres that fill a row?

| Theme (films, this library) | TMDB | In library | Unwatched per person (median / min) |
| --- | --- | --- | --- |
| psychological-thriller OR twist-ending tags, ≤120 min | 1,240 | 78 | 76 / 67 |
| twist-ending tag AND Thriller genre, ≤120 min | 69 | 7 | 7 / 6 |
| "lesser-known" sci-fi (50–1,500 votes, ≥6.5) | 805 | 144 | 143 / 125 |
| dark-comedy tag | 2,003 | 184 | 179 / 152 |
| heist OR whodunit tags | 569 | 77 | 75 / 64 |

Tags fill rows, but they miss the heart of a fuzzy brief. An AI naming films from its own knowledge
(simulated with 52 titles I named for "psychological thrillers with surprising endings"):

| | Count |
| --- | --- |
| Named | 52 |
| Found on TMDB | 52 |
| In this library | 47 |
| In library and ≤120 min | 36 |
| …of those, **missing from the tag pool** | **24** (The Usual Suspects, The Others, Oldboy, Saw, The Invisible Guest, Predestination, Unbreakable, Side Effects…) |
| Named but over two hours | 11 (Shutter Island, Gone Girl, The Prestige, Se7en, Prisoners…) |

What this settles:

1. **AI recall is the point of the feature.** Tags alone lose two thirds of the films a person means by
   "surprising endings". Tags are still useful as a second source and as the no-AI fallback.
2. **Hard rules must be enforced in code.** The namer broke "under two hours" 11 times out of 47 while
   knowing the rule. Runtime, year, rating and votes come from TMDB, not from the AI.
3. **Watched history barely thins a theme** (78 → 76). Per-person difference must come from ranking, as
   #124 found (a rating-only order gave two people 48% overlap; genre fit cut it to 13%).
4. **Tag noise is real**: dark comedy includes Life Is Beautiful and Charlie and the Chocolate Factory;
   the vote band pulls in TV specials (Clash of the Thundermans). A minimum vote floor and an optional AI
   vet of the merged list are worth their cost.
5. Caveat: 47/52 reflects a 10k-film library and my own recall. A small library or a small local model
   will name fewer hits; the preview must show the count, not hide it.

## Approaches

1. **Brief → stored theme; code fills every run (recommended).** AI runs only when the owner saves or
   refines a brief, and when an exploring row moves to its next theme. It returns a name, hard rules,
   TMDB tags/genres and ~60 named titles. The server checks every one against TMDB and the library and
   stores the result as a theme. Each run fills the row exactly like a custom season: in-library members,
   unwatched, visible to that person, ranked by that person's taste. Zero AI calls per night. Keeps the
   July decision intact: AI proposes, code ranks and explains.
2. **The brief as a per-run AI source (like `llm_web`).** Rejected: it would call the AI for every person
   on every run whatever the cadence (`rows.py:2940`), 45 people × nightly here, and the row would drift
   between runs.
3. **Bring back LLM curation with the brief.** Rejected: it reverses `ebd14ce6`, costs per person per
   refresh, and the pool it would curate is the person's similar-titles pool, which holds almost none of
   a theme's films (#124: median 6 Christmas films).

## Where it sits in the redesigned app (#140, on dev 2026-10-03)

- **A, per row:** row editor → What goes in → under the Recommendation sources switches, an "AI instructions"
  field shown only while Web search is on (Use the default / Add to the default / Write your own, plus an
  "Exactly what's sent" preview). Saved through the existing sticky save bar.
- **A, default:** Settings → Defaults → Title sources → the Web search disclosure, under "How much it searches".
  Settings saves each change on its own.
- **B, entry:** two templates in the Add a row gallery, "Describe a row" (keeps its theme) and "AI Picks"
  (explores), under a new "AI" filter chip; inside the editor the row type is "AI row".
- **B, editor:** the description, limits, the list and "Change it" sit in What goes in (as every kind's settings
  do); new jump-list sections "How it changes", "Try it" and "AI prompts", plus three new Schedule fields.
  "Build the list" and "Try it" are outline buttons, so Save changes / Add row stays the one amber control.
- Found while mapping it: `web/src/lib/sources.ts:46` and `web/src/components/runs/run-stat-tiles.tsx:232` still
  send the owner to "Settings → Finding titles", which the Settings split removed.

## Part A: AI instructions on any row

- The only AI prompt today is AI web search's, in three shapes, one per search backend (`curator/base.py`:
  `_WEB_SYSTEM` native, `_WEB_RAG_SYSTEM` SearXNG, `_WEB_PICK_SYSTEM` Exa final picks). Each is split into
  **guidance** (editable: what to favour, e.g. "strongly prefer titles released in {last_year} or {year}")
  and **mechanics** (locked: the year, "search before answering", exact title + year, released only, reply
  format). The comments in `base.py` record why the mechanics are load-bearing (without the year, two models
  returned nothing from 2024 or later).
- Settings → Finding titles gets a default; each row picks Use the default / Add to the default / Write your
  own. `{recent_watches}`, `{top_genres}`, `{count}`, `{year}` placeholders. A live "exactly what's sent" preview.
- The instruction text goes into `pool_key` and the recipe, so a change rebuilds that row and two rows with
  different instructions never share a pool.
- Honest limits shown in the editor: other sources on the row don't read it; with Exa, the per-seed searches are
  cached server-wide, so instructions steer only the final pick; AI web search runs every run for every person
  (prod 2026-10-02: ~360k tokens a night for 46 people, Claude Haiku 4.5 + Exa, median ~7,800 a person).
- The dead `Collection.prompt` / `CollectionUserOverride.prompt` JSON columns (cleared by 0036) are where July's
  version lived. Reusing them needs a migration that confirms they are empty; a fresh column is cleaner.

## Design (approach 1)

### The theme

A theme is a custom season without a date, plus hard rules and the brief it came from.

- `themes` table: `slug`, `name`, `emoji`, `brief` (text, may be empty), `origin` (`ai` | `manual`),
  `tags`, `genres`, `excluded_genres`, `collections`, `picks` (each `{tmdb_id, media, origin: ai|owner}`),
  `rules` (`max_runtime`, `min_year`, `max_year`, `min_rating`, `min_votes`), `media`, `content_hash`,
  `created_at`, `ai_tokens`.
- Engine: extract the membership half of `seasons.Season` (tags, genre, excluded, collections, picks) and
  `load_titles` into a shared spec used by both seasons and themes, then apply `rules` to the loaded
  members once per run (runtime from `TmdbClient.details`, cached 7 days; year/rating/votes are already in
  discover results). Not person-specific, so it costs one pass per theme per run.
- A `theme` source and theme filter mirroring `season`; `pool_key` gains the theme; the recipe gains
  `theme=<slug>#<content_hash>` so editing a theme or switching to the next one fully rebuilds the row.
- `{theme}` / `{theme_emoji}` placeholders, treated like `{season}` (title needs a run to render).
- Reason for a pick: "Fits *Movie Night*, in genres you watch" (seedless), or "Fits *Movie Night* — like
  Memento, which you watched" when a seed matches.

### Writing a theme from a brief (server, `services/theme_author.py`)

One `curator.complete` call with a JSON-only prompt. The AI gets the brief, the media type and, when
refining, the current theme. For a theme written for one person it also gets that person's taste summary,
the same recent-titles list `llm_web` already sends (`curator/base.py` `taste_summary`), so "within my
preferences" is honoured. A shared theme gets no watch history. Names are never sent.
It returns name, emoji, rules, TMDB tag names, genre names, and up to ~60 `{title, year}`.

The server then resolves tag names (`search_keywords`, exact or top match), genre names (TMDB genre list),
titles (`/search/movie|tv` + year → first match), drops what does not resolve, applies rules, and
intersects with the library. Optional second call: show the AI the merged in-library list (title + year
+ one-line overview) and let it drop misfits. Off by default; decided after measuring on a real brief.

Refining ("less horror", "more recent") is the same call with the current theme attached. The editor
shows what changed (rules diff, +/− titles, new count) before it is saved. No chat log is kept.

With the AI provider set to None, the AI half is hidden and the same editor builds a theme by hand (tags,
genre, rules, picks), which is a dateless custom season.

### The row

- New row kind **Themed** (tile in Add a row). Fields on `Collection`: `theme_id`, `theme_mode`
  (`fixed` | `explore`), `explore_brief` (the broad instruction), `theme_days` (explore interval).
- **Fixed:** the theme stays; the existing cadence refreshes picks inside it.
- **Explore:** every `theme_days` the server writes the next theme from `explore_brief`, giving the AI the
  last N theme names to avoid. The next theme is written a day early and shown in the editor as "Up next",
  where the owner can regenerate or edit it. A failed write keeps the current theme and logs an event;
  the row is never emptied for want of a theme. On a per-person row each person gets their own theme,
  written from their taste: one AI call per person per interval (45 people weekly is 45 calls a week,
  against `llm_web`'s 45 every night). On a shared row, one theme for the audience.
- `themes_history` (`row_id`, `theme_id`, `started_at`) feeds "avoid the last N themes" and the editor's
  history.

### The controls

| Ask | Design | Default |
| --- | --- | --- |
| Rotation interval | Explore: `theme_days`. Fixed: existing `refresh_days` + `schedule`. | 7 |
| How much changes | New per-row `refresh_share` replacing the fixed third. | ⅓, today's behaviour |
| No repeats for a period | New per-row `repeat_cooldown_days`: drop titles this person was shown in this row within N days, read from `picks` (dry runs excluded; history already kept). | Off |
| Less overlap with chosen rows | New per-row `avoid_rows`: drop titles currently in those rows for the same person (this run's picks if built, else the latest stored). Opt-in; overlap stays accepted elsewhere. | None |
| Preview with reasons | `POST /api/themes/preview`: rules, counts (named, resolved, in library, after rules, unwatched median), sample with origin badges. "Try it for <person>" = the existing scoped dry run; its report shows picks and reasons. New themed rows start disabled until the owner switches them on. | — |
| Privacy, restrictions, parental controls | Unchanged path: watched filter, excluded genres, `RowPolicy.visible` with the person's token, leak-safe delivery. No new Plex write path. | — |
| AI optional, cached, metered | AI only on save/refine/next-theme. Tokens recorded on the theme and on the event. Editor states "uses your AI provider once per theme". | — |

`refresh_share`, `repeat_cooldown_days` and `avoid_rows` are implemented generically in the engine but
exposed only on themed rows in v1.

### Plex safety

- Rule 1: an ordinary row; delivered unpromoted, excludes merged, then promoted. A theme switch is a
  rename + rebuild, the season-switch path.
- Rule 4: no delete path added. Theme names go through the duplicate-title check (#137 decision 17),
  because collection titles are global tags (a rename onto an existing title 409s).
- Migration + rows → Architecture Review applies.

## Non-goals (v1), with reasons

- **MCP / ChatGPT authoring.** ChatGPT only reaches MCP servers at a public HTTPS URL, which means
  exposing a self-hosted Shortlist to the internet, and today's API token is unscoped owner access
  (share filters included). A local MCP (Claude Desktop, Cursor) is feasible later as a thin wrapper over
  the theme endpoints; prerequisite: a token scoped to rows. "Use ChatGPT" today means an OpenAI API key,
  which is already supported.
- **Placeholders for titles not on the server.** Shortlist is placeholder-blind today (see #118 facts).
  v1 is library-only; the preview counts AI-named titles that are missing, and nothing is requested.
- **Generic cooldown / avoid-rows on other row kinds.** Engine-ready; UI later if asked.

## Changed verdicts (vs the poster's framing)

- PREMISE WRONG: "per-row curation prompts are in the beta notes" — they were removed before beta.9 and
  the changelog never said so.
- RESCOPED: MCP/ChatGPT and placeholders move out of v1, for the reasons above.
- SMALLER than it looks: most of the engine exists (custom seasons, per-row cron, cadence, picks history).
- BIGGER than it looks: runtime/year/rating rules are new engine filters; explore mode is a new server job.

## Decisions (owner delegated them to the recommendations, 2026-10-03)

1. Row instructions: Use the default / Add to the default / Write your own guidance; Shortlist locks the year,
   "search before answering", exact title + year, released-only and the reply format.
2. Default and per row only; no per-person instructions.
3. AI row: the AI builds the list once per theme; Shortlist picks per person every run, with no AI.
4. Explore mode's "Whose taste" starts at "Each person's own" (a per-row setting either way).
5. Length, year and rating limits go on every row, under What goes in.
6. Reasons: the AI writes one line per title when building the list; Shortlist adds the personal hook.
7. Usage: shown per row and per step, plus "Pause AI for this row"; no token cap.
8. Order: phase 1 instructions, 2 limits, 3 AI row (fixed theme), 4 explore + over-time controls.
9. MCP / ChatGPT: later, after an API token limited to rows.
10. Name: "AI row"; templates "Describe a row" and "AI Picks".

Every new setting defaults to today's behaviour. Each phase gets its own plan in `.claude/docs/plans/`.
