# Reference

## Environment variables (container)

| Variable                                                                   | Default   | Live or seed                                                                                    |
| -------------------------------------------------------------------------- | --------- | ----------------------------------------------------------------------------------------------- |
| `PORT`                                                                     | `5959`    | live                                                                                            |
| `TZ`                                                                       | `Etc/UTC` | live                                                                                            |
| `PUID` / `PGID`                                                            | `1000`    | live                                                                                            |
| `SHORTLIST_CONFIG`                                                         | `/config` | live                                                                                            |
| `PLEX_URL`, `PLEX_TOKEN`, `TAUTULLI_URL`, `TAUTULLI_APIKEY`, `TMDB_APIKEY` | —         | **seed once**: copied into settings on first boot, ignored afterwards                           |
| `LOG_LEVEL`                                                                | `DEBUG`   | **seed once**: initial value for the `log.level` setting; change it live in Settings → Advanced |

## Settings keys (DB-backed; Settings UI or `PUT /api/settings`)

| Key                                                   | Default                            | Notes                                                                                                                                                                                                                                                                                                                                                                                       |
| ----------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `plex.url` / `plex.token`                             | —                                  | token stored Fernet-encrypted, redacted in API                                                                                                                                                                                                                                                                                                                                              |
| `tautulli.url` / `tautulli.apikey`                    | —                                  | optional                                                                                                                                                                                                                                                                                                                                                                                    |
| `tmdb.apikey`                                         | —                                  | required for personal mode                                                                                                                                                                                                                                                                                                                                                                  |
| `curator.provider`                                    | `none`                             | `anthropic` \| `openai` \| `google` \| `ollama` \| `none`                                                                                                                                                                                                                                                                                                                                   |
| `curator.api_key` / `curator.model`                   | —                                  | BYO key; sensible default model per provider                                                                                                                                                                                                                                                                                                                                                |
| `curator.ollama_url`                                  | `http://localhost:11434`           | Ollama endpoint; only used when `curator.provider` = `ollama`                                                                                                                                                                                                                                                                                                                               |
| `curator.prompt_tone`                                 | `balanced`                         | `balanced`\|`warm`\|`concise`\|`cinephile`\|`playful`                                                                                                                                                                                                                                                                                                                                       |
| `curator.prompt_guidance`                             | —                                  | free-text notes injected into the curation prompt                                                                                                                                                                                                                                                                                                                                           |
| `curator.prompt_template`                             | —                                  | full custom system prompt; blank = built-in skeleton                                                                                                                                                                                                                                                                                                                                        |
| `row.name_template`                                   | `✨ {library_name} Picked for You` | `{library_name}` (the delivering library), `{top_seed}` and `{user}` placeholders                                                                                                                                                                                                                                                                                                           |
| `row.size`                                            | `15`                               | any whole number 5–40 (free number picker in the UI); size PER library — each library a row targets fills to this                                                                                                                                                                                                                                                                           |
| `rows.hub_anchor`                                     | `{}`                               | Per-library placement in Plex's Recommended shelf: `{"<sectionKey>": {"top": true}}` (very top, no anchor) **or** `{"<sectionKey>": {"anchor": "<collection title>", "before": false}}` (next to a collection). Empty = Plex's default order (rows land last, under a co-managing tool like Kometa). Re-applied at end of each run; only Shortlist's own hubs move, the anchor is read-only |
| `staleness_runs`                                      | `3`                                | prefer titles not picked in the last N runs                                                                                                                                                                                                                                                                                                                                                 |
| `recommendations.watched_pct`                         | `0.0`                              | max share of a row that may be already-finished titles (0 = all fresh, 1 = no filtering); per-row overridable                                                                                                                                                                                                                                                                               |
| `recommendations.freshness`                           | `0.0`                              | day-to-day variability (0 = stable/best quality, 1 = fresh/most variety); rotates a row's picks by run day; per-row overridable                                                                                                                                                                                                                                                             |
| `candidates.sources`                                  | `["tmdb_similar","tmdb_discover"]` | sources to pool: `tmdb_similar`, `tmdb_discover`, `llm_library`, `trakt`, `llm_web`. Each enabled source gets a fair share of the candidates the AI sees — a wide source can't crowd out a narrow one                                                                                                                                                                                       |
| `llm_web.search_provider`                             | `auto`                             | how the `llm_web` source searches: `native` (the curator's own web-search tool — Claude/GPT/Gemini only), `exa` (the Exa search API — every provider incl. Ollama), or `auto` (the default: the curator's own tool AND Exa **unioned** when both are set up — they surface mostly different titles — else whichever one)                                                                    |
| `trakt.client_id`                                     | —                                  | Trakt API key; required for the `trakt` source; encrypted                                                                                                                                                                                                                                                                                                                                   |
| `exa.apikey`                                          | —                                  | Exa web-search API key; powers the `llm_web` source for any provider (the only web-search path for a local Ollama curator); encrypted                                                                                                                                                                                                                                                       |
| `plextv.throttle_s`                                   | `0.0`                              | FLOOR (min seconds) between plex.tv writes. `0` = fire as fast as plex.tv accepts; the client backs off adaptively on a 429 (jumps to ≥1s, doubles, capped 30s, eases back on clean writes), so 0 is safe. Range 0–60                                                                                                                                                                       |
| `log.level`                                           | `DEBUG`                            | container log verbosity: `ERROR`\|`WARNING`\|`INFO`\|`DEBUG`\|`TRACE`. DEBUG (default) narrates a run in full — per-source candidate counts, AI calls with timing/tokens, cache hits, throttle waits; TRACE adds full AI prompts; INFO trims to stage narration. Applied live                                                                                                               |
| `run.concurrency`                                     | `4`                                | how many users a run processes at once (1–16). Only history/candidate/AI reads overlap; every Plex + plex.tv write stays serial. 1 = fully sequential                                                                                                                                                                                                                                       |
| `paused_all`                                          | `false`                            | Danger-Zone "stop all runs" switch; pauses without disabling anyone                                                                                                                                                                                                                                                                                                                         |
| `requests.enabled`                                    | `false`                            | ask Radarr/Sonarr for picks the library lacks                                                                                                                                                                                                                                                                                                                                               |
| `requests.radarr.url` / `.apikey`                     | —                                  | Radarr (movies); key stored Fernet-encrypted, redacted                                                                                                                                                                                                                                                                                                                                      |
| `requests.radarr.quality_profile_id` / `.root_folder` | `0` / —                            | picked from dropdowns in the UI (fetched from Radarr)                                                                                                                                                                                                                                                                                                                                       |
| `requests.sonarr.url` / `.apikey`                     | —                                  | Sonarr (shows); key stored Fernet-encrypted, redacted                                                                                                                                                                                                                                                                                                                                       |
| `requests.sonarr.quality_profile_id` / `.root_folder` | `0` / —                            | picked from dropdowns in the UI (fetched from Sonarr)                                                                                                                                                                                                                                                                                                                                       |
| `requests.rating_source`                              | `tmdb`                             | `tmdb` (no setup) \| `imdb` (needs an OMDb key)                                                                                                                                                                                                                                                                                                                                             |
| `requests.omdb.apikey`                                | —                                  | free OMDb key; required for the `imdb` source; encrypted                                                                                                                                                                                                                                                                                                                                    |
| `requests.min_rating`                                 | `7.0`                              | score floor (0–10) on the chosen source                                                                                                                                                                                                                                                                                                                                                     |
| `requests.min_votes`                                  | `100`                              | vote-count floor on the chosen source                                                                                                                                                                                                                                                                                                                                                       |
| `requests.min_demand`                                 | `1`                                | request only titles wanted by ≥ N distinct people                                                                                                                                                                                                                                                                                                                                           |
| `requests.min_year`                                   | `0`                                | `0` = no lower bound; else request only titles from ≥ this year (a show is judged by its first-air year)                                                                                                                                                                                                                                                                                    |
| `requests.max_year`                                   | `0`                                | `0` = no upper bound; else request only titles from ≤ this year. With `min_year`, forms a release-year window; a candidate with no known year is excluded whenever either bound is set                                                                                                                                                                                                      |
| `requests.max_per_run`                                | `5`                                | hard cap on titles **auto**-requested per run, both apps                                                                                                                                                                                                                                                                                                                                    |
| `requests.auto_send`                                  | `true`                             | `false` = fully manual; every qualifying title is queued                                                                                                                                                                                                                                                                                                                                    |
| `requests.auto_min_demand`                            | `3`                                | auto-send only titles wanted by ≥ N distinct people                                                                                                                                                                                                                                                                                                                                         |
| `requests.auto_min_rating`                            | `8.0`                              | ...and rated ≥ this on the chosen source; rest are queued                                                                                                                                                                                                                                                                                                                                   |
| `requests.tag`                                        | `shortlist`                        | global tag on every requested title (created in the app; `""` = no tag)                                                                                                                                                                                                                                                                                                                     |

