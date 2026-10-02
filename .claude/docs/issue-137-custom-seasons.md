# Custom seasons for Seasonal rows (issue #137)

Status: **designed 2026-10-02**. It extends [discussion-124-seasonal-rows.md](discussion-124-seasonal-rows.md),
and every decision there still holds unless this file says otherwise. The mockup the owner agreed is the
"Custom Seasons Mockup" canvas: a row editor where you tick seasons, add a ready-made one, or create your own.

## The ask

From mrain1p in #137: where do the seasonal lists come from, and could owners pick or add holidays such as
St Patrick's, Thanksgiving, 4th of July, New Year, Mother's Day and Father's Day? It is a question plus
a feature request. Nothing is broken.

## Measurements that shaped the design (SFLIX, 2 Oct 2026, read-only)

| Question | Answer |
| --- | --- |
| How many library films does each holiday's own TMDB tag give? (10,030 films) | New Year's 31, Thanksgiving 15, 4th of July 8, Hanukkah 6, Easter 5, St Patrick's 2, Mother's Day 2, Diwali 0, Lunar New Year 0. Father's Day's tag (195439) holds 3 films on TMDB, none in this library. |
| Do broader tags fix it? | They fill the count with the wrong films. Mother's Day plus "mother–daughter relationship" etc. gives 235, but TMDB's top films there are Forrest Gump, Joker, The Shining and Black Swan. Father tags give 270 (Interstellar, Endgame). Irish tags give a usable 58 for St Patrick's (In Bruges, Banshees, Brooklyn). |
| Can a season read an owner's Plex collection? | Yes. 150 movie collections (43 smart). Members of smart and regular collections read in 0.05–1.4s (46–1,187 films), and every one carried a TMDB guid. |
| Can the editor search the library by title? | `section.search(title=…, maxresults=20)` takes 0.06s, and every result carried a TMDB guid. |
| Can the API count "in your libraries" without a run? | The engine's cached index (`index3:{section}:{signature}`) missed on every section because the library had changed since the last run. A full scan of 10,030 films takes 7.5s. |
| How does Kometa do it? | Hand-curated IMDb/MDBList lists, 19 holidays, fixed US date ranges. Its holiday template sets `delete_not_scheduled: true`, so a Kometa seasonal collection **does not exist** outside its window and is recreated, with a new ratingKey, each year. |

## Decisions

1. **Everything happens inside the Seasonal row editor.** There is no Seasons page. A season created
   there is saved server-wide, so any Seasonal row can tick it. The owner agreed this after seeing both
   mockups.
2. **Built-ins stay in code and are not editable.** Valentine's, Halloween and Christmas keep their
   definitions, their row-level timing and their recipes, so no existing row rebuilds when this ships.
   Custom seasons live in a new `seasons` table.
3. **A season's films are the union of five sources, and only films in the owner's libraries are used.**
   The sources are TMDB tags, one optional TMDB genre (at the existing `DISCOVER_MIN_VOTES` floor),
   Plex collections, and films the owner picks by hand. Genres to leave out apply to tag and collection
   films, but never to a film the owner picked by hand. This answers "what if TMDB has no tag" and
   "look within my library".
4. **Plex labels are not a source.** A Plex smart collection filtered on a label does the same job and
   takes seconds to make in Plex. One label on SFLIX matches 10,028 items, which takes 8.8s to read.
5. **Collections are referenced by section and title, never by ratingKey.** Kometa deletes a seasonal
   collection out of season and recreates it with a new ratingKey, so a ratingKey would break every
   year. A collection missing on a given night adds nothing that night and is logged. The editor says
   so.
6. **Shortlist only reads collections.** A foreign collection's items, title, labels and artwork are
   never written (plex-safety rule 4). The only access is `section.collections()` plus `collection.items()`.
