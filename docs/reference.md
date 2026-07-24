# Reference

## Environment variables (container)

| Variable                                                                   | Default   | Live or seed                                                                                                                                                                                                                                                     |
| -------------------------------------------------------------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PORT`                                                                     | `5959`    | live                                                                                                                                                                                                                                                             |
| `TZ`                                                                       | `Etc/UTC` | live                                                                                                                                                                                                                                                             |
| `PUID` / `PGID`                                                            | `1000`    | live                                                                                                                                                                                                                                                             |
| `SHORTLIST_CONFIG`                                                         | `/config` | live                                                                                                                                                                                                                                                             |
| `PLEX_URL`, `PLEX_TOKEN`, `TAUTULLI_URL`, `TAUTULLI_APIKEY`, `TMDB_APIKEY` | —         | **seed once**: copied into settings on first boot, ignored afterwards                                                                                                                                                                                            |
| `LOG_LEVEL`                                                                | `DEBUG`   | **seed once**: initial value for the `log.level` setting; change it live in Settings → Advanced                                                                                                                                                                  |
| `SHORTLIST_DRY_RUN`                                                        | unset     | live: when set (`1`/`true`), EVERY run is forced to dry-run — the app builds its clients and logs the would-be changes but writes NOTHING to Plex/plex.tv. Safe mode for a demo/test instance pointed at a real server (even a manual "Run now" can't modify it) |
| `SHORTLIST_ENABLE_DOCS`                                                    | unset     | live: when set (`1`), exposes the API docs at `/api/docs` and `/api/openapi.json` (off by default)                                                                                                                                                               |

## Settings keys (DB-backed; Settings UI or `PUT /api/settings`)

| Key                                                   | Default                            | Notes                                                                                                                                                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `plex.url` / `plex.token`                             | —                                  | token stored Fernet-encrypted, redacted in API                                                                                                                                                                                                                                                                                                                                                          |
| `tautulli.url` / `tautulli.apikey`                    | —                                  | optional                                                                                                                                                                                                                                                                                                                                                                                                |
| `tmdb.apikey`                                         | —                                  | required for personal mode                                                                                                                                                                                                                                                                                                                                                                              |
| `curator.provider`                                    | `none`                             | `anthropic` \| `openai` \| `openai_compatible` (any local/self-hosted OpenAI-API server) \| `google` \| `none`. `ollama` is the pre-merge name, still accepted                                                                                                                                                                                                                                          |
| `curator.api_key` / `curator.model`                   | —                                  | BYO key; sensible default model per provider                                                                                                                                                                                                                                                                                                                                                            |
| `curator.openai_base_url`                             | —                                  | your local/self-hosted server's URL (Ollama, llama.cpp, LM Studio, vLLM, LocalAI, OpenRouter). A bare host gains `/v1` automatically. `curator.ollama_url` is the pre-merge key, still read as a fallback                                                                                                                                                                                               |
| `row.name_template`                                   | `✨ {library_name} Picked for You` | `{library_name}` (the delivering library), `{top_seed}` and `{user}` placeholders                                                                                                                                                                                                                                                                                                                       |
| `row.size`                                            | `15`                               | any whole number 5–40 (free number picker in the UI); size PER library — each library a row targets fills to this                                                                                                                                                                                                                                                                                       |
| `rows.hub_anchor`                                     | `{}`                               | Per-library placement in Plex's Recommended shelf: `{"<sectionKey>": {"top": true}}` (very top, no anchor) **or** `{"<sectionKey>": {"anchor": "<collection title>", "before": false}}` (next to a collection). Empty = Plex's default order (rows land last, under a co-managing tool like Kometa). Re-applied at end of each run; only Shortlist's own hubs move, the anchor is read-only             |
| `rows.manage_shelf_order`                             | `true`                             | master switch for Shortlist touching the Recommended-shelf ORDER at all. `true` (default) applies your `rows.hub_anchor` placement at the end of each run; `false` = never reorder the shelf, leaving the order entirely to a co-managing tool like agregarr/Kometa                                                                                                                                     |
| `recommendations.watched_pct`                         | `0.0`                              | max share of a row that may be already-finished titles (0 = all fresh, 1 = no filtering); per-row overridable. The "already-finished" set is each user's COMPLETE watched set, read from Plex AS them each run — so it includes titles they only _marked_ watched, not just played (see [Watched titles](#watched-titles-and-why-one-can-still-be-recommended)).                                        |
| `recommendations.freshness`                           | `0.5`                              | REFRESH CADENCE, not a nightly shuffle: 0 = frozen once built, 1 = rebuild every night, in between = every N days (0.5 ≈ weekly). On a refresh night the strongest ~⅔ stay and the weakest third is swapped for new picks; other nights the row is reused unchanged (no rebuild, no Plex write). Per-row overridable.                                                                                   |
| `recommendations.recent_count`                        | `10`                               | how many of a person's most recent watches the `llm_web` source searches per row (one cached search each, "what to watch if you liked X"); results cached 14 days and shared across users so a popular title is searched once server-wide; overridable per row, and per person on a row (User → Rows), each falling back to the next: user → row → this global (1–25)                                   |
| `privacy.hide_shared_from_disabled`                   | `true`                             | when on, disabling a user hides EVERY shared row from them too — even public "Popular on this server" rows — so a disabled user sees nothing from Shortlist. Off = disabled users still see public shared rows like any account with library access. Re-enabling (or turning this off) restores the rows on the next run.                                                                               |
| `candidates.sources`                                  | `["tmdb_similar","tmdb_discover"]` | sources to pool: `tmdb_similar`, `tmdb_discover`, `trakt`, `llm_web`. Each enabled source gets a fair share of the pooled candidates — a wide source can't crowd out a narrow one                                                                                                                                                                                                                       |
| `llm_web.search_provider`                             | `auto`                             | how the `llm_web` source searches: `native` (the provider's own web-search tool — Claude/GPT/Gemini only), `exa` (the Exa search API — every provider, including a local server), or `auto` (the default: the provider's own tool AND Exa **unioned** when both are set up — they surface mostly different titles — else whichever one)                                                                 |
| `trakt.client_id`                                     | —                                  | Trakt API key; required for the `trakt` source; encrypted                                                                                                                                                                                                                                                                                                                                               |
| `exa.apikey`                                          | —                                  | Exa web-search API key; powers the `llm_web` source for any provider (the only web-search path for a local model); encrypted                                                                                                                                                                                                                                                                            |
| `plex.timeout_s`                                      | `45`                               | seconds to wait on a single PMS call before giving up and retrying. Reads are near-instant, but rebuilding a big library's collection (a TV row on a large server) legitimately takes 15-20s+, so too low a value times those out and forces a wasteful retry. Range 5-300. Advanced                                                                                                                    |
| `plextv.throttle_s`                                   | `0.0`                              | FLOOR (min seconds) between plex.tv writes. `0` = fire as fast as plex.tv accepts; the client backs off adaptively on a 429 (jumps to ≥1s, doubles, capped 30s, eases back on clean writes), so 0 is safe. Range 0–60                                                                                                                                                                                   |
| `log.level`                                           | `DEBUG`                            | container log verbosity: `ERROR`\|`WARNING`\|`INFO`\|`DEBUG`\|`TRACE`. DEBUG (default) narrates a run in full — per-source candidate counts, AI calls with timing/tokens, cache hits, throttle waits; TRACE adds full AI prompts; INFO trims to stage narration. Applied live. TRACE reaches the container log only — the in-app Logs view reads a file sink opened at DEBUG, so it has no TRACE filter |
| `run.concurrency`                                     | `4`                                | how many users a run processes at once (1–16). Only history/candidate/AI reads overlap; every Plex + plex.tv write stays serial. 1 = fully sequential                                                                                                                                                                                                                                                   |
| `runs.retention`                                      | `100`                              | how many past runs to keep; after each run older ones (and their picks) are pruned to this count — except a run still inside the 30-day watch-credit window, so pruning can never cost the report a hit. `0` = keep everything                                                                                                                                                                          |
| `paused_all`                                          | `false`                            | Danger-Zone "stop all runs" switch; pauses without disabling anyone                                                                                                                                                                                                                                                                                                                                     |
| `requests.enabled`                                    | `false`                            | ask Radarr/Sonarr for picks the library lacks                                                                                                                                                                                                                                                                                                                                                           |
| `requests.radarr.url` / `.apikey`                     | —                                  | Radarr (movies); key stored Fernet-encrypted, redacted                                                                                                                                                                                                                                                                                                                                                  |
| `requests.radarr.quality_profile_id` / `.root_folder` | `0` / —                            | picked from dropdowns in the UI (fetched from Radarr)                                                                                                                                                                                                                                                                                                                                                   |
| `requests.sonarr.url` / `.apikey`                     | —                                  | Sonarr (shows); key stored Fernet-encrypted, redacted                                                                                                                                                                                                                                                                                                                                                   |
| `requests.sonarr.quality_profile_id` / `.root_folder` | `0` / —                            | picked from dropdowns in the UI (fetched from Sonarr)                                                                                                                                                                                                                                                                                                                                                   |
| `requests.rating_source`                              | `tmdb`                             | `tmdb` (no setup) \| `imdb` \| `trakt` \| `tomatoes` (Rotten Tomatoes) \| `metacritic` — all non-TMDB sources come from MDBList, normalised to a 0–10 scale                                                                                                                                                                                                                                             |
| `requests.mdblist.apikey`                             | —                                  | free MDBList key; required for any non-TMDB rating source; encrypted. One lookup returns every source and is cached 7 days; on a 429 (daily cap) the gate falls back to TMDB and the owner is notified                                                                                                                                                                                                  |
| `requests.min_rating`                                 | `7.0`                              | score floor (0–10) on the chosen source                                                                                                                                                                                                                                                                                                                                                                 |
| `requests.min_votes`                                  | `100`                              | vote-count floor on the chosen source                                                                                                                                                                                                                                                                                                                                                                   |
| `requests.min_demand`                                 | `1`                                | request only titles wanted by ≥ N distinct people                                                                                                                                                                                                                                                                                                                                                       |
| `requests.min_year`                                   | `0`                                | `0` = no lower bound; else request only titles from ≥ this year (a show is judged by its first-air year)                                                                                                                                                                                                                                                                                                |
| `requests.max_year`                                   | `0`                                | `0` = no upper bound; else request only titles from ≤ this year. With `min_year`, forms a release-year window; a candidate with no known year is excluded whenever either bound is set                                                                                                                                                                                                                  |
| `requests.max_per_run`                                | `5`                                | hard cap on titles **auto**-requested per run, both apps                                                                                                                                                                                                                                                                                                                                                |
| `requests.auto_send`                                  | `true`                             | `false` = fully manual; every qualifying title is queued                                                                                                                                                                                                                                                                                                                                                |
| `requests.auto_min_demand`                            | `3`                                | auto-send only titles wanted by ≥ N distinct people                                                                                                                                                                                                                                                                                                                                                     |
| `requests.auto_min_rating`                            | `8.0`                              | ...and rated ≥ this on the chosen source; rest are queued                                                                                                                                                                                                                                                                                                                                               |
| `requests.tag`                                        | `shortlist`                        | global tag on every requested title (created in the app; `""` = no tag)                                                                                                                                                                                                                                                                                                                                 |

## API

The interactive API docs are off by default (they'd disclose the whole surface unauthenticated);
set `SHORTLIST_ENABLE_DOCS=1` to expose `/api/docs` and `/api/openapi.json` for local development
(also required if you regenerate the frontend API types with `pnpm -C web gen:api` against a live
server). Highlights:

```
POST /api/auth/pin · GET /api/auth/pin/{id} · GET /api/auth/session · POST /api/auth/logout
POST /api/setup/probe · POST /api/setup/link · GET/PUT /api/setup/state
GET  /api/users · PATCH /api/users/{id} {enabled?, request_tag?, prefs?} · POST /api/users/sync (shared + Home users from plex.tv, plus the server owner, whom that list never returns)
POST /api/users/set-enabled {enabled} (bulk enable/disable every user at once)
GET  /api/users/{id}/rows · PUT /api/users/{id}/rows/{collection_id} {muted?, row_size?, recent_count?} (per-person, per-row: `recent_count` (1–25) overrides how many recent watches the `llm_web` source searches for this person on this row; null on any field clears it back to the row's own setting)
GET  /api/users/{id}/runs · GET /api/users/{id}/history (recent watches; each item carries `title`, `media_type`, `year`, plus `season`/`episode`/`episode_title` for TV)
GET/POST /api/collections · PATCH/DELETE /api/collections/{id} (incl. `request_tag`, `candidate_sources`, `library_keys`, `hub_anchor` — per-row shelf-placement override, and `poster` — custom row artwork {mode: ""|upload|generate, title, subtitle, style})
POST /api/collections/{id}/cleanup {dry_run?} (remove this row's Plex collections for everyone; dry-run previews)
POST /api/collections/{id}/poster/upload (multipart image) · GET/DELETE /api/collections/{id}/poster/image (serve/remove uploaded artwork) · POST /api/collections/{id}/poster/preview {title,subtitle,style} -> generated sample image
GET  /api/system/image-provider -> {capable, provider, reason} (can the AI provider generate poster images — drives the row editor's Generate gate)
GET  /api/system/logs?level=&q=&limit= (parsed + redacted log lines) · GET /api/system/logs/download (all log files, redacted, as a zip)
GET  /api/system/libraries -> [{key, title, type}] (the server's Plex libraries, for the row editor)
GET  /api/system/libraries/{key}/collections -> [{title}] (a library's managed collections — anchor choices for row placement, excludes Shortlist's own)
GET  /api/system/owned-collections -> {collections:[{library,title,label,rating_key,kind,slug,orphan}], total, orphans} (cleanup audit: every shortlist-labelled collection ON PLEX, drift-flagged, DB-independent)
GET  /api/runs · GET /api/runs/summary · GET /api/runs/{id} (each user carries `status`, `error`, `reason` — why a `skipped` user built nothing — and `has_trace`) · GET /api/runs/{id}/users/{user_id}/trace -> {username, display_name, status, error, reason, trace, breakdown} (the full per-user pipeline trace — history (with true distinct-title watched totals per library, split by media type) / seeds with each seed's weight ingredients, each source's queries+returns tagged with their fate (kept / already_watched / not_in_your_libraries / excluded_genre / lost_ranking_cutoff), the web-search/RAG prompts, resolved vs. hallucinated titles (the AI's resolved proposals carry the same fate so the UI marks each kept vs. dropped), plus `error`/`reason` for a failed or skipped person and `breakdown` (the delivered picks per library); a cold-start user carries a trace too (their thin history + a synthetic `cold_start` source), so `has_trace` is set and the "How we picked" page renders for them; fetched on demand, `trace: {}` on runs predating the feature) · GET /api/runs/{id}/log (activity feed) · POST /api/runs {user_ids?, collection_ids?, dry_run?} · POST /api/runs/{id}/cancel · DELETE /api/runs (clear all run history)
GET  /api/requests · GET /api/requests/status -> {request_id: "downloaded"|"downloading"|"queued"|"monitored"|"unmonitored"|null} (live Sonarr/Radarr status for SENT items; fetched separately so the list itself makes no Arr calls) · POST /api/requests/send {ids, dry_run?} · POST /api/requests/reject {ids} (permanent) · POST /api/requests/restore {ids} (un-reject → back to Waiting) · POST /api/requests/delete {ids} (removable; can re-surface) · POST /api/requests/clear {ids} (hide SENT items from the log without un-sending — the tombstone stays so the title isn't re-requested)
GET  /api/events (SSE) · GET /api/events/log (audit feed)
GET  /api/notifications -> {items[]} · POST /api/notifications/dismiss {id} (dismiss one alert)
GET/PUT /api/settings · POST /api/settings/test/{plex|tautulli|tmdb|llm|radarr|sonarr|mdblist|trakt|exa}
GET  /api/settings/arr/{radarr|sonarr}/options -> {quality_profiles, root_folders}
POST /api/settings/curator/models {provider?, api_key?, ollama_url?} -> {provider, models[]} (models the provider offers; the body lets the picker list the provider being edited before it is saved — blank fields fall back to saved settings, a redacted key means "use the saved key"; [] = free-text fallback)
GET  /api/report -> {overall, trend[], per_user[], per_row[], recent[], watch_sync, coverage, runs, requests, top_titles} (delivered-vs-watched hit rates, from picks.watched_at)
POST /api/report/sync -> 202 (kick off a watch-history sync — re-reads every user's watched set from Plex so hit rates and "N titles watched" stay fresh between runs; writes nothing to Plex)
GET  /api/system/health · GET /api/system/version · GET /api/system/debug (plain-text diagnostics bundle) · POST /api/system/uninstall {confirm: "UNINSTALL"}
GET  /api/system/api-token -> {enabled, created_at, token} (owner-gated; token revealable) · POST /api/system/api-token -> {token, created_at} (generate/replace) · DELETE /api/system/api-token (revoke)
GET  /api/setup/servers (Plex server picker during onboarding) · GET /api/setup/state
```

The AI provider (`curator.provider`) no longer ranks a fixed candidate pool — the engine does the
diversification and writes the genre-template reasons itself. The provider's one remaining job is the
`llm_web` source: it turns a person's recent watches into web searches for what to watch next. So a
run needs a provider only when `llm_web` is enabled; every other source is provider-free, and with
`curator.provider = none` you still get full rows ranked by score with plain reasons.

**`PUT /api/settings` validates values, not just keys.** `plextv.throttle_s` must be 0–60 (0 = fire
as fast as plex.tv accepts, with adaptive 429 backoff), `row.size` must be 5–40, `paused_all` must be a real boolean,
and `candidates.sources` / `curator.provider` are checked against their known values.

Candidate sources are set globally (`candidates.sources`) and can be overridden per row
(`collections.candidate_sources`, `[]` = inherit the global set; valid values: `tmdb_similar`,
`tmdb_discover`, `trakt`, `llm_web`). `llm_web` proposes titles to watch next from a
live web search, each resolved via TMDB search then library-verified. It works on **every** AI
provider via `llm_web.search_provider`: `native` uses the provider's own web-search tool (Claude,
GPT, or Gemini), `exa` uses the Exa search API (`exa.apikey`) — the only path for a local
model — and `auto` (the default) UNIONS the provider's own tool and Exa when both are set up (they
surface mostly different titles, so the pool is widest), else whichever one is available. When a
source's dependency is missing, the Settings UI keeps the toggle usable but shows an inline fix
(enter the key right there, or set up an AI provider) — it never reads as on while silently doing nothing.

Config changes reconcile onto Plex immediately, without waiting for a run. Deleting a row, disabling
a user, and dropping a user from a row's audience all remove the now-stale collections (a removal, so
gate-exempt); renaming a row retitles its collections in place for every user (privacy-neutral — the
hiding filter is keyed on the row's label, which never changes). A per-person row's per-user
collection is found by the exact title the last run delivered for it (the run's persisted breakdown),
scoped to that user's own label, so a reconcile can never touch another user's row or a foreign
(Kometa) collection. Each row also has a **Remove from Plex** button (`POST
/api/collections/{id}/cleanup`, dry-run-able) for an on-demand sweep. Every reconcile is audited.

A row builds a Plex collection in each library it targets (`collections.library_keys`, a list of
Plex section keys; `[]` = every library of the row's media type — the default). A row's `media` is
derived from the types of its selected libraries. This lets an owner point a row at a specific
library (e.g. only "4K Movies") on a server with several libraries of one type. A row builds **per
library**: each targeted library seeds from its own watched history and fills to `row.size` on its
own, so a movies-and-TV watcher gets a full movie row AND a full TV row.

Placement is per row (`collections.placement`: `both` \| `home` \| `library`, default `both`), which
sets which Plex surfaces the row appears on once promoted (Home, the library's Recommended tab, or
both). WHERE in that shelf it sits is the **Position** control (`collections.hub_anchor`, per library:
`{"top": true}` or `{"anchor": "<collection>", "before": bool}`); it
replaces the old `pin_top` toggle (still honoured for rows not yet re-saved). This order is Plex's
Managed Recommendations, which are **server-wide** — Plex exposes no per-viewing-user hub order.

Request tags are three-layered: the global `requests.tag` setting, a per-user `request_tag`
(`PATCH /api/users/{id}`), and a per-row `request_tag` (`collections`, per-person rows only —
shared rows never request). A requested title is tagged with the union of the global tag, every
wanting user's tag, and the tag of every per-person row that user is in the audience of; the queued
tags round-trip through `GET /api/requests` (`tags[]`) and are applied on `send`.

Before queuing, the request pass reconciles the missing pool against the Arrs (one bulk fetch each,
failing open on error): a title Sonarr/Radarr already tracks is dropped — not really "missing", just
not imported into Plex yet — matched on tmdbId for movies and tvdbId for shows (the candidate's TVDB
id is resolved once and reused for the send). A title on an Arr import-exclusion list (usually a past
delete) is kept but flagged (`excluded` on `GET /api/requests`) and never auto-sent, so the inbox can
warn that approving it is a no-op until the exclusion is removed in the Arr. A sent title records the
Arr's `titleSlug` (`arr_slug` on `GET /api/requests`) so the Sent log deep-links straight to its
Sonarr/Radarr page; **Clear** (`POST /api/requests/clear`) hides a sent entry via a `hidden` flag without
deleting the tombstone that stops a still-downloading title being re-requested.

All endpoints except `/api/system/health` require the owner session; mutations require the
`x-shortlist-csrf: 1` header.

**Programmatic access (API token).** For scripting, generate an owner token in Settings → Advanced →
API access (or `POST /api/system/api-token`) and send it as `Authorization: Bearer <token>`. It
grants the same owner-level access as the browser session and needs no CSRF header (a browser never
sends it automatically). The token is stored encrypted at rest (Fernet, like the Plex/AI-provider keys)
and stays revealable to the owner — the Settings card and `GET /api/system/api-token` show it
(owner-gated) so you can copy it any time; it never appears in `GET /api/settings`. Regenerating or
revoking (`DELETE /api/system/api-token`) invalidates the old token immediately.

```
curl -H "Authorization: Bearer <token>" https://<host>/api/runs
```

## Watched titles (and why one can still be recommended)

Shortlist excludes what someone has already watched. Each run reads every user's **complete watched
set directly from your Plex server, as that user** — no extra configuration, no database mount, and
it works for every account on the server.

The mechanism is the per-user server token Plex already mints for every share. When you share
libraries with someone, plex.tv issues a server-scoped `accessToken` for their account
(`GET /api/servers/{machine}/shared_servers`); reading `library/sections/{key}/all?unwatched=0` with
that token returns exactly the titles Plex considers watched **for them** — carrying their own
`viewCount` (movies) and `viewedLeafCount`/`leafCount` (shows). The owner isn't shared to their own
server, so their set is read with the admin token; a managed Home profile with no share of its own is
read by briefly switching to it and exchanging for a server token (the same path the privacy system
uses).

This closes the gap that used to let watched titles reappear. Plex has two notions of "watched": a
**playback session** (something was streamed) and a **mark-as-watched** (ticked off, or a whole
season marked, with no play). The old playback-history API returned only the former — and capped at
roughly the most recent 200 plays — so a heavy watcher's older titles and everyone's marks were
invisible, and already-seen films kept coming back. `viewCount > 0` (what `unwatched=0` filters on)
counts **both**, at any depth. On one real server that meant seeing all ~13k watched titles instead
of the ~1k the API reported.

A show counts as "finished" once the user has watched enough of it — `viewedLeafCount` against a
fraction of its episodes (80% by default), with a length-scaled floor so a long-running series a
person is genuinely deep into isn't treated as fresh, while three episodes of a 200-episode run isn't
treated as finished. `recommendations.watched_pct` then decides how much of a row (if any) may be
titles already finished.

**Why a watched title can still appear:** the read is per-run, so a title marked watched _after_ the
last run stays eligible until the next run re-reads. Between runs, **Tools → Sync history**
(`POST /api/report/sync`) re-reads every user's watched set on demand — it writes nothing to Plex,
only refreshes what Shortlist knows, so hit rates and the per-user "N titles watched" count stay
current without waiting for a scheduled run.

## How a pick is chosen (and why a row can be short)

Every candidate carries an **affinity** — how strongly the source that produced it vouched for it,
0..1:

- **TMDB** sets it from which endpoint suggested the title and how near the top of that list it sat
  (`/recommendations` is worth more than `/similar`, and both decay down their list), multiplied by
  a **genre-coherence** factor: the share of the candidate's own genres that the seed does not have.
  TMDB tags a medical drama simply "Drama" and so is nearly everything it suggests, so overlap alone
  discriminates nothing — but a suggestion also tagged "Sci-Fi & Fantasy" is measurably further away.
- **Sources with no ranking of their own** — `tmdb_discover`, `trakt`, `llm_web` —
  report the neutral `1.0`. That means "no ranking information", not "perfect match"; they are
  deliberate picks rather than the tail of a list, and `pre_rank`'s per-source round-robin is what
  keeps them competing fairly.

Ranking is `(1 + seed_frequency) × rating × (1 + seed_weight) × affinity`, so a well-rated but
distant title no longer beats an obviously similar one.

**A row is allowed to come up short.** Padding a partly-filled row only draws from candidates at or
above `MIN_FILLER_AFFINITY` (0.35) — four genuinely-similar titles beat ten where six are filler.
When that happens the run log says so at INFO, naming the closest rejected title, so a short row
reads as the filter working rather than as a failure.

Each delivered pick records its provenance (`sources`, `affinity`, returned by `GET /api/users` and
the run detail) and the UI shows it under the title — _"suggested by TMDB · loosely related"_. At
DEBUG the run log prints the same per row: every pick with its seed, source and affinity.

## How rows stay private

Each row is a Plex collection labelled `shortlist_<userslug>`. Every _other_ account's share filter
gets a `label!=shortlist_<userslug>` exclusion (merged into their existing `filterMovies` /
`filterTelevision`, never rebuilt), so only its owner ever sees it. The write ordering is what keeps
this leak-safe: a run delivers rows **unpromoted**, merges all the exclusions, and only **then**
promotes rows onto Home — a new row is never visible before the exclusion that hides it exists. Rows
Plex cannot hide (wrong media type for their library) are swept away first, before anything else.

Before Shortlist first edits an account's filters it snapshots them (`restriction_snapshots`), so
**Uninstall** restores every share exactly as it found it. The one hard requirement is a PMS
**≥ 1.43.2.10687** (older builds ignore the label exclusion).

> Earlier versions ran an automatic _Privacy Check_ that verified the hiding before each write and
> refused to write if it couldn't confirm it. That check + its write gate were removed at the
> maintainer's request; the hiding above still happens on every run, but it is no longer verified
> after the fact.

## Files under /config

`shortlist.db` (SQLite — settings, users, runs, restriction snapshots, and the
durable plex-account-id → slug map a row's label is built from) · `secret.key` (Fernet, 600) ·
`session.secret` · `logs/`.