## API

The interactive API docs are off by default (they'd disclose the whole surface unauthenticated);
set `SHORTLIST_ENABLE_DOCS=1` to expose `/api/docs` and `/api/openapi.json` for local development
(also required if you regenerate the frontend API types with `pnpm -C web gen:api` against a live
server). Highlights:

```
POST /api/auth/pin · GET /api/auth/pin/{id} · GET /api/auth/session · POST /api/auth/logout
POST /api/setup/probe · POST /api/setup/link · GET/PUT /api/setup/state
GET  /api/users · PATCH /api/users/{id} {enabled?, request_tag?, prefs?} · POST /api/users/sync
POST /api/users/set-enabled {enabled} (bulk enable/disable every user at once)
GET  /api/users/{id}/rows · PUT /api/users/{id}/rows/{collection_id} {muted?, row_size?, prompt_*?}
GET  /api/users/{id}/runs · GET /api/users/{id}/history
GET/POST /api/collections · PATCH/DELETE /api/collections/{id} (incl. `request_tag`, `candidate_sources`, `library_keys`, `hub_anchor` — per-row shelf-placement override)
POST /api/collections/{id}/cleanup {dry_run?} (remove this row's Plex collections for everyone; dry-run previews)
GET  /api/system/libraries -> [{key, title, type}] (the server's Plex libraries, for the row editor)
GET  /api/system/libraries/{key}/collections -> [{title}] (a library's managed collections — anchor choices for row placement, excludes Shortlist's own)
GET  /api/system/owned-collections -> {collections:[{library,title,label,rating_key,kind,slug,orphan}], total, orphans} (cleanup audit: every shortlist-labelled collection ON PLEX, drift-flagged, DB-independent)
GET  /api/runs · GET /api/runs/{id} · GET /api/runs/{id}/log (activity feed) · POST /api/runs {user_ids?, dry_run?}
GET  /api/requests · POST /api/requests/send {ids, dry_run?} · POST /api/requests/reject {ids}
GET  /api/events (SSE) · GET /api/events/log (audit feed)
GET/PUT /api/settings · POST /api/settings/test/{plex|tautulli|tmdb|llm|radarr|sonarr|omdb|trakt|exa}
GET  /api/settings/arr/{radarr|sonarr}/options -> {quality_profiles, root_folders}
GET  /api/settings/curator/models -> {provider, models[]} (available models for the saved AI provider; [] = free-text)
POST /api/settings/prompt-preview {tone?, guidance?, template?, shared?} -> {system, user}
GET  /api/system/health · GET /api/system/version · POST /api/system/uninstall {confirm: "UNINSTALL"}
GET  /api/setup/servers (Plex server picker during onboarding) · GET /api/setup/state
```

The curation prompt is tunable: a `tone` preset + free-text `guidance` + an optional full custom
`template` (the fixed output contract is always re-appended, so edits can't break a run). Set the
global recipe via the `curator.prompt_*` settings; override any field per user via
`PATCH /api/users/{id}` prefs (`prompt_tone` / `prompt_guidance` / `prompt_template`, empty =
inherit). `prompt-preview` assembles the prompt against sample data so the UI can show the effect.

Every row can carry its own recipe (`collections.prompt`) — **except the seeded `picked` row**,
which is curated with the global one so it stays in sync with Settings (as its name and size do).
The API normalizes `prompt` to `{}` for that slug on create and PATCH, so the stored state can never
disagree with what a run will apply.

**Blank means inherit, at every layer.** A row's recipe is the global one with the row's set fields
laid over it; one person's row override is laid over that. A blank field inherits; it never resets the
layer below to a default. Guidance is additive (the house note plus the specific one); tone and
template replace.

**`PUT /api/settings` validates values, not just keys.** `plextv.throttle_s` must be 0–60 (0 = fire
as fast as plex.tv accepts, with adaptive 429 backoff), `row.size` must be 5–40, `paused_all` must be a real boolean,
and `candidates.sources` / `curator.provider` / tones are checked against their known values.

Candidate sources are set globally (`candidates.sources`) and can be overridden per row
(`collections.candidate_sources`, `[]` = inherit the global set; valid values: `tmdb_similar`,
`tmdb_discover`, `llm_library`, `trakt`, `llm_web`). `llm_web` proposes titles to watch next from a
live web search, each resolved via TMDB search then library-verified. It works on **every** curator
provider via `llm_web.search_provider`: `native` uses the provider's own web-search tool (Claude,
GPT, or Gemini), `exa` uses the Exa search API (`exa.apikey`) — the only path for a local Ollama
model — and `auto` (the default) UNIONS the curator's own tool and Exa when both are set up (they
surface mostly different titles, so the pool is widest), else whichever one is available. When a
source's dependency is missing, the Settings UI keeps the toggle usable but shows an inline fix
(enter the key right there, or a curator prompt) — it never reads as on while silently doing nothing.

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
library (e.g. only "4K Movies") on a server with several libraries of one type. Curation runs **per
library**: each targeted library seeds from its own watched history and fills to `row.size` on its
own, so a movies-and-TV watcher gets a full movie row AND a full TV row.

Placement is per row (`collections.placement`: `both` \| `home` \| `library`, default `both`), which
sets which Plex surfaces the row appears on once promoted (Home, the library's Recommended tab, or
both). WHERE in that shelf it sits is the **Position** control (`collections.hub_anchor`, per library:
`{"top": true}` or `{"anchor": "<collection>", "before": bool}`) — see [Row placement](#) above; it
replaces the old `pin_top` toggle (still honoured for rows not yet re-saved). This order is Plex's
Managed Recommendations, which are **server-wide** — Plex exposes no per-viewing-user hub order.

Request tags are three-layered: the global `requests.tag` setting, a per-user `request_tag`
(`PATCH /api/users/{id}`), and a per-row `request_tag` (`collections`, per-person rows only —
shared rows never request). A requested title is tagged with the union of the global tag, every
wanting user's tag, and the tag of every per-person row that user is in the audience of; the queued
tags round-trip through `GET /api/requests` (`tags[]`) and are applied on `send`.

All endpoints except `/api/system/health` require the owner session; mutations require the
`x-shortlist-csrf: 1` header.

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