7. **Three date rules.** A fixed day (17 March), the nth or last weekday of a month (4th Thursday of
   November), or N days from Western Easter (Mothering Sunday is −21). Lunar and Hebrew calendars
   (Lunar New Year, Diwali, Hanukkah) are out: TMDB tags 0–6 such films here anyway. 29 February is
   refused with a message.
8. **Custom seasons carry their own timing; built-ins keep the row's.** This revises #124 decision 8
   for custom seasons only. Short holidays sit close together, and a 30-day lead would show
   "St Patrick's picks" from Feb 15. The row's control is relabelled "Built-in seasons show from…".
9. **Ready-made seasons are presets that open the editor pre-filled.** Clicking Add never creates a
   season blind. The owner sees the count and the sample, adjusts, then saves. Presets are defined in
   code (`seasons.PRESETS`). A preset already added is hidden, tracked by `seasons.preset`.
10. **The editor tells the truth about how many films there are, relative to the row it was opened
    from.** Fewer films than the row's size: "too few to fill this row". In a per-person row, fewer than
    100: "people's rows will be much alike — works best in a shared row". (#124 judged 63 too few to
    make rows differ, and this is a heuristic, not a measured line.) Otherwise: enough. Saving is never
    blocked on count. Saving IS blocked when the season has no source at all.
