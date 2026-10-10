# Discussion #124 — Seasonal rows

Status: **built 2026-09-15**, proven live on a large production server (see "Live proof"). Every number below was measured on the maintainer's
server (read-only probes: 46 people, 9,984 films, 4,901 shows), not estimated.

## The ask

`mrjayarr`: do recommendations change with the season — spooky films before Halloween, Christmas
films in December? If not, add it.

The owner's shape (2026-09-15), which replaced a row-per-holiday draft:

- a new **Seasonal** row template;
- the row works out the current season and fills itself for it;
- the owner picks which seasons it includes;
- a setting for how early it shows ("1 month prior");
- in season it keeps to that season and changes every day;
- per person or shared.

## What exists today

Nothing is date-aware. Rows are built from each person's watches (TMDB similar titles), ranked in
code; the LLM "curate" step was removed and AI only runs in the optional `llm_web` source, whose
prompt knows the year and nothing finer. A manual workaround is impossible: no row setting limits a
row to a theme, and "When it appears" (#102) only knows weekdays.

## Measurements that shaped the design

| Question                                                 | Answer                                                                                                 |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Do people here watch seasonally?                         | Christmas films: 38% last watched in December (films overall 7%). Halloween-tagged: 30% in October (16%). |
| Can TMDB say which films are seasonal?                   | Yes. `christmas` (207317) + 8 related keywords tag 340 of our films; `halloween` (3335) + 4 tag 63; Horror has 644. |
| Which seasons can fill a row here?                       | Christmas 340, Halloween 63 (670 with horror), New Year's Eve 30, Valentine's 20 (941 romance), Thanksgiving 15, Hanukkah 6, Easter 5. |
| Can a seasonal row just filter today's pool?             | No. Each person's similar-titles pool holds a median of **6** Christmas and **1** Halloween film (9 people: none). |
| Is rating-only ranking personal enough?                  | No. Two people's 15-film Christmas rows would overlap 48% (today's rows: 3%). A genre-fit weight cuts it to 13%. |
| TV?                                                      | Barely: 13 Christmas shows, 2 Halloween. Seasonal rows default to films.                                   |
| Shared seasonal rows viable?                             | Yes. 55 Christmas films watched by 2+ people (37 by 3+); Halloween + horror 177.                          |
| TMDB discover paging                                     | Sorted by popularity it silently drops ~7% (3,133 of 3,370). Sorted by release date it returns all.        |
| Membership cost (movies)                                 | Christmas 184 pages; Halloween 34 keyword + 112 horror (≥200 votes); Valentine's 6 keyword + 124 romance. |

## Decisions

1. **One row follows the calendar** (owner's shape). Not a row per holiday.
2. **Catalogue v1: Halloween (31 Oct), Christmas (25 Dec), Valentine's Day (14 Feb).** The only
   three that fill a row on a real library. Fixed dates, so hemisphere and moving-date calendars do
   not arise. Weather seasons are out: they flip between hemispheres and TMDB has no tag for them.
3. **Halloween includes Horror; Valentine's includes Romance** (films only — TMDB's TV genre list has
   neither). Keywords alone would give 63 and 20 films here, too few to make rows differ per person.
4. **Out of season the row is hidden and frozen, never deleted.** Same mechanism as #102's off days,
   so the ratingKey survives (#119, agregarr keys per-collection settings on it) and no delete path
   is involved. `_retired_rows` also cannot retire a row whose title needs a run to render.
5. **"Changes every day" is the existing nightly refresh** (`refresh_days=1`: keep the best two-thirds,
   swap the rest). No new rotation mechanism.
6. **The name follows the season** through `{season}` ("Christmas") and `{season_emoji}` ("🎄"),
   treated like `{top_seed}`: a title only a run can render.
7. **AI web search is not used on seasonal rows.** Its per-title searches are not seasonal, so nearly
   everything it proposes (and is paid for) would be filtered out. The editor says so.
8. **One lead time and one "stays after" per row**, not per season. A different lead for one season is
   a second seasonal row.
9. **The Seasonal template ignores release date** (`recency: 0`). Seasonal favourites are old: Christmas
   films watched on a large production server have a median release year of 2008 (61% pre-2015). At the server's 0.95, a
   Christmas row's tail was obscure 2025 TV movies shared by everyone; at 0 it was Christmas Vacation,
   Trading Places, Klaus. Halloween improved too (Casper, The Lost Boys). It is a template value, not a
   forced one: the row's "Recent releases" control still overrides it.
10. **Halloween keyword matches drop Drama and Romance** unless they are also horror
    (`Season.keyword_excluded_genres`). TMDB tags any film with a Halloween scene; of 23 non-horror
    Halloween keyword films in the library, those two genres cut exactly the misses (When We First Met,
    War Pony, Ed Wood, Brick, In America; Donnie Darko is the one debatable loss). Christmas keeps its
    romances.
11. **The season never strengthens a match a seeded source already measured.** Without this, a weak
    similar-title match ("Fresh", for a kids' animation watcher) was raised to the season's genre fit and
    then multiplied by its seed, topping their Halloween row.

## How an owner sets one up

Rows → Add a row → **🗓️ Seasonal** tile ("Halloween, Christmas & Valentine's · Starts a month before
· Changes nightly"). The editor opens with:

- Name `{season_emoji} {season} picks` → "🎃 Halloween picks", "🎄 Christmas picks".
- Who gets it: per person (default) or shared (then "watched by at least N people" as today).
- **Seasons** (new group, right after "Who gets it"):
  - "Follow the calendar" switch (on = the row has seasons).
  - One checkbox per season with its date and what it holds ("Halloween — 31 Oct · Halloween films and
    horror").
  - "Starts showing" N days before (0–90, default 30) and "Stays up" N days after (0–30, default 0).
  - A plain line: "Shows now: 🎃 Halloween until 31 Oct", or "Hidden until 🎄 Christmas on 25 Nov".
  - A warning when the row's schedule is not nightly: it would neither change daily nor switch
    seasons on time.
- What goes in it: media Movies, size 15; sources as usual with the AI note; everything else as for any
  row (already-watched cap, recency, cold start…).
- When it updates: schedule Nightly 03:30, "How often it changes" nightly.
- Where people see it: placement and weekdays as today — weekdays narrow further inside a season.

The row card badges the same season line.

## Behaviour

### Which season, on which day (pure, `engine/seasons.py`)

A season instance's **show window** is `[anchor − lead, anchor + after]` (dates, server local time,
same clock as #102). For a date:

- `shown` = the instance whose window contains the date. Several → an anchor on/after the date beats
  one before it; among those, the nearest anchor.
- `build` = `shown(today) or shown(tomorrow)`.
- **dormant** = no `build` season.

`build` looking one day ahead is the **pre-build night**: the run on the day before a season starts
builds it (hidden — not shown today), so the midnight flip shows a fresh row rather than last
season's collection. A back-to-back hand-over (Halloween shown today, Christmas tomorrow) builds
today's season; the next run switches.

### Server resolution (`context_builder._build_rows`)

For a row with seasons: `spec.seasons` (configured), `spec.season` (tonight's build instance or None),
placement `off` unless shown today (and the weekday allows). The engine never reads a clock.

### Engine

- **Membership** (`engine/seasons.py` + `TmdbClient.discover_all`): one TMDB discover per season per
  media type — keywords OR'd with `|`, no vote floor (TV-movie Christmas films have few votes); plus
  the genre (movies only) at the same ≥200-vote floor `tmdb_discover` uses. Sorted by release date,
  pages fetched in a small pool, stored as ONE compact cache entry for 7 days. Loaded once per run in
  `pipeline.run`, only for seasons some non-dormant row builds tonight, and only when the run has
  users. A failure is per season: rows that need it keep last night's row (pool failure), exactly as
  a dead source does today.
- **`season` source** (`gather_candidates`): the season's titles **in this server's libraries**, each
  with affinity `genre_coherence(person's top-3 seed genres, title genres)` (0.5–1.0). No seed.
  Library-only so it never creates request demand on its own.
- **Season filter** (`_candidate_pool`): after the gather, keep only season members (any TMDB member,
  so a missing Christmas film similar to their watches can still be requested under the row's request
  settings). Dropped titles are traced as `not_in_season`.
- **Pool key** gains the season slug; **recipe** gains `season=<slug>@<anchor>` (seasonal rows only,
  so no other row's recipe changes) — a new season instance fully rebuilds, bypassing cadence and idle
  hold.
- **Sources**: `effective_row_sources` drops `llm_web` for a seasonal row.
- **Cold start**: a seasonal row's "popular" fill is the season's titles in that library by TMDB
  rating, not the server's top-rated.
- **Rewatch**: finished titles are filtered to season members too ("Christmas films you've seen").
- **Dormant rows**: not gathered, not built, not delivered. `rows_considered = "out_of_season"`.
  In a run that covers the row (a full run, or the row's own cron) a person who owns one is still a
  promotion candidate, so its `off` placement is applied (identified through the delivery ledger); a
  run scoped to another row leaves it alone rather than re-place every row that person has. Dormant
  shared rows are promoted without being built, in any run with people in it. The midnight pass hides
  both kinds for the week after a season closes, whatever runs.
- **Names**: `render_row_name(..., season=Season|None)` fills `{season}` / `{season_emoji}`;
  without a season they are unfillable, exactly like `{top_seed}` without a seed. Engine call sites
  pass `spec.season`; server call sites rendering without a run treat both as dynamic.
- **Pick reason** for a seedless season pick: "Right for {season}, in genres you watch".
- **Shared rows**: the watcher tally is filtered to season members.

### Midnight (`jobs._rows_visibility`)

The gate also takes a seasonal row on a day its shown-state differs from any of the previous seven
days' (stateless: pure calls, a week back). One night would do if every pass succeeded; a week means a
failed pass whose retry lands after the next midnight, one deferred behind a run, or one skipped under
`paused_all` is still redone. A server whose only scheduled rows are seasonal runs the pass on a few
weeks a year, not 365. PATCHing a row's seasons/lead/after queues the pass like a `show_days` edit.

## Privacy (plex-safety)

- **Rule 1.** A seasonal row is an ordinary row: delivered unpromoted, excludes merged, then promoted.
  Pre-build nights deliver it with `off` placement. The midnight flip is the #102 job, which merges and
  checks every filter before showing anything.
- **Rule 4 / delete paths.** None added. Out of season is a visibility flip.
- **Dynamic titles.** Every "can this title be predicted without a run?" check (`{top_seed}` today)
  also asks about `{season}`/`{season_emoji}`: removal matches by ledger only, #121 claims use the
  ledger, and promote's static-title fallback maps an in-season row by tonight's title and skips a
  dormant one (its template cannot render).
- **Rule 8/10.** No new write path; existing dry-run and audit apply.

## Known limits (from the Architecture Review, 2026-09-15)

- **A missed pre-build shows last season's row.** If the night before a season opens does not rebuild
  the row — the season's list failed to load, or the row's schedule is not nightly — the midnight pass
  still shows it, wearing last season's name and picks until a run succeeds. Visible only to that row's
  own audience. The editor warns when the schedule is not nightly.
- **A library with nothing in the new season keeps what it had.** A films-and-shows seasonal row whose TV
  library holds no titles for a season is not rebuilt there, so last season's TV collection stays up for
  that season. The template is films-only for this reason (13 Christmas shows and 2 Halloween on a
  5,000-show library).
- **Two libraries: the second is rebuilt, not renamed, on a season switch.** The first library's rename
  takes the name, and Plex refuses the twin's rename (409) — existing `rename_or_keep` behaviour — so that
  collection gets a new ratingKey each season.
- **Duplicate-name checks expand a seasonal name into every catalogue season**, not only the row's own,
  so `{season} picks` is refused beside a plain "Valentine's Day picks" row even if it follows only
  Christmas. Refusing a name too many is recoverable; one collection for two rows is not.

## Inherited limitation (not new)

`_retired_rows` skips a switched-OFF row whose title needs a run to render, so switching off a
seasonal row whose name uses `{season}` leaves its collection for a rebuild — as `{top_seed}` rows
do today. Out-of-season does not go through that path.

## Non-goals (v1)

Owner-defined seasons/keywords, moving-date holidays (Easter, Thanksgiving, Hanukkah, Lunar New
Year, Diwali), weather seasons, per-season lead times, AI web search on seasonal rows, a count of
each season's films in the editor.

## Live proof (a large production server, 2026-09-15)

The branch's engine was copied into the running container at a scratch path and driven against the
real PMS, TMDB and watch cache — nothing persisted to the app database, scratch removed afterwards.

- **Dry, 8–10 people per season:** Halloween resolved to 2,817 TMDB films (641 on the server), Christmas
  to 3,671 (340). Mean pairwise row overlap 6.3% (Halloween) and 9.7% (Christmas), 71–98 distinct films
  across the people; today's ordinary rows overlap 3%.
- **Live, on the Guest canary only, owner-authorised:** run 1 created `🎃 Halloween picks` (marker,
  `Shortlist` + `Shortlist_moohouse` labels, 10 items, Friends' Home + Recommended); run 2 renamed the
  SAME ratingKey to `🎄 Christmas picks` and refilled it 10/10; run 3 (out of season) built nothing and
  took it off every surface; the collection was then deleted.
- **UI:** the real app on :5960 against the e2e fakes, driven with Playwright at 1280/1024/390 — template
  tile, Seasons group (new, saved, out of season, weekly-schedule warning), row card badges, run trace.

### Deployed proof (`d07fe5e4` on the maintainer's server, 2026-09-15)

- **No regression, measured before and after the deploy:** a dry run of every person (47, AI web search
  left out so two runs compare) on the old image and again on the new one: every status, row decision,
  pick and would-be diff identical. A read-only snapshot of every Shortlist collection (186: title,
  labels, items, surfaces), every account's share filter (48), the rows and the ledger: identical after
  the deploy, and identical again after the live test below. Migration 0092 ran with its pre-migration
  backup; existing rows came up with no seasons. Every page endpoint answered 200 and every page rendered
  at 1280 and 390 with no console errors; the log since the deploy holds no error.
- **Live, on the Guest canary only, through the real API:**
  - refusals: an unknown field, and a season name on a row with no seasons (422, nothing created);
    a new row keeps its request settings;
  - a Halloween row (90 days' lead) reported `showing Halloween until 31 Oct` on the server's clock;
    its scoped run delivered 10 films, all on TMDB's Halloween list, as a marked, labelled collection;
  - the rename screen renamed it in place (same ratingKey, marker and items);
  - lead days cut to 0: the PATCH's visibility pass took it off every surface and kept its items, and a
    run marked it `out_of_season` and left it untouched;
  - deleted: collection, row and ledger entries gone.

Still not covered live: a seasonal row turning over at the real midnight, and a shared seasonal row (a
subset of one cannot reach the two-watcher floor, and an everyone-row would show on other people's Home).

## Build order

1. `engine/seasons.py`: catalogue, windows, resolution — tests first.
2. TMDB `discover_all` + membership loading/cache — tests.
3. Engine: RowSpec fields, `{season}` rendering, season source + filter + affinity, pool key, recipe,
   sources, cold start, rewatch, dormancy (per-person + shared), pipeline loading, trace/reason.
4. Server: migration 0092 + model, API in/out/PATCH/validation/`season_status`, `GET
   /api/collections/seasons`, context builder, midnight gate, dynamic-title call sites.
5. Web: generated types, template, Seasons group, card badge, placeholder hint/render, sources note,
   trace labels, `out_of_season` decision.
6. Docs: guides/rows.md, reference/api.md, reference/settings.md; regenerate `llms-full.txt`.
7. Full verification (pytest, pnpm test, tsc -b, eslint, e2e), Architecture Review, then ask to commit.