11. **Editing a source rebuilds the row.** The recipe gains `season=<slug>@<anchor>#<content hash>` for
    custom seasons only (built-ins keep today's recipe). A name or emoji change only renames the row.
12. **Deleting a season unticks it in every row, in one transaction.** It is refused, naming the rows,
    when any row has it as its only season. The editor lists "Also used by…" whenever another row
    uses the season.
13. **Names are unique across the catalogue (case-insensitive), built-ins included.** Row titles render
    `{season}`, so two seasons sharing a name would collide.
14. **Slugs are made from the name at creation and never change.** Rows store slugs, and the recipe
    carries the slug.
15. **A season that finds nothing keeps last season's collection hidden** (final review C-1, 2026-10-02).
    A library where a seasonal row builds nothing for tonight's season keeps the collection it last built,
    with that season's title and films. Promotion never shows a seasonal row's collection unless it was last
    BUILT for tonight's season and year (`slug@anchor` recorded per library in `deliveries.season`, migration
    0096, else read from a per-person row's stored picks' recipe; compared on slug and year, so moving a
    season's date within the year hides nothing). A mismatch is treated as dormant: hidden, placement off.
    Nothing is deleted. A collection with neither record is promoted as before, so no row loses its place
    on the night this ships.
16. **The editor counts for its row** (final review I-1). `POST /preview` takes the row's `media` and
    `library_keys`, and the editor says "films", "shows" or "titles" to match. It also returns the films and
    shows separately (null for a type the row has no library of), and a row of both is "too few" when either
    half is, since each library is filled from its own type.
17. **A season's name cannot give two rows one title** (final review I-2). `POST`/`PUT` render every
    `{season}` row's template with the proposed name and emoji and run the row editor's duplicate-title
    check; a row PATCH that ticks a season checks the title it gives the row in that season.

## Engine

- `seasons.DateRule(kind: "fixed"|"nth"|"easter", month, day, nth, weekday, offset)` with
  `anchor(year) -> date`. `nth` is 1–4 or −1 for last, `weekday` uses Python's numbering (Monday=0), and
  `easter(year)` is the anonymous Gregorian computus. Built-ins become `DateRule("fixed", m, d)`.
- `Season` gains `rule`, `lead_days | None`, `after_days | None`, `collections: tuple[CollectionRef]`
  (section key + title), `picks: tuple[(tmdb_id, MediaType)]`, `builtin`, and `content_hash`.
  `month`/`day` go away, since `rule` replaces them.
- **Catalogue.** Every function that read the module-level `SEASONS` now takes a
  `catalogue: Mapping[str, Season]`, a required argument with no default, because a silent default
  would ignore custom seasons and hide rows.
  - `BUILTIN_SEASONS` stays in code.
  - The server builds `built-ins + DB` once per request, job, or run (`services/season_catalogue.py`).
  - The engine receives it as `EngineConfig.seasons`.
  - Affected: `normalise_slugs`, `_windows`, `shown_on`, `build_on`, `last_shown_day`, `next_after`,
    `row_season_on`, `rows.row_shown_today`, `placeholders.catalogue_seasons`/`season_renderings`,
    `delivery` (756), `collection_reconcile` (216, 1068), `pipeline._load_season_titles`.
- `_windows` uses `season.rule.anchor(y)` and the season's own lead/after, falling back to the row's.
- `load_titles(tmdb, plex, season, library_index)`:
  - The TMDB queries are unchanged.
  - **Collection members:** `PlexClient.collection_members(section_key, title)` returns a list of
    `(tmdb_id, MediaType)`, or `None` when the collection is absent.
  - **Picks** are added to the season's titles.
  - **Bare ids** not already covered by a TMDB result get `TmdbClient.details`, which is cached for 7
    days. An adapter maps `genres → genre_ids` and keeps exactly the keys the pool reads (`id`,
    `title`/`name`, `release_date`/`first_air_date`, `genre_ids`, `vote_average`, `vote_count`,
    `poster_path`, `overview`, `original_language`).
  - **Limits:** at most 1,000 Plex-sourced titles per season, with details fetched by 4 workers.
  - **Errors:** a 404 on details skips that title. Any other TMDB or Plex failure fails the season,
    and its rows keep what they have, as today.
- `discover_all(..., workers=1)`: pages after page 1 may be fetched concurrently. Only the API passes
  `workers > 1`.
- `TmdbClient.search_keywords(q)` calls `/search/keyword`. Each result's TMDB film count comes from page 1
  of discover, fetched concurrently and cached.
- `PlexClient.list_collections()`, `collection_members()` and `search_titles(q, limit)` are new and
  read-only.

## Server

- **Table `seasons`** (migration 0095): id, slug (unique), name, emoji, rule_kind, month, day, nth,
  weekday, easter_offset, lead_days, after_days, tags JSON `[{id,name}]`, genre (nullable int),
  excluded_genres JSON, collections JSON `[{section_key, section_title, title}]`, picks JSON
  `[{tmdb_id, media_type, title, year}]`, preset (nullable), created_at, updated_at.
- **API**, all under a new owner-only router at `/api/seasons`, which replaces `GET /api/collections/seasons`:
  - `GET` (extended): every season, with `builtin`, `rule`, `rule_label`, `next_dates[2]`, its own
    timing, its sources (custom only), and `used_by`.
  - `GET /presets`.
  - `POST`, `PUT /{slug}` and `DELETE /{slug}`, applying decision 12.
  - `POST /preview`: takes a draft and returns `next_date`, `total`, per-source marginal counts,
    `per_tag` and `per_collection` in-library counts (plus `found`), and up to 10 sample titles.
  - `GET /tmdb-tags?q=`, `GET /plex-collections?q=` and `GET /library-search?q=`.
- **Library index for the API:** read the engine's cache for the current signature, otherwise scan and
  hold it in process memory for 10 minutes per section and signature. The editor warms it when it opens.
- **Validation:**
  - Name: 1–40 characters, unique.
  - Emoji: 1–8 characters.
  - Date rule: valid month and day (29 Feb refused), nth in {1, 2, 3, 4, −1}, Easter offset ±63.
  - Timing: lead 0–90, after 0–30.
  - Source limits: up to 20 tags, 10 collections, 200 picks, 1 genre and 5 left-out genres.
  - At least one source.

## UI (row editor, `row-seasons-field.tsx`)

- **Season list.** Every season on the server is a checkbox row showing:
  - the emoji and name, with a Built in or Yours badge;
  - the date, as a rule label plus the next date;
  - its timing;
  - its film count (loaded lazily);
  - a warning against this row's size and mode;
  - Edit, for custom seasons only.

  The rule that the last ticked season can't be unticked stays.
- **Add more seasons.** A grid of presets not yet added, each with its date and count (loaded
  lazily). Add opens the editor pre-filled. A Create your own button opens it blank.
- **Timing.** "Built-in seasons show from [30] days before and stay [0] after." It appears only when a
  built-in season is ticked.
- **Year strip.** It shows the ticked seasons' windows, using `next_dates` from the server so the date
  rules are never re-implemented in TypeScript, and a line explaining any overlap.
- **Season editor** (a large `Dialog`, which sits under the row editor). Its sections are Name, When,
  Films, and a summary panel.
  - **Films:**
    - TMDB tags: search, then Add each tag, which joins the chosen list with its in-library count. When
      a search finds nothing, a no-tag message points to broader words, a collection, or picks.
    - From your library: a searchable list of collections showing their library and count.
    - Picked by hand: a library title search, with the chosen films as chips.
    - Also a genre: optional.
    - Leave out genres.
  - **Summary panel:** the total, the verdict (decision 10), the counts per source, a sample, and
    "Also used by".
  - **Footer:** Save (which ticks the season in this row), Cancel, and Delete with a confirmation.

## Presets (tag ids verified against TMDB 2 Oct 2026)

| Preset | Date | Timing | Tags |
| --- | --- | --- | --- |
| 🎆 New Year's Eve | 31 Dec | 7 before, 1 after | 613 new year's eve, 252123 new year |
| 🎇 4th of July | 4 Jul | 7 | 235503 independence day, 159743 fourth of july, 282190 4th of july, 190024 american revolution, 2407 fireworks |
| 🦃 Thanksgiving (US) | 4th Thu Nov | 14 | 4543 thanksgiving |
| 🍁 Thanksgiving (Canada) | 2nd Mon Oct | 7 | 4543 thanksgiving |
| ☘️ St Patrick's Day | 17 Mar | 7 | 209352 st. patrick's day, 10310 leprechaun, 14985 ireland, 299594 irish, 4729 dublin, ireland. Leaves out Horror, which drops the *Leprechaun* slashers |
| 🐣 Easter | Easter Sunday | 14 | 9921 easter, 9923 easter bunny |
| 💐 Mother's Day (US, CA, AU, NZ) | 2nd Sun May | 7 | 173983 mother's day |
| 💐 Mothering Sunday (UK, IE) | Easter −21 | 7 | 173983 mother's day |
| 👔 Father's Day (US, UK, CA, IE) | 3rd Sun Jun | 7 | 195439 father's day: 3 films on TMDB, so the note asks for a collection or picks |
| 👔 Father's Day (AU, NZ) | 1st Sun Sep | 7 | 195439 father's day: same |

## Non-goals

Plex labels as a source (decision 4), lunar and Hebrew calendars, editing built-ins, list sources (Trakt,
MDBList, IMDb lists), and AI-suggested tags.

## Testing

- **Unit:**
  - Computus against known Easter dates for 2024–2030.
  - The nth and last weekday rule.
  - Windows with per-season timing, and that built-ins are unchanged.
  - Catalogue merge.
  - `load_titles` with collections (present and missing), picks, the details adapter, and a 404 skip.
  - The recipe hash appears for custom seasons only.
  - Presets carry only measured tag ids.
- **API integration:**
  - CRUD and validation.
  - Delete semantics.
  - Name uniqueness against built-ins.
  - Preview with fakes.
  - The migration: drift test plus its own class.
- **Web:**
  - The season list, presets, the editor's flows (no-tag, collection, picks, verdicts) and delete
    confirmation.
- **e2e:** create a custom season from the Seasonal row editor against `fake_plex`. The fake gains
  title search on `/all`, and the fake TMDB gains `/search/keyword` and `/discover`.
- **Live, read-only:** run the new preview code against SFLIX and match the measured totals above.
