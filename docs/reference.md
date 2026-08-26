---
title: "Reference: settings, API and env vars"
description: Every Shortlist configuration key, REST API endpoint, container environment variable and default value.
heading: Reference
---

## Environment variables (container)

| Variable                                                                   | Default   | Live or seed                                                                                                                                                                                                                                                    |
| -------------------------------------------------------------------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PORT`                                                                     | `5959`    | live                                                                                                                                                                                                                                                            |
| `TZ`                                                                       | `Etc/UTC` | live                                                                                                                                                                                                                                                            |
| `PUID` / `PGID`                                                            | `1000`    | live                                                                                                                                                                                                                                                            |
| `SHORTLIST_CONFIG`                                                         | `/config` | live                                                                                                                                                                                                                                                            |
| `PLEX_URL`, `PLEX_TOKEN`, `TAUTULLI_URL`, `TAUTULLI_APIKEY`, `TMDB_APIKEY` | —         | **seed once**: copied into settings on first boot, ignored afterwards                                                                                                                                                                                           |
| `LOG_LEVEL`                                                                | `DEBUG`   | **seed once**: initial value for the `log.level` setting; change it live in Settings → Advanced                                                                                                                                                                 |
| `SHORTLIST_DRY_RUN`                                                        | unset     | live: when set (`1`/`true`), EVERY run is forced to dry-run. The app builds its clients and logs the would-be changes but writes NOTHING to Plex/plex.tv. Safe mode for a demo/test instance pointed at a real server (even a manual "Run now" can't modify it) |
| `SHORTLIST_ENABLE_DOCS`                                                    | unset     | live: when set (`1`), exposes the API docs at `/api/docs` and `/api/openapi.json` (off by default)                                                                                                                                                              |

## Settings keys (DB-backed; Settings UI or `PUT /api/settings`)

| Key                                                   | Default                            | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ----------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `plex.url` / `plex.token`                             | —                                  | token stored Fernet-encrypted, redacted in API. Saving a URL/token that answers with a DIFFERENT machine id is refused (409): every record Shortlist holds — the delivery ledger, share-filter snapshots, the user list — belongs to the linked server, so switching is a re-link (uninstall, then set up again), not a settings edit                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `tautulli.url` / `tautulli.apikey`                    | —                                  | optional                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `tmdb.apikey`                                         | —                                  | required for personal mode. Stored Fernet-encrypted and redacted in the API like every other key; an install that predates that has its plaintext value encrypted on the next boot.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `curator.provider`                                    | `none`                             | `anthropic` \| `openai` \| `openai_compatible` (any local/self-hosted OpenAI-API server) \| `google` \| `none`. `ollama` is the pre-merge name, still accepted                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `curator.api_key` / `curator.model`                   | —                                  | BYO key; sensible default model per provider. Optional for `openai_compatible` — a server on your own network needs none, a hosted gateway on the same API (ollama.com, OpenRouter) does; both the wizard and Settings offer the field                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `curator.openai_base_url`                             | —                                  | your local/self-hosted server's URL (Ollama, llama.cpp, LM Studio, vLLM, LocalAI, OpenRouter). A bare host gains `/v1` automatically. `curator.ollama_url` is the pre-merge key, still read as a fallback                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `row.name_template`                                   | `✨ {library_name} Picked for You` | `{library_name}` (the delivering library), `{top_seed}` and `{user}` placeholders. This IS the default row's title, so editing it here renames every user's collection on Plex immediately. The same reconcile the Rows page runs. Refused with a 422 if another row is already titled from the same template — two rows that render one title become one collection on Plex, since per-person rows share a label and are told apart by title alone. The same check guards creating or renaming any row.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `row.size`                                            | `15`                               | any whole number 5–40 (free number picker in the UI); size PER library — each library a row targets fills to this                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `rows.hub_anchor`                                     | `{}`                               | Per-library placement in Plex's Recommended shelf: `{"<sectionKey>": {"top": true}}` (very top, no anchor) **or** `{"<sectionKey>": {"anchor": "<collection title>", "before": false}}` (next to a collection). A per-ROW override may also use `{"row": "<row slug>", "before": false}` to sit next to another Shortlist row; the global default here cannot, since every row following it would include the anchor row itself. Empty = Plex's default order (rows land last, under a co-managing tool like Kometa). Re-applied at the end of every run, by the nightly privacy sync (05:15 by default), by **Check and fix rows on Plex**, and whenever a change to who-sees-what triggers a privacy sync — so a row promoted between runs is repositioned on the next of those rather than waiting for the next nightly build. Only rows actually out of place are moved, and the shelf is re-read afterwards to confirm it took — if another tool (Kometa, agregarr) is reordering the same shelf, that is recorded as unverified rather than reported as success; only Shortlist's own hubs move, the anchor is read-only |
| `rows.manage_shelf_order`                             | `true`                             | master switch for Shortlist touching the Recommended-shelf ORDER at all. `true` (default) applies your `rows.hub_anchor` placement at the end of each run; `false` = never reorder the shelf, leaving the order entirely to a co-managing tool like agregarr/Kometa                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `recommendations.watched_pct`                         | `0.0`                              | max share of a row that may be already-finished titles (0 = all fresh, 1 = no filtering); per-row overridable. The "already-finished" set is each user's COMPLETE watched set, read from Plex AS them each run. So it includes titles they only _marked_ watched, not just played (see [Watched titles](#watched-titles-and-why-one-can-still-be-recommended)).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `recommendations.refresh_days`                        | `8`                                | REFRESH CADENCE IN DAYS, not a nightly shuffle: 0 = frozen once built, 1 = rebuild every night, N = every N days. On a rebuild night the strongest ~⅔ stay and the weakest third is swapped for new picks; other nights the row is reused unchanged (no rebuild, no Plex write). Per-row overridable. Except on a row whose name uses `{top_seed}` or that cycles its seed, which always rebuilds nightly (a row naming a recent watch cannot be allowed to lag behind it) and where the editor hides the control accordingly. Was `recommendations.freshness`, a 0–1 fraction a curve stretched onto 1–14 days; migration 0065 converts every stored value to the day count it already meant, so no row changes pace. Anything up to 365 is now sayable — the fraction could not express a cadence slower than a fortnight.                                                                                                                                                                                                                                                                                                   |
| `recommendations.recency`                             | `0.5`                              | How much a title's RELEASE DATE counts when ranking it: 0 = ignore age entirely, which is how ranking worked before this setting existed; 1 = every ~8 years of age halves a title's weight (0.5 = every ~16). A WEIGHT, never a filter — an old title is only ever asked to be a better match, never excluded, so nothing is dropped for being old. Distinct from `recommendations.refresh_days` above, which is how OFTEN a row rebuilds rather than which titles win. Per-row overridable. Applies to EVERY install, existing servers included: upgrading to the release that introduced it shifts each row towards newer titles on its next rebuild night. Set it to `0` for age-blind ranking (how it worked before).                                                                                                                                                                                                                                                                                                                                                                                                     |
| `recommendations.recent_count`                        | `10`                               | how many of a person's most recent watches the `llm_web` source searches per row (one cached search each, "what to watch if you liked X"); results cached 14 days and shared across users so a popular title is searched once server-wide; overridable per row, and per person on a row (User → Rows), each falling back to the next: user → row → this global (1–25)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `recommendations.max_seeds`                           | `30`                               | how many of a person's watched titles SEED a row. The titles every discovery source searches from, not just `llm_web`. Fewer = a tighter row about a couple of things; more = broader coverage of their taste. Overridable per row (1–100), which is where a deliberately narrow value belongs: a row named `{top_seed}` wants 1 so its title is true. This server-wide default is floored at **5** because seeds are shared across the media types a row covers, so a global 1 or 2 would leave every movies-and-TV row with one half unseeded (5–100)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `recommendations.min_history`                         | `10`                               | how many titles someone must have watched before Shortlist recommends from THEIR taste. Below it they are a **cold start** and get whatever `recommendations.cold_start` says. Floored at 1: at 0 nobody is ever cold, which would silently disable the whole path (1–100)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `recommendations.cold_start`                          | `popular`                          | what a cold-start person gets. `popular` = a row of the server's highest-rated titles (the long-standing behaviour). `skip` = no row is built for them, and any row they already have is REMOVED, so skipping means gone rather than left to go stale. Overridable per row — a `{top_seed}` row is the one worth skipping, having no seed to name itself after. Note a `{top_seed}` row is not built for a cold-start person either way unless it has a `fallback_name`: `popular` decides the row's CONTENTS, and a row still needs a title                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `recommendations.rating_source`                       | `tmdb`                             | which service's score a row ordered by **Highest rated** sorts on. `tmdb` is already carried on every candidate and costs no lookups; `imdb`/`trakt`/`tomatoes`/`metacritic` come from MDBList (one cached lookup per title, shared by every row and user) and need `requests.mdblist.apikey`. Without a key, or once the daily quota is spent, the row falls back to TMDB for its whole ordering rather than mixing two scales                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `privacy.hide_shared_from_disabled`                   | `true`                             | when on, disabling a user hides EVERY shared row from them too — even public "Popular on this server" rows. So a disabled user sees nothing from Shortlist. Off = disabled users still see public shared rows like any account with library access. Changing this setting, or re-enabling someone, rewrites the share filters straight away rather than waiting for a run.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `candidates.sources`                                  | `["tmdb_similar","tmdb_discover"]` | sources to pool: `tmdb_similar`, `tmdb_discover`, `trakt`, `llm_web`. Each enabled source gets a fair share of the pooled candidates — a wide source can't crowd out a narrow one                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `llm_web.search_provider`                             | `native`                           | which backend the `llm_web` source searches with — exactly one: `native` (the provider's own web-search tool, Claude/GPT/Gemini only), `exa` (the hosted Exa search API), or `searxng` (your own self-hosted SearXNG — no account, key or per-search bill; it still forwards each query on to real search engines). Either external works for every provider, including a local model that cannot search on its own. Naming a backend never falls back to another. A fourth value, `auto` (native unioned with an external), was removed in 1.3 — migration 0063 pins every existing install to the backend it was actually using                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `trakt.client_id`                                     | —                                  | Trakt API key; required for the `trakt` source; encrypted. Trakt now requires a paid **VIP** subscription to create an API key, so this source is unavailable on a free Trakt account — everything else in Shortlist works without it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `exa.apikey`                                          | —                                  | Exa web-search API key; powers the `llm_web` source for any provider, including a local model that cannot search on its own (`searxng.url` is the self-hosted alternative — you need only one); encrypted                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `searxng.url`                                         | —                                  | address of your own [SearXNG](https://docs.searxng.org) instance, e.g. `http://your-host:8080`. Powers the `llm_web` source for any provider. SearXNG is a metasearch proxy, not an index: it forwards each query to real engines (Google, Brave, DuckDuckGo, …) and merges what they return — so queries do leave your network, just with no account, key or bill attached. Those engines rate-limit and CAPTCHA self-hosted instances, so expect some to fail on any given search; the **Test** button names the ones that did. **Its JSON API must be enabled** — add `json` to `search.formats` in SearXNG's `settings.yml` and restart, or it answers Shortlist with a 403. A reverse-proxy subpath (`https://example.com/searxng`) is kept as given                                                                                                                                                                                                                                                                                                                                                                      |
| `searxng.username`                                    | —                                  | username, only if you keep SearXNG behind a reverse-proxy login (SearXNG itself has no auth)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `searxng.password`                                    | —                                  | password for that login; encrypted. Put the login HERE, not in `searxng.url` — a URL carrying `user:pass@` is rejected, because that value is stored in the clear, returned by the API and recorded verbatim in the immutable `settings.change` audit event                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |     |
| `plex.timeout_s`                                      | `45`                               | seconds to wait on a single Plex Media Server call before giving up and retrying. Reads are near-instant, but rebuilding a big library's collection (a TV row on a large server) legitimately takes 15-20s+, so too low a value times those out and forces a wasteful retry. Range 5-300. Advanced                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `plextv.throttle_s`                                   | `0.0`                              | FLOOR (min seconds) between plex.tv writes. `0` = fire as fast as plex.tv accepts; the client backs off adaptively on a 429 (jumps to ≥1s, doubles, capped 30s, eases back on clean writes), so 0 is safe. Range 0–60                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `log.level`                                           | `DEBUG`                            | container log verbosity: `ERROR`\|`WARNING`\|`INFO`\|`DEBUG`\|`TRACE`. DEBUG (default) narrates a run in full. Per-source candidate counts, AI calls with timing/tokens, cache hits, throttle waits; TRACE adds full AI prompts; INFO trims to stage narration. Applied live. TRACE reaches the container log only — the in-app Logs view reads a file sink opened at DEBUG, so it has no TRACE filter                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `run.concurrency`                                     | `4`                                | how many users a run processes at once (1–16). Only history/candidate/AI reads overlap; every Plex + plex.tv write stays serial. 1 = fully sequential                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `runs.retention`                                      | `3`                                | how many months of run history to keep; after each run, anything older is auto-pruned (runs + per-user traces + activity logs deleted; **picks and deliveries are always kept**. The first is the dashboard's history, the second is what tells a cleanup which Plex collection is which). `0` = keep everything forever                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `events.retention`                                    | `0` (forever)                      | how many months of the audit trail (`events`) to keep (0–24). Kept forever by default: "what changed on whose share at 03:31" is the record you want long after the run detail around it is gone. `0` = never prune. Set from Settings → Advanced → "Change log kept"                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `jobs.max_parallel_readonly`                          | `3`                                | how many READ-ONLY background jobs may run at once (1–8). Jobs that write to Plex/plex.tv are always exclusive and never overlap a run. Share-filter writes are read-modify-write merges, so two at once would lose one of them. Read-only: `sync.history`, `backup.take`, `maintenance.prune` and `watch.reconcile`. `sync.users` counts as a writer because it renames collections. Dial to 1 if your PMS objects to the concurrency                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `sync.watch_cron`                                     | `""` (daily 04:17)                 | cron expression for the watch-history sync schedule. Blank = built-in default. Set from the job's frequency picker on the Jobs page                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `sync.watch_incremental`                              | `true`                             | read only what changed since the last sync instead of every watched title, per user, per library, every night. Done by ORDERING (`sort=lastViewedAt:desc`, then stop at the first title older than the cursor), because a `lastViewedAt>=` query filter is silently ignored by PMS 1.43.3. It returns the full set with a 200 (measured; see `tests/fixtures/pms_watched_incremental.xml.txt`). When the read can prove it reached everything back to the cursor — it either saw an older title or walked the library's reported total — a title missing from it that was watched inside that window has been un-watched, and is dropped. When it cannot prove that (a PMS that reports no total and caps the page, or one that ignores the sort), it tops up and deletes nothing, because a truncated read and an un-watch look identical from the outside. Further back than the cursor it cannot tell either way, so a complete read still happens every `sync.watch_full_days` regardless. `false` = always read everything                                                                                                |
| `sync.watch_full_days`                                | `7`                                | how often the COMPLETE watch-history re-read happens, in days (1–90). It is the only thing that can notice a title un-watched or removed longer ago than the nightly read reaches back, so it is not optional. Only its frequency is. It also bounds how quickly a **Plex rating** on an OLDER title takes effect. Rating something does not move its `lastViewedAt`, and the incremental read walks by that stamp — but it still returns every title watched since the cursor, ratings included, so rating what you just watched is picked up on the very next sync. It is only a rating on something watched further back than the cursor reaches that waits for the full read, up to 7 days at the default (measured on a live server: two accounts' ratings arrived incrementally while a third's older one needed the full pass). Lower this if you want those to land sooner, at the cost of a full re-read that much more often. If a library cannot be read incrementally at all, that library falls back to a complete read on its own rather than serving a stale set                                                |
| `sync.users_cron`                                     | `""` (daily 04:47)                 | cron expression for the user-list sync schedule. Blank = built-in default                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `privacy.sync_cron`                                   | `""` (daily 05:15)                 | cron for the nightly privacy sync. A re-merge of every account's share filter. It builds, delivers and promotes nothing, so it can only ever make the server _more_ private; it is the cheapest safety net against drift now that nothing verifies hiding after the fact                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `sync.check_cron`                                     | `""` (daily 05:45)                 | cron for the drift check — after the rows build (03:30) and the privacy pass (05:15), so it checks the state those left behind. The ONE schedule that can be switched off entirely: it writes corrections to Plex, so choosing **Off** in its frequency picker (Jobs → Check and fix rows on Plex) stores an empty value the scheduler reads as an explicit "off" rather than "inherit the default" (every other blank cron means "use the built-in default"). Because of that, its picker is driven by the EFFECTIVE cron from `GET /api/schedule`, not by the raw setting. The two are indistinguishable in `GET /api/settings`, which folds the blank default in. The way back is the **Built-in (05:45)** chip in the same picker, which saves `null` — that deletes the stored value, so the job inherits the built-in cron again rather than pinning a copy of it                                                                                                                                                                                                                                                        |
| `maintenance.prune_cron`                              | `""` (daily 06:15)                 | cron for the retention prune. It applies `runs.retention` and `events.retention` and drops expired cache rows. Last of the night, after every other schedule has finished writing, so it trims a settled database. The prune is also queued after every run; this schedule is the FLOOR under that, for a server whose rows have no cron (or one paused from the Danger Zone) and so has no runs to queue it. Local database housekeeping. Nothing on Plex changes. Blank = built-in default                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `recommendations.blocked_shared_seeds`                | `[]`                               | TMDB ids that must never seed a SHARED row. Separate from each person's own blocked seeds on purpose: a shared row is public, so letting one person's block reshape what everyone sees would make an individual preference into a server-wide edit nobody else can see or undo                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `recommendations.use_plex_ratings`                    | `true`                             | when on, a title someone rated low in Plex stops being used to find similar things FOR THEM. Their rating arrives on the watched read Shortlist already makes, scoped to their own share token, so it costs no extra calls and one person's opinion can never reach another's row. A title nobody rated is unaffected — on a real 50-account server that was 99.7% of watches. Never applied to a SHARED row, for the same reason as `blocked_shared_seeds` above. Ratings that look tool-written (Kometa and friends sync IMDb scores into the same field) are ignored: Plex's own controls write whole numbers only, so a fractional value was not typed by a person, and an account whose ratings are mostly fractional is disbelieved wholesale                                                                                                                                                                                                                                                                                                                                                                            |
| `recommendations.dislike_threshold`                   | `2.0`                              | the 0–10 Plex rating at or below which that happens, inclusive. 2 = one star, which is also where a thumbs-down lands. Capped at 6 (three stars): above that “disliked” stops being a fair reading, and 10 would suppress every rated title at once (0–6)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `paused_all`                                          | `false`                            | Danger-Zone "stop all runs" switch; pauses without disabling anyone                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `requests.enabled`                                    | `false`                            | ask Radarr/Sonarr for picks the library lacks                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `requests.radarr.url` / `.apikey`                     | —                                  | Radarr (movies); key stored Fernet-encrypted, redacted                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `requests.radarr.quality_profile_id` / `.root_folder` | `0` / —                            | picked from dropdowns in the UI (fetched from Radarr)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `requests.sonarr.url` / `.apikey`                     | —                                  | Sonarr (shows); key stored Fernet-encrypted, redacted                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `requests.sonarr.quality_profile_id` / `.root_folder` | `0` / —                            | picked from dropdowns in the UI (fetched from Sonarr)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `requests.sonarr.monitor`                             | `all`                              | how much of a show Sonarr monitors — and so downloads — when Shortlist adds it. Sonarr's own Add Series "Monitor" choice, passed through: `all` \| `firstSeason` \| `lastSeason` \| `pilot` \| `none`. `all` takes the whole back catalogue of a long-running show the night it is added; `firstSeason` is a taster; `none` files it unmonitored and downloads nothing. Every mode except `all` is sent with `monitorNewItems: none`, so a restricted show does not pick up new seasons as they air. The rest of Sonarr's list is deliberately not offered — `future`, `existing` and `recent` each monitor NOTHING on a show the server doesn't have yet, so on a new add they are an obscure spelling of `none`. Shows Sonarr already tracks are skipped whole, so this only ever applies to a NEW add                                                                                                                                                                                                                                                                                                                       |
| `requests.rating_source`                              | `tmdb`                             | `tmdb` (no setup) \| `imdb` \| `trakt` \| `tomatoes` (Rotten Tomatoes) \| `metacritic` — all non-TMDB sources come from MDBList, normalised to a 0–10 scale                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `requests.mdblist.apikey`                             | —                                  | free MDBList key; required for any non-TMDB rating source; encrypted. One lookup returns every source and is cached 7 days; on a 429 (daily cap) the gate falls back to TMDB and the owner is notified                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `requests.min_rating`                                 | `7.0`                              | score floor (0–10) on the chosen source                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `requests.language_mode`                              | `any`                              | how a title's ORIGINAL language is treated: `any` (one bar for everything — what Shortlist has always done) \| `prefer` (other languages must clear `requests.min_rating_other` to auto-send; below it they wait in the inbox) \| `only` (never request another language at all — these are dropped, not queued)                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `requests.preferred_languages`                        | `["en"]`                           | ISO 639-1 codes counted as preferred (TMDB's `original_language`). Never read while the mode is `any`. An EMPTY list is meaningful: in `only` mode it requests nothing. A title whose language is unknown — only a non-TMDB source such as Trakt produces one — counts as preferred                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `requests.min_rating_other`                           | _unset_                            | auto-send floor for a title NOT in a preferred language, when the mode is `prefer`. Unset (`null`) means **follow `requests.min_rating` + 1.5**, so it tracks your own floor rather than a number of ours — a 6.0 server starts at 7.5, an 8.0 server at 9.5. Set a number to pin it; `0` is a real bar (nothing fails it), not "unset"                                                                                                                                                                                                                                                                                                                                                                                     |
| `requests.min_votes`                                  | `100`                              | vote-count floor on the chosen source                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `requests.min_demand`                                 | `1`                                | request only titles wanted by ≥ N distinct people **within one row** — counted per row, so a title one person wants in three rows is 1 in each, not 3                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `requests.min_year`                                   | `0`                                | `0` = no lower bound; else request only titles from ≥ this year (a show is judged by its first-air year)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `requests.max_year`                                   | `0`                                | `0` = no upper bound; else request only titles from ≤ this year. With `min_year`, forms a release-year window; a candidate with no known year is excluded whenever either bound is set                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `requests.max_per_run`                                | `5`                                | hard cap on titles **auto**-requested per run, both apps. When `requests.rating_source` is not `tmdb`, this also sets how many MDBList rating lookups a run may spend (4x, floor 20) — so raising it lets a run rate more titles before it gives up, not just send more. The budget counts lookups that cost an API call; a rating already in the cache is read for free and does not use any of it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `requests.auto_send`                                  | `true`                             | `false` = fully manual; every qualifying title is queued                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `requests.auto_min_demand`                            | `3`                                | auto-send only titles wanted by ≥ N distinct people **within one row** (see `requests.min_demand`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `requests.auto_min_rating`                            | `8.0`                              | ...and rated ≥ this on the chosen source; rest are queued                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `requests.tag`                                        | `shortlist`                        | global tag on every requested title (created in the app; `""` = no tag)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `requests.auto_user_tag`                              | `false`                            | also tag each requested title with the WANTING PERSON'S slug, so the Arr shows who it was added for. Off by default; a per-user `request_tag` replaces the slug rather than stacking with it, and a row may override this either way (`req_auto_user_tag`). The tag records who TRIGGERED the add: a title the Arr already tracks is skipped whole, tags included.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |

### Per-row request overrides

Any per-person row may override these in the row editor; the column is the `collections` column name,
and NULL always means "inherit the global `requests.*` setting".

| Row column                      | Overrides                                                                                                                                                               |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `req_min_rating`                | `requests.min_rating`                                                                                                                                                   |
| `req_min_votes`                 | `requests.min_votes`                                                                                                                                                    |
| `req_min_demand`                | `requests.min_demand` — counted WITHIN the row                                                                                                                          |
| `req_min_year` / `req_max_year` | `requests.min_year` / `requests.max_year`                                                                                                                               |
| `req_auto_send`                 | `requests.auto_send`                                                                                                                                                    |
| `req_auto_min_demand`           | `requests.auto_min_demand`                                                                                                                                              |
| `req_auto_min_rating`           | `requests.auto_min_rating`                                                                                                                                              |
| `req_max_per_row`               | this row's share of `requests.max_per_run`; may only restrict it. Blank = inherit the global; `0` = this row never auto-sends, and its picks queue for approval instead |
| `req_radarr_root_folder`        | `requests.radarr.root_folder`                                                                                                                                           |
| `req_radarr_quality_profile_id` | `requests.radarr.quality_profile_id`                                                                                                                                    |
| `req_sonarr_root_folder`        | `requests.sonarr.root_folder`                                                                                                                                           |
| `req_sonarr_quality_profile_id` | `requests.sonarr.quality_profile_id`                                                                                                                                    |
| `req_sonarr_monitor`            | `requests.sonarr.monitor` — a taster row can take season 1 only while every other row keeps the whole show                                                              |
| `req_language_mode`             | `requests.language_mode` — a kids row can be English-only while an anime row stays on `any`                                                                             |
| `req_preferred_languages`       | `requests.preferred_languages` — JSON, so `[]` (cleared) stays distinct from NULL (inherit)                                                                             |
| `req_min_rating_other`          | `requests.min_rating_other`. NULL inherits the global, which may itself be "follow the floor" — in which case this row derives from ITS OWN `req_min_rating`            |
| `req_auto_user_tag`             | `requests.auto_user_tag`                                                                                                                                                |

`requests.enabled`, `requests.rating_source`, `requests.mdblist.apikey`, `requests.max_per_run` and
the Arr URLs and API keys are server-wide and cannot be overridden per row — the first four are the
run's ceilings and its one rating account, and the last two mean a row files into a different folder
on the SAME Radarr, not a second one.

Shared rows carry none of these: built from already-watched titles, they surface nothing missing.

## API

The interactive API docs are off by default (they'd disclose the whole surface unauthenticated);
set `SHORTLIST_ENABLE_DOCS=1` to expose `/api/docs` and `/api/openapi.json` for local development
(also required if you regenerate the frontend API types with `pnpm -C web gen:api` against a live
server). Highlights:

```

```

### Sign-in and setup

```
POST /api/auth/pin · GET /api/auth/pin/{id} · GET /api/auth/session · POST /api/auth/logout
     Sign-in is owner-only in BOTH states. Once claimed, the Plex account must match the linked server's owner (403 otherwise). Before it is claimed there is no owner to compare against, so plex.tv is asked directly: an account that owns no Plex server is refused (403) rather than handed a session. Someone who merely has a share on your server can never sign in. If plex.tv can't be reached — or answers with something that isn't a resources list. The sign-in fails closed with 503, never open. **Known gap:** on an unclaimed instance the test is "owns *a* Plex server", not "owns *this* one", so someone running their own PMS can still claim an instance that has credentials seeded from the environment but no server linked yet. Link your server as step 1 and the window closes
POST /api/setup/probe · POST /api/setup/link · GET/PUT /api/setup/state
     `/link` verifies with plex.tv that `machine_id` is a server the caller's account OWNS (403 otherwise). The body's `owner_account_id` is only the caller vouching for themselves — `/servers` lists shared servers alongside owned ones, so without the plex.tv check a friend with a share could link your PMS and become this instance's owner. The wizard shows shared servers but won't let you select one
```

### Users

```
GET  /api/users · PATCH /api/users/{id} {enabled?, manage_sharing?, request_tag?, prefs?} · DELETE /api/users/{id} (only for someone plex.tv no longer lists: drops their picks and run history and hides them from the list; keeps the users row and their pre-Shortlist share-filter snapshot, which uninstall restores from — 409 for anyone still on the share) · POST /api/users/sync (shared + Home users from plex.tv, plus the server owner, whom that list never returns)
POST /api/users/set-enabled {enabled} (bulk enable/disable every user at once)
```

`manage_sharing` (default true) is a SEPARATE axis from `enabled`, not another value of it. `enabled`
decides whether someone gets a row; `manage_sharing` decides whether Shortlist may edit that
account's Plex share filters. Set it false and Shortlist adds no `label!=shortlist_*` exclusions for
them and removes the ones it already added — so that account can see other people's rows unless its
own Plex restrictions stop it. That is the point: an account with its own "allow only" label list is
already kept away from Shortlist's rows by that list, and our exclusions were fighting it. Every
OTHER account still excludes this person's own row label, so leaving one account alone never exposes
their row to the rest of the server. Changing it either way rewrites the filters straight away rather
than waiting for a run. Turning someone OFF (`enabled=false`) still writes their filters — it means
"no row for them", not "let them see everyone else's" — unless you have also left their sharing
alone, in which case that wins and nothing is written for them at all.

A **restricted shared row**'s exclusion is the exception that is NOT removed. A shared row limited to
an audience is hidden from everyone outside it by that exclusion alone, so dropping it would hand the
account a row you explicitly restricted; "leave their sharing alone" clears the per-person exclusions
it accumulated, not that. The consequence to know: while an account is left alone, later changes to a
shared row's audience never reach it, so its exclusion can go stale in the direction of seeing less.
Switching management back on resyncs it.

```
GET  /api/users/{id}/rows · PUT /api/users/{id}/rows/{collection_id} {muted?, row_size?, recent_count?} (per-person, per-row: `recent_count` (1–25) overrides how many recent watches the `llm_web` source searches for this person on this row; null on any field clears it back to the row's own setting)
GET  /api/users/{id}/runs (this person's outcome per run — `status`, `reason` for a non-failing skip, `duration_ms`, their diff and picks) · GET /api/users/{id}/runs/summary -> {included, total} (a run is server-wide, so "6 runs" on a person's page only reads honestly next to "of 148")
GET  /api/users/{id}/outcomes -> [{tmdb_id, media_type, title, row, outcome, percent, watched_at, finished_at}] (what became of each pick delivered to this person — `outcome` is `finished|dropped|bounced|watching`, resolved by the same `resolve_outcomes` the dashboard uses, so the two can never disagree)
GET  /api/users/search/titles?q=&media_type=movie|show -> [{tmdb_id, title, media_type, year}] (TMDB's best guess, for the block-a-seed picker)
POST /api/users/{id}/blocked-seeds {tmdb_id, title?, media_type?, year?} · DELETE /api/users/{id}/blocked-seeds/{tmdb_id} -> {blocked_seeds: [{tmdb_id, title, media_type, year}]}
     Titles that must never SEED this person's recommendations. The watch stays in their history, it just stops shaping their picks.
     Stored on `users.prefs`; an install that predates the richer shape holds bare TMDB ids and keeps working unchanged.
GET  /api/users/{id}/history (recent watches read LIVE from Plex; each item carries `title`, `media_type`, `year`, plus `season`/`episode`/`episode_title` for TV)
GET  /api/users/{id}/watched?q=&media_type=movie|show&limit=&offset= -> {items, total, last_full_sync_at, synced_titles, dislike_threshold, ratings_trusted, rated_count}
     Search this person's CACHED watched set — the same set recommendations are filtered against, so
     it can answer "I watched that, why was it recommended?". Unlike `/history` it never touches Plex,
     so it searches the whole set rather than the newest page, and each item carries `watch_count`
     plus `viewed_leaf_count`/`leaf_count` (null for movies) for the "3 of 8 episodes" progress.
     `last_full_sync_at` is null while ANY library has never had a full read — the set is incomplete.
     Each item also carries `user_rating` — what THIS person rated it in Plex, 0–10, or null if they
     never did (nearly always). The three page-level rating fields say whether that rating is acting:
     `dislike_threshold` is the cutoff in force (null = `recommendations.use_plex_ratings` is off),
     and `ratings_trusted` is false when this account's ratings look tool-written, in which case none
     of them are used whatever the threshold says. `rated_count` counts the whole set, not the page.
```

### Watching account (the owner's escape from seeing everyone's rows)

```
GET  /api/watching-account/candidates -> [{plex_account_id, title, protected, already_a_shortlist_user}]
     Plex Home users the owner could move their watching to. The admin account is never a candidate.
POST /api/watching-account/transfer {to_user_id, from_user_id?, dry_run?} -> {planned, applied, unreachable, failed, marks,
     unmarks, offsets_set, offsets_cleared, removals_preview, verify_mismatched, verify_checked,
     shows_cleared, target_unreadable, events_copied, titles_cached, snapshot_id, dry_run,
     source_empty, errors}
     Replicates one account's watch state onto the watching account: the exact episodes, the exact
     rewatch counts, and the exact position in anything part-watched.
     `from_user_id` defaults to the OWNER, which is the case the guide walks through. Name a
     different one when the history to copy lives on an account you already moved to — it is read
     with THAT account's own server token, never the admin's, so one person's history can never be
     copied under another's name. The web page offers this as "Copy the history from" once more than
     one candidate exists.
     Repairing an account an older Shortlist over-marked needs no special action: mirroring un-marks
     the spurious watches on the next run, and the preview says so when the removal count is large.
     MIRRORS — `unmarks`/`offsets_cleared` count what it REMOVES from the target because the SOURCE
     account has not watched it. That is what makes the result a replica, and what repairs an account an
     older Shortlist over-marked. Always dry-run first: `removals_preview` names up to 50 of them.
     A snapshot of the target is taken before the first write; `snapshot_id` is what `/undo` needs.
     `unreachable` counts titles in libraries that account cannot see (the PMS 404s) — normal, not a
     failure. `failed` counts writes that RAISED (a timeout, a 500) and is the opposite claim: not
     "that title isn't there for them" but "we don't know what happened". Those stay in
     `verify_mismatched` rather than being excused out of it. `verify_mismatched` comes from re-reading the target afterwards, so it reports what
     actually landed rather than what was sent.
     `source_empty` means the SOURCE ACCOUNT has nothing watched — told apart from `planned: 0`,
     which means the two already match.
     `shows_cleared` counts show rows un-scrobbled because every episode of them was removed — an
     episode un-scrobble does not clear its show, and a show left flagged at 0/N goes invisible to
     the watch cache. `target_unreadable` lists libraries the TARGET cannot see: not a failure, but
     it makes the snapshot partial, so undo is refused for it.
GET  /api/watching-account/snapshots -> [{id, user_id, username, taken_at, entries, complete}]
     Transfers that can still be undone, newest first. Needed because the undo is otherwise reachable
     only from the response of the transfer that created it — and the queue exists precisely so the
     work survives a request timing out, which is the case where that response never arrives.
     Snapshots an UNDO took are excluded: restoring one re-applies the transfer it reversed, and
     without the copied play events it would arrive undated. `complete: false` means a library was
     unreadable when it was taken, so restoring from it could remove watches it never recorded.
POST /api/watching-account/undo {snapshot_id, dry_run?} -> (same shape as /transfer)
     Restores the watching account exactly as the transfer found it, from that snapshot — counts and
     positions included, not just watched/unwatched. Refuses a second time rather than replaying.
     It is a mirror too, so it REMOVES anything watched on that account since the copy; dry-run it
     first and read `removals_preview`. It takes its own snapshot, so an undo is itself undoable.
```

### Rows

```
GET/POST /api/collections · PATCH/DELETE /api/collections/{id} (incl. `request_tag`, `candidate_sources`, `library_keys`, `max_seeds` — how many watched titles the row is built from (1–100; null inherits the engine default of 30), `recency` — how much a title's release date counts when ranking it for this row (0.0–1.0; null inherits the global `recommendations.recency`), `cold_start` — what the row does for someone below `recommendations.min_history` (`popular` | `skip`; null inherits the global `recommendations.cold_start`), `fallback_name` — what to call this row for someone whose name cannot be filled in, i.e. a `{top_seed}` row for a person with nothing watched. `""` (the default) means there is no such name and the row is simply not built for them — Shortlist never invents one, and a value containing `{top_seed}` is refused because it could not be filled in either, `seed_window` — how many recent watches a one-title row cycles between, one per run (1–20, default 1 = always their most recent; no global to inherit), `pick_order` — how the delivered collection is ordered (`best` | `rating` | `newest` | `shuffle` | `new_first` — titles that arrived this run lead | `rotate` — the front advances by one title a day, default `best`), `hub_anchor` — per-row shelf-placement override, and `poster` — custom row artwork {mode: ""|upload|generate, title, subtitle, style})
GET  /api/collections/{id}/effectiveness -> {delivered, watched, finished, first_delivered_at, matured_days, matured, per_library} (has this row actually landed? `matured` is null until picks are old enough to judge — a pick counts as a hit only if watched while the row was still showing it, so a newer row is reported as "too early" rather than scored 0%)
     `finished` accompanies every `watched` here too, including per library. A row spanning Movies and TV can land the same share in
     both and finish almost none of the TV — that gap is the panel's most useful line, and it is invisible in `watched` alone.
     `rewatch` (bool, default false) makes a REWATCH row: already-finished titles are ordered FIRST and unwatched ones only fill what is left.
     `watched_pct` cannot express this — it is a ceiling, so the ranking shows unwatched titles first and merely PERMITS finished ones; even at 1.0 a
     library with plenty of unwatched candidates yields a mostly-unwatched row. Setting `rewatch` also keeps finished titles in the row's candidate
     POOL regardless of `watched_pct`, so the two rows do not share one pool.
     `unstarted_only` (bool, default false; accepted on any row that can hold shows) drops every series the person has started, however little of it.
     It only changes anything on a row whose `watched_pct` is ABOVE 0: such a row caps FINISHED titles and so still admits a series someone is three
     episodes into, and this is what makes "a series to start" literally true there. At `watched_pct` 0 the row already excludes started series (see
     "What 'already watched' means for a show"), so the flag is a no-op. Refused for `media: "movie"`, where any view is already a finish.
     The finished bar itself is not configurable — it is `EngineConfig.watched_show_pct`, fixed at 0.8. Earlier revisions of this document cited a
     `recommendations.watched_show_pct` setting; no such key has ever existed.
     Both are refused (422) in combinations that cannot work: `rewatch` + `unstarted_only` together (they ask for opposite things — the row would fill
     with titles nobody has seen, under a "you've already seen" name), and `unstarted_only` on a `media: "movie"` row. PATCH validates the MERGED row,
     not just the fields sent, so neither invalid pair can be reached one field at a time.
POST /api/collections/{id}/cleanup {dry_run?} (remove this row's Plex collections for everyone; dry-run previews)
POST /api/collections/{id}/poster/upload (multipart image) · GET/DELETE /api/collections/{id}/poster/image (serve/remove uploaded artwork) · POST /api/collections/{id}/poster/preview {title,subtitle,style} -> generated sample image
```

### System, jobs and libraries

```
GET  /api/system/image-provider -> {capable, provider, reason} (can the AI provider generate poster images — drives the row editor's Generate gate)
GET  /api/system/logs?level=&q=&limit= (parsed + redacted log lines) · GET /api/system/logs/download (all log files as a zip; credentials, addresses and this server's machine id removed — the live view above strips credentials only, since it renders on the owner's own screen where the address is what makes a line readable)
GET  /api/system/libraries -> [{key, title, type}] (the server's Plex libraries, for the row editor)
GET  /api/system/jobs?kind=&limit=&before_id=&status= -> [{id, kind, payload, result, status, attempts, max_attempts, detail, error, created_at, started_at, finished_at}] (background maintenance history, newest first; `kind` narrows it to one job type, which is how the Jobs page shows a single job's own history; `status` narrows it to one of `queued`/`running`/`done`/`failed` and anything else is refused with 422 rather than ignored — the Jobs page's "N failed" badge counts every failed row in the table, so its list has to be able to reach past the newest page; runs have their own page)
GET  /api/schedule -> {jobs[{kind, label, setting, cron, using_default, default_cron, optional, writes_plex, next_run}], rows[{cron, rows[], next_run}]} (everything on a timer, rows grouped by shared cron exactly as the scheduler groups them. One trigger builds all of them). `cron` is the EFFECTIVE one with defaults resolved; `default_cron` is the built-in it falls back to when nothing is stored, and `using_default` says whether that is what it is running on. Read-only: crons are still edited through PUT /api/settings and PATCH /api/collections, so each one is validated in exactly one place
GET  /api/system/jobs/catalog -> [{kind, label, description, manual, trigger, scheduled, next_run, last, total, queued, running, failed}] (every job Shortlist can run, with its schedule, its tallies and its most recent run — the Jobs page renders straight from this, so labels can't drift from the handler registry)
POST /api/system/jobs {kind, payload?, background?} -> the job after an inline drain, or as soon as it is queued when `background` is set (the Jobs page uses that and polls, so a slow job can't end in a proxy timeout that reads as a failure). Only `sync.users`, `sync.history`, `sync.check`, `privacy.sync`, `backup.take` and `maintenance.prune` (retention trim of old runs, picks, log lines and expired caches — touches Plex not at all) may be triggered by hand. All of them passes that bring things back to how they should be that take no target. The rest are queued by the mutation that knows their target and are rejected here with 422: `user.cleanup` (someone turned off), `user.hide`/`user.restore` (paused/un-paused), `row.reconcile` (a row deleted, switched off, narrowed to fewer libraries, or with someone dropped from its audience). Queued work waits for any run in progress, is retried with backoff, survives a restart, and raises a notification if it gives up
GET  /api/system/libraries/{key}/collections -> [{title}] (a library's FOREIGN managed collections — the title-anchor choices for row placement; Shortlist's own are excluded, since a row is anchored to another row by slug via `hub_anchor[library].row`)
GET  /api/system/owned-collections -> {collections:[{library,title,label,rating_key,kind,slug,orphan}], total, orphans} (cleanup audit: every shortlist-labelled collection ON PLEX, drift-flagged, DB-independent)
```

### Runs

```
GET  /api/runs?limit=&collection=&before_id= (newest first; `before_id` pages backwards) · GET /api/runs/summary · GET /api/runs/{id} (each user carries `status`, `error`, `reason` — why a `skipped` user built nothing — `has_trace`, and `cost` — that person's timing and token spend for this run, `null` on a run recorded before this was measured, which must render as "not recorded" and never as `0s`; when present, `{setup_ms, rows: {row_slug: {duration_ms, blocked_ms}}, pools: [{label, tokens, exa_searches, duration_ms, rows}]}` — `setup_ms`/`pools` are the person's shared spend (history fetch + candidate gather), repeated across every row because it belongs to none of them; each row's own `duration_ms` is wall-clock INCLUDING `blocked_ms` (time spent waiting on the shared Plex write lock at concurrency above 1), so that row's own work time is `duration_ms - blocked_ms`; tokens are reported per POOL, never per row — all AI spend happens in the pool-scoped candidate gather, and pools are shared between rows, so a per-row token figure would be an allocation invented by the API rather than a measurement; `pools[].rows` names every row slug that drew on a given pool) · GET /api/runs/{id}/users/{user_id}/trace -> {username, display_name, status, error, reason, trace, breakdown} (the full per-user pipeline trace. History (with true distinct-title watched totals per library, split by media type) / seeds with each seed's weight ingredients, each source's queries+returns tagged with their fate (kept / already_watched / not_in_your_libraries / excluded_genre / lost_ranking_cutoff), the web-search/RAG prompts, resolved vs. hallucinated titles (the AI's resolved proposals carry the same fate so the UI marks each kept vs. dropped), plus `error`/`reason` for a failed or skipped person and `breakdown` (the delivered picks per library); a cold-start user carries a trace too (their thin history + a synthetic `cold_start` source), so `has_trace` is set and the "How we picked" page renders for them; fetched on demand, `trace: {}` on runs predating the feature) · GET /api/runs/{id}/log?after_seq=&format=json|text (the run's activity feed, kept in `run_log_lines` so an older run still has one; `after_seq` returns only what is new, `format=text` is the download) · POST /api/runs {user_ids?, collection_ids?, dry_run?} · POST /api/runs/{id}/cancel · DELETE /api/runs (clear all run history; changes nothing on Plex, and no longer disarms the row reconciles — they address collections by label + rendered title, not by run history)
```

### Requests

```
GET  /api/requests?wanted_by=&wanted_by= (the inbox, pending first then sent then rejected, capped at 500 rows; `wanted_by` repeats one `wanters` username per value and keeps a title any of them wanted — applied BEFORE the cap, so picking a name searches the whole history rather than the 500 the page loaded; omitted = everyone) · GET /api/requests/status -> {statuses: {request_id: "downloaded"|"downloading"|"queued"|"unmonitored"|null}, radarr: "ok"|"unreachable"|"off", sonarr: same} (live Sonarr/Radarr status for WAITING and SENT items — rejected are skipped; null = the app is fine and doesn't track it, which is why `radarr`/`sonarr` report reachability separately: an app that never answered would otherwise be indistinguishable from one with nothing to say. Fetched separately so the list itself makes no Arr calls, and read from whole-library maps so the cost doesn't scale with inbox size — which is what makes the inbox's poll cheap — it runs every 10s only while a title is actually downloading, and every 30s while an app is unreachable so the badge clears itself when it comes back; a settled inbox does not poll at all) · POST /api/requests/send {ids, dry_run?} · POST /api/requests/reject {ids} (permanent) · POST /api/requests/restore {ids} (un-reject → back to Waiting) · POST /api/requests/delete {ids} (removable; can re-surface) · POST /api/requests/clear {ids} (hide SENT items from the log without un-sending — the tombstone stays so the title isn't re-requested)
```

### Events and notifications

```
GET  /api/events (SSE) · GET /api/events/log?scope=&limit=&before_id= (audit feed; `before_id` pages backwards — a cursor rather than an offset, since events are appended while you read)
GET  /api/notifications -> {items[]} · POST /api/notifications/dismiss {id} (dismiss one alert)
     One of them is "Playback tracking is offline", raised when the PMS notification socket has been unreachable for 45+ minutes.
     Not dismissable (like "Runs are paused"): silencing it would leave you believing a feature is running that isn't. It clears
     itself on reconnect and raises again on a new outage — being undismissable, it is never hidden in the first place.
     The threshold is deliberately long — a container restart takes seconds and a Plex restart a minute or two, and alerting on
     those trains you to ignore the bell. A dropped socket is normal: the listener retries for ever, backing off 5s -> 120s.
     A socket that CONNECTS and immediately drops does not reset the clock: the handshake succeeding is not the same as frames
     arriving, and since the backoff caps at 120s, every flap cycle is shorter than the threshold — so a server flapping all
     night would otherwise have looked healthy. A connection has to survive 60s to count as recovery.
     An unreachable-but-configured Plex counts as an outage too; an install that has not finished setup does not.
     Worth alerting at all because the failure is SILENT: the nightly play-log sweep still credits finished watches, so every
     number keeps looking plausible. What stops is the partial-watch signal, which only the live socket can see — and
     "nobody abandoned anything this week" looks exactly like a healthy week.
```

### Settings and connections

```
GET/PUT /api/settings · POST /api/settings/test/{plex|tautulli|tmdb|llm|radarr|sonarr|mdblist|trakt|exa} (a PUT that changes anything also writes a `settings.change` audit event carrying `{changed: {key: {from, to}}, actor: {via, account_id, client}}` — `changed` covers the changed keys only (secrets record `<redacted>` on both sides, long object values are summarised), and `actor` says WHO: `via` is `browser` or `api_token`, `client` is a truncated User-Agent. No client IP is recorded, deliberately: these rows are immutable and the support bundle exports them. Read it back with `/api/events/log?scope=settings.change` to see which thresholds a past run actually used, and what changed them)
GET  /api/settings/arr/{radarr|sonarr}/options -> {quality_profiles, root_folders}
POST /api/settings/curator/models {provider?, api_key?, ollama_url?} -> {provider, models[]} (models the provider offers; the body lets the picker list the provider being edited before it is saved — blank fields fall back to saved settings, a redacted key means "use the saved key"; [] = free-text fallback)
```

### Reports and the dashboard

```
GET  /api/report?window=7|30|90|all -> {window, since, first_pick, overall, trend[], per_user[], per_row[], recent[], watch_sync, coverage, runs, requests, top_titles} (what got watched, from picks.watched_at)
     Windowed, default 30 days, with each headline figure carried alongside its previous equal period so the UI can show a change.
     `requests.watched_after_sent` compares a watch against `request_candidates.sent_at`, stamped once when the status flips
     to "sent" (rows predating that column fall back to `updated_at`). It used to be an unordered set intersection, which
     counted a title watched BEFORE it was ever requested.
     `first_pick` is the oldest pick on record (null when there are none). On a young install every window already covers all the data, so 7/30/90/all
     return identical numbers and the selector looks broken; the UI compares `first_pick` against `since` to say why rather than leaving it a mystery.
     `first_pick` also gates the comparison itself: `watched_prev`, `watchers_prev` and every `*_delta` are **null** unless the previous period is one
     Shortlist was installed for its whole length. A previous window that reaches back before the first pick would be counting a month the app did not
     run in, which reads as growth — a real server showed "53 watched, +53 vs previous" the day after its first-ever pick fell inside the comparison.
     Partial coverage is excluded for the same reason: it undercounts the earlier period, so every delta would lean toward good news. The UI says
     "no earlier period yet" rather than printing a comparison with no number in it.
     `overall.bounced` / `overall.dropped` split the picks that were STARTED and abandoned, by how far they got (under 5%, and past it).
     They come from live playback (`watch_sessions`), not from Plex's watched flag, which cannot see a partial play at all — so both are 0
     until the playback listener has observed some, and a title nobody has played since tracking began is in neither.
     SHARED rows count here too. They write no pick rows, so their credits live in `shared_row_watches` and are folded into the same
     per-(person, title) outcome — a title on both a personal and a shared row is one thing that person watched, counted once.
     A shared-row credit needs a play the delivery ledger and the run's own audience snapshot both agree that person could see at the time;
     Plex's watched flag alone never credits one, because everyone sees a shared row and a popular title would otherwise credit for everyone.
     Un-watching in Plex WITHDRAWS a credit, but only one that Plex's flag was the sole evidence for, and only on the weekly
     complete re-read. A credit backed by playback we observed — a live session or a play-log entry — is kept: it is a fact
     about a moment, not a mirror of a checkbox, and a partial watch never sets the flag at all, so withdrawing on absence
     would delete the exact signal this feature exists to capture. An incremental read withdraws nothing (it sees an un-watch
     only inside the window it covered), and neither does a read that came back empty for someone.
     Resuming later needs nothing special: progress is the furthest across ALL sittings and only ever moves up, so a second
     sitting extends the percentage, while the credit stays pinned to the first time they pressed play.
     Every figure that counts a WATCH includes them: `overall.watched`/`finished`/`watchers`, `bounced`/`dropped`, `trend`, `per_user`, `per_row`,
     `top_titles`, `recent` and `requests.watched_after_sent`. The ones that do not are `delivered`, `landing` (a ratio of delivered to watched) and
     `avg_days_to_watch` (an interval that starts at a per-person delivery): a shared row is ONE
     collection for the whole server, so there is no per-person delivery to count and inventing one would be a number with no referent. A shared
     row's `per_user`/`per_row` line therefore shows watched and finished with no "delivered" clause, which the UI already omits when it is zero.
     `overall.landing` is the one RATIO, and it is computed over a MATURED cohort: picks delivered in the window AND at least 30 days
     old. That matters — a pick stops being creditable once its row drops it, so counting a pick delivered
     yesterday in the denominator drags the rate toward zero for no reason. `per_user`/`per_row` return COUNTS, not rates, sorted by
     what was actually watched: at these sample sizes a percentage is noise, and sorting by one put `1/31` above `3/103`.
     Every count comes in a WATCHED/FINISHED pair (`overall.finished`, `landing.finished`/`finished_rate`, and a `finished` on each
     `per_user`, `per_row` and `trend` entry). `overall.finished` carries NO `_prev`/`_delta`, unlike its neighbours: this window's
     finishes are counted as of now while the previous window's have had an extra period to complete, so a server behaving perfectly
     steadily would report a permanent decline. The level is honest; a shifted-window change is not. `watched` is Plex's own flag, which for a SERIES flips on the FIRST finished episode —
     so one episode of a 60-episode show scores exactly like a whole film, and a TV row therefore out-scores a movie row for a
     structural reason rather than a real one (measured on a 47-user server: only 21 of 158 credited show picks had been finished).
     `finished` is the stricter count — a film played, or a series with every episode watched. See
     [Watched vs finished](#watched-vs-finished) for why the threshold is ours to choose. Sorting still uses `watched`, deliberately:
     ranking by `finished` would bury every TV row under every movie row.
POST /api/report/sync -> 202 (kick off a watch-history sync — re-reads every user's watched set from Plex so hit rates and "N titles watched" stay fresh between runs; writes nothing to Plex)
GET  /api/report/engagement?window=7|30|90|all -> {window, people[], losing[], stop_points[]} (what people DID with their picks: per person with how far
     each got, the titles several people start and few finish, and where abandons cluster). Outcomes are per (person, title): finished | dropped |
     bounced (under 5% in) | watching (credited, but no live session ever said how far). `percent` is null where no session observed the play —
     which is not 0%, and is the normal state for anything watched before playback tracking was running.
GET  /api/report/deleted-rows -> [{slug, picks, first_seen, last_seen}] (history left behind by rows that no longer exist, biggest first; NOT windowed — "what can I clear" is a question about all of it)
DELETE /api/report/deleted-rows?slug= -> {cleared, picks, slugs[]} (permanently delete that history; omit `slug` to clear every deleted row)
     Eligibility is recomputed server-side from `collections` vs the slugs in `picks` AND `shared_row_watches`, so naming a
     live row's slug deletes nothing and returns `cleared: 0` rather than erroring. The DELETE re-checks it in the same
     statement (`NOT EXISTS`), which closes the window in which a row re-created between the two would be treated as an orphan.
     `picks` rows AND `shared_row_watches` rows are removed — a SHARED row writes no picks at all, so its credits are the only
     history it has. The `picks` FIELD in both responses counts both (the name is kept for wire compatibility); one slug can
     carry both kinds, because a row's `build` can be switched from per-person to shared, and the GET's number is always what
     the DELETE will remove. `deliveries` is deliberately untouched — it is the ledger of which Plex collection is
     which row, and clearing it would strand a real collection with nothing left to clean it up. Audited as
     `report.clear_deleted_rows`, splitting the total into `pick_rows` + `shared_watches` with a per-slug count.
```

### Health, tokens and onboarding

```
GET  /api/system/health -> {status} (the ONE unauthenticated endpoint — liveness only, for Docker's HEALTHCHECK; the version lives on the owner-gated /system/version)
GET  /api/system/api-token -> {enabled, created_at, token} (owner-gated; token revealable) · POST /api/system/api-token -> {token, created_at} (generate/replace) · DELETE /api/system/api-token (revoke)
GET  /api/setup/servers (Plex server picker during onboarding) · GET /api/setup/state
POST /api/system/uninstall {confirm, dry_run?} -> {filters_restored, filters_skipped[], filters_unreachable[], filters_failed[], collections_deleted[], rows_disabled, dry_run, message} (the trust feature: switches every row off and clears its schedules, deletes every Shortlist collection, then restores each account's share filters from its pre-Shortlist snapshot — in that order, so the excludes hiding a row are never removed while the row still exists. `dry_run` previews; the real thing needs the literal string UNINSTALL, and 409s while an engine run is in flight. No single account can stop it: one that has left this server is reported in `filters_skipped`, one plex.tv's roster omits that Shortlist's own records say is here in `filters_unreachable` (worth retrying — that is what a partial roster read looks like), and one plex.tv refuses in `filters_failed`. A write plex.tv accepted but that could not be verified is still audited, with what was sent)
```

### Support checks ("Have an issue?")

Twenty-two read-only diagnostics behind `/issue` in the UI. **Nothing here writes** — not to Plex, not
to plex.tv, not to the settings a run reads. The only mutations are the mode's own switch and the
audit rows it leaves.

Two gates, not one. Every tool needs the owner session AND support mode switched on; the mode lapses
by itself after 24 hours. Being off by default matters because this surface reads share filters and
per-user tokens, and an install that never files a bug report should never expose it.

```
GET  /api/support/status -> {enabled, expires_at, seconds_remaining}   (owner only; usable while the mode is off)
POST /api/support/enable -> switches the checks on for 24h (audited: events scope `support.enable`)
POST /api/support/disable -> switches them off now
```

Everything below additionally requires the mode to be on, and returns **403** when it is not. Each
response carries its own fields plus `text`: a fixed-width block, ≤76 columns, that the UI's "Copy
for support" button puts on the clipboard verbatim. The block is rendered server-side so the format
a maintainer reads is decided (and tested) in one place. Credentials are stripped centrally before
anything reaches it, including from quoted exception messages.

```
GET  /api/support/health -> {checks[{name, ok, detail}], text} (Plex, libraries, tokens, TMDB, curator, database, clocks, last run — each probed independently so one failure is content, not a 500)
GET  /api/support/title?q= -> {rows[{user, watched_record, viewed_leaf_count, leaf_count, counts_as_watched, cap_pct, delivered[], problem}], flagged[], text}
GET  /api/support/person/{slug} -> {user_type, watched_movies, watched_shows, libraries[{section_key, library, titles_known, ever_read}], never_read[], text}
GET  /api/support/rows -> {rows[{slug, watched_pct, watched_pct_source, refresh_days, refresh_days_source, rewatch, unstarted_only}], global_watched_pct, text}
GET  /api/support/row-schedule -> {rows[{slug, refresh_days_source, rebuild_every_days, last_built_at, days_since_built, due}], text}
GET  /api/support/libraries -> {libraries[{key, title, type, items}], error, text}
GET  /api/support/connection -> {users[{user, has_token, libraries_read[], never_read[]}], problems[], text}
GET  /api/support/read-as?user=&endpoint=&section= -> {status_code, total_size, body, sections[], choices[], text}
GET  /api/support/sharing -> {accounts[{user, shortlist_excludes[], other_conditions[], should_hide[], missing[]}], rows_on_plex[], rows_error, missing_excludes_for[], error, text} (measured against the rows that EXIST on Plex, not the enabled-user list; reports `rows_error` rather than health when that read fails or comes back empty while marked rows exist)
GET  /api/support/surfaces -> {rows[{library, title, label, marked, rating_key, recommended, own_home, shared_home}], owner_label, on_owner_home[], on_owner_shelf[], unlabelled[], error, text} (the live Plex promotion flags per row: `own_home` on someone else's row is always a bug, since no share filter applies to the owner; `recommended` on one is the documented Plex limitation and a settings change)
GET  /api/support/drift -> {ledger_count, plex_count, marked_count, missing_on_plex[], orphans_on_plex[], error, text} (`marked_count` counts rows by invisible title marker, so "the rows were deleted" and "their labels are unreadable" stop looking identical)
GET  /api/support/pick?user=&title= -> {picks[{row, rank, seed_title, sources, affinity, reason}], text}
GET  /api/support/missing?user=&title= -> {verdict, hits[], run_id, text}
GET  /api/support/funnel?user= -> {stages[{pool, pooled, disposition{}}], delivered, run_id, text}
GET  /api/support/ai?user= -> {provider, model, llm_tokens, by_step{}, error, text}
GET  /api/support/timeline?user= -> {entries[{at_utc, at_local, kind, what}], text}
GET  /api/support/settings-history -> {changes[], last_build_at, change_after_last_build, text}
GET  /api/support/jobs -> {jobs[], counts{}, failed, text}
GET  /api/support/clocks -> {tz, local_now, utc_now, offset_hours, scheduled[], text}
GET  /api/support/database -> {head, tables_present, tables_expected, missing_tables[], indexes, size_mb, text}
GET  /api/support/config -> {settings[{env, key, env_set, secret, value, has_value}], text}
GET  /api/support/bundle.txt -> text/plain; every server-wide block in one downloadable file
GET  /api/support/report.zip -> the bundle plus every log file, redacted, as one attachment
GET  /api/support/suggestions -> {users[], titles[], libraries[]} (type-ahead for the inputs above; owner-only, never part of a report)
```

**What a report masks.** Credentials (rule 9), IP addresses, and this server's machine id — in the
report body, in every quoted exception, and in every log file inside `report.zip`. A URL keeps only
its scheme and port (`https://<host>:32400`), which are the parts with diagnostic value. The machine
id is additionally removed by exact match rather than by pattern alone, because it reaches a log line
URL-encoded (`uri=server%3A%2F%2F<id>%2F…`) where a word-boundary pattern cannot see it.

This is a filter for the shapes we know about, not a proof of absence, and the docs should not claim
otherwise — a promise the code cannot keep is what gets a report pasted unread. Log files are the
weak spot: they carry whatever a dependency chose to print. Every leak found so far arrived in an
escaping nobody had thought of (`%2F`, then `%252F`, then a `plex.direct` hostname with the id
embedded), so assume the next one will too. `tests/unit/test_redaction.py` is where a newly found
shape gets pinned.

**People are named, deliberately.** The report prints each account's Plex username and slug — a
maintainer cannot follow one person through a report otherwise, and the slug is what appears on Plex
as `shortlist_<slug>`. Anyone who would rather not publish them can replace them before posting. The
report BODY does not print nicknames or friendly names, which are free text and routinely someone's
real name; that is a property of the renderers, pinned by `TestNoDisplayNameReachesTheReport`.

That guarantee stops at the report body. The LOG FILES bundled in `report.zip` are not filtered for
names and nothing masks them — `redaction.py` knows about this server's own host and machine id, not
about people. A row template containing `{user}` renders as the nickname (`UserProfile.display_name`),
and the rendered title is logged verbatim on every delivery, so the nickname is in the logs. Say so
in the UI and the guide rather than implying the whole artifact is covered: the previous wording put
nicknames in a "Masked" column, which is exactly the overstatement that gets a zip pasted unread.

(An `anonymise=true` mode existed briefly in 1.2 and was removed before release: it governed only
these two endpoints, not the per-check `text` blocks beside them, so a tickbox reading "hide
everyone's names" covered less than it appeared to.)

**`read-as` runs against an allowlist, never a URL you supply.** `endpoint` is one of `libraries`,
`watched-movies`, `watched-shows`, `home-rows`, and `section` is validated against the keys the PMS
itself just reported. The container sits on a home network, so an arbitrary-URL fetcher behind owner
auth would be a port scanner with extra steps. It refuses (409) rather than falling back to the
owner's token when a person has no share token — reading as the owner would answer a different
question while looking like it worked.

**`drift` reports, it never repairs.** It also refuses to call anything missing when the Plex read
itself failed: an unread server is not an empty one, and treating it as one would mark every
delivered row as missing.

The AI provider (`curator.provider`) no longer ranks a fixed candidate pool. The engine does the
diversification and writes the genre-template reasons itself. The provider's one remaining job is the
`llm_web` source: it turns a person's recent watches into web searches for what to watch next. So a
run needs a provider only when `llm_web` is enabled; every other source is provider-free, and with
`curator.provider = none` you still get full rows ranked by score with plain reasons.

**`PUT /api/settings` validates values, not just keys.** `plextv.throttle_s` must be 0–60 (0 = fire
as fast as plex.tv accepts, with adaptive 429 backoff), `row.size` must be 5–40, `paused_all` must be a real boolean,
and `candidates.sources` / `curator.provider` are checked against their known values.

**`null` on a schedule key means "use the built-in default".** For any of the six `*_cron` keys,
`PUT /api/settings {"values": {"sync.check_cron": null}}` DELETES the stored value rather than
writing one, putting the job back on the built-in cron `GET /api/schedule` reports as
`default_cron`, and the live APScheduler trigger is rebuilt in the same request, not at the next
restart. It is the only way back for `sync.check_cron`, where a stored `""` means OFF rather than
"inherit"; writing the default expression itself would pin a copy of today's value instead. For the
other five, `null` and `""` land in the same place, because a blank already means "inherit".

Candidate sources are set globally (`candidates.sources`) and can be overridden per row
(`collections.candidate_sources`, `[]` = inherit the global set; valid values: `tmdb_similar`,
`tmdb_discover`, `trakt`, `llm_web`). `llm_web` proposes titles to watch next from a
live web search, each resolved via TMDB search then library-verified. It works on **every** AI
provider via `llm_web.search_provider`, which names exactly ONE backend: `native` (the default) uses
the provider's own web-search tool (Claude, GPT, or Gemini), `exa` uses the hosted Exa API
(`exa.apikey`), and `searxng` uses your own SearXNG instance (`searxng.url`). Either external backend
is a path for a local model, which cannot search on its own. Only the named backend runs, so a title
is never searched — or billed — twice. A fourth value, `auto` (native unioned with an external), was
removed in 1.3; migration 0063 pins every install to the backend it was actually using. When a
source's dependency is missing, the Settings UI keeps the toggle usable but shows an inline fix
(enter the key right there, or set up an AI provider). It never reads as on while silently doing nothing.

Config changes reconcile onto Plex immediately, without waiting for a run. Deleting a row, disabling
a user, and dropping a user from a row's audience all remove the now-stale collections (a removal, so
gate-exempt); renaming a row retitles its collections in place for every user (privacy-neutral, since the
hiding filter is keyed on the row's label, which never changes). A per-person row's per-user
collection is found by the exact title the last run delivered for it (the run's persisted breakdown),
scoped to that user's own label, so a reconcile can never touch another user's row or a foreign
(Kometa) collection. Each row also has a **Remove from Plex** button (`POST
/api/collections/{id}/cleanup`, dry-run-able) for an on-demand sweep. Every reconcile is audited.

A row builds a Plex collection in each library it targets (`collections.library_keys`, a list of
Plex section keys; `[]` = every library of the row's media type. The default). A row's `media` is
derived from the types of its selected libraries. This lets an owner point a row at a specific
library (e.g. only "4K Movies") on a server with several libraries of one type. A row builds **per
library**: each targeted library seeds from its own watched history and fills to `row.size` on its
own, so a movies-and-TV watcher gets a full movie row AND a full TV row.

Placement is per row and held **once per audience**: `collections.placement` for the owner's own
collection and `collections.placement_friends` for each friend's (both `both` \| `home` \| `library`
\| `off`, default `both`). Each decodes to two of Plex's three promotion flags. `home` is
`promotedToOwnHome` on the owner's side and `promotedToSharedHome` on the friends' side, `library`
is `promotedToRecommended`, and `off` claims neither surface (the collection still exists and is
still browse-hidden, so it stays reachable from the library's Collections tab). Exception: when a
run cannot map an existing collection back to its row, that collection keeps its audience's Home
flag for that run, never the Recommended shelf, which is the one surface the owner cannot filter.

The two sides are independent because **every person gets their own Plex collection**, so
`promotedToRecommended` is set per collection rather than once for the row. That is what lets an
owner keep their own row on the Recommended shelf without every friend's row landing there too.
The one thing it cannot do is the reverse: a friend's row on the Recommended shelf is also visible
to the **owner**, because the owner has no share filter to hang a `label!=` exclude on. Shortlist
says so at the control rather than pretending otherwise. A **shared** row is one public collection
rather than one per person, so it has nothing to split on, so it takes both Home flags and the union
of the two `library` settings.

WHERE in that shelf it sits is the **Position** control (`collections.hub_anchor`, per library:
`{"top": true}`, `{"row": "<row slug>", "before": bool}`, or
`{"anchor": "<collection>", "before": bool}`); it
replaces the old `pin_top` toggle (still honoured for rows not yet re-saved). This order is Plex's
Managed Recommendations, which are **server-wide**, because Plex exposes no per-viewing-user hub order.

A row can be positioned relative to **another Shortlist row** (`row`, a row slug) or to a foreign
collection (`anchor`, a title) — one or the other, never both; `row` is what the engine reads first.
It is a slug and not a title because a per-person row is one Plex collection PER PERSON, so a title
names one account's copy and would place the row for that account alone. The rows of a library are
then applied in dependency order, so a row always lands after the one it follows has itself been
placed. Two rows pointing at each other, or a row pointing at itself, is refused when you save it;
if an anchor row has nothing in that library yet, the rows following it are left where they are for
that run rather than falling back to a different slot.

Request tags are three-layered: the global `requests.tag` setting, a per-user `request_tag`
(`PATCH /api/users/{id}`), and a per-row `request_tag` (`collections`, per-person rows only:
shared rows never request). A requested title is tagged with the union of the global tag, every
wanting user's tag, and the tag of every per-person row that user is in the audience of; the queued
tags round-trip through `GET /api/requests` (`tags[]`) and are applied on `send`.

The per-user layer can also be filled in automatically. With `requests.auto_user_tag` on, a user who
has no `request_tag` of their own contributes their SLUG instead, so every request is attributable to
a person in Sonarr/Radarr without hand-setting a tag on each user. An explicit `request_tag` REPLACES
the slug rather than stacking with it — carrying both is the clutter the automatic tag was dropped
for in 2026-07. A row may override the switch either way (`req_auto_user_tag`; NULL inherits), and
the override governs only the automatic slug: a tag the owner typed on a person is never dropped.

Slugs are sanitized to the Arr tag charset (`a-z`, `0-9`, `-`) before being sent, so `moo_house`
becomes `moo-house`. Note what the tag can and cannot tell you: a title the Arr ALREADY tracks is
skipped entirely by `add_movie`/`add_series`, tags included, so the tag records who triggered the
original add — not everyone who has wanted the title since. The full wanters list lives in the
Requests inbox (`why[]`), which never reaches the Arr.

Before queuing, the request pass reconciles the missing pool against the Arrs (one bulk fetch each,
failing open on error): a title Sonarr/Radarr already tracks is dropped, since it is not really "missing", just
not imported into Plex yet. Matched on tmdbId for movies and tvdbId for shows (the candidate's TVDB
id is resolved once and reused for the send). A title on an Arr import-exclusion list (usually a past
delete) is kept but flagged (`excluded` on `GET /api/requests`) and never auto-sent, so the inbox can
warn that approving it is a no-op until the exclusion is removed in the Arr. A sent title records the
Arr's `titleSlug` (`arr_slug` on `GET /api/requests`) so the Sent log deep-links straight to its
Sonarr/Radarr page; each candidate also carries TMDB's `poster_path` (`"/abc.jpg"`, or `""` when
TMDB has no artwork). A path and not a URL, because the image host and size buckets are TMDB's to
change, so the web UI builds the URL itself and draws a placeholder tile when the path is empty. It
also carries TMDB's synopsis (`overview`, `""` when TMDB has none or the row predates the field), so
an unfamiliar title can be judged in the inbox; both ride in the same TMDB list response, so neither
costs an extra call, and both backfill on the next run that re-surfaces the title. **Clear** (`POST /api/requests/clear`) hides a sent entry via a `hidden` flag without
deleting the tombstone that stops a still-downloading title being re-requested.

All endpoints except `/api/system/health` require the owner session; mutations require the
`x-shortlist-csrf: 1` header.

**Programmatic access (API token).** For scripting, generate an owner token in Settings → Advanced →
API access (or `POST /api/system/api-token`) and send it as `Authorization: Bearer <token>`. It
grants the same owner-level access as the browser session and needs no CSRF header (a browser never
sends it automatically). The token is stored encrypted at rest (Fernet, like the Plex/AI-provider keys)
and stays revealable to the owner. The Settings card and `GET /api/system/api-token` show it
(owner-gated) so you can copy it any time; it never appears in `GET /api/settings`. Regenerating or
revoking (`DELETE /api/system/api-token`) invalidates the old token immediately.

```
curl -H "Authorization: Bearer <token>" https://<host>/api/runs
```

## Watched titles (and why one can still be recommended)

Shortlist excludes what someone has already watched. Each run reads every user's **complete watched
set directly from your Plex server, as that user**, with no extra configuration, no database mount, and
it works for every account on the server.

The mechanism is the per-user server token Plex already mints for every share. When you share
libraries with someone, plex.tv issues a server-scoped `accessToken` for their account
(`GET /api/servers/{machine}/shared_servers`); reading `library/sections/{key}/all?unwatched=0` with
that token returns exactly the titles Plex considers watched **for them**, carrying their own
`viewCount` (movies) and `viewedLeafCount`/`leafCount` (shows). The owner isn't shared to their own
server, so their set is read with the admin token; a managed Home profile with no share of its own is
read by briefly switching to it and exchanging for a server token (the same path the privacy system
uses).

This closes the gap that used to let watched titles reappear. Plex has two notions of "watched": a
**playback session** (something was streamed) and a **mark-as-watched** (ticked off, or a whole
season marked, with no play). The old playback-history API returned only the former, and capped at
roughly the most recent 200 plays, so a heavy watcher's older titles and everyone's marks were
invisible, and already-seen films kept coming back. `viewCount > 0` (what `unwatched=0` filters on)
counts **both**, at any depth. On one real server that meant seeing all ~13k watched titles instead
of the ~1k the API reported.

### What "already watched" means for a show

A movie is watched the moment it is played. A show has no such moment — Plex gives no show-level
`viewCount`, only `viewedLeafCount` and `leafCount` — so every yes/no answer is a threshold over
those two numbers, and **which threshold depends on where `recommendations.watched_pct` sits**:

| Cap                 | What it excludes                                                                                                                                                | A show they're 2 episodes into |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| `0.0` (the default) | Everything they've **touched** — any show with at least one watched episode. This is Plex's own answer; `unwatched=0` returns a series from its first episode.  | Excluded                       |
| above `0.0`         | A ceiling on **finished** titles: at most that share of the row may be things they've completed. Unwatched titles still come first — it permits, never prefers. | Allowed (it isn't finished)    |

"Finished", used by the cap only, is `viewedLeafCount` against 80% of the episodes with a
length-scaled floor (`max(3, 15%)`), so a long-running series someone is genuinely deep into isn't
treated as fresh while three episodes of a 200-episode run isn't treated as finished.

> **Changed in 1.2.** A 0% row used to exclude only _finished_ shows, so one you were two episodes
> into was, to the row, a fresh discovery and could be recommended straight back. A live probe of a
> real server found `?type=2&unwatched=0` returning shows as little as 1.1% watched (2 of 176), and
> five of ten started shows there were still eligible. At 0%, started now means watched. Rows above
> 0% are unchanged. If you relied on the old behaviour, set the cap above 0.

A row that should LEAD with rewatches needs the per-row `rewatch` flag instead. `unstarted_only`
still exists and still matters — but only on a row whose cap is **above** 0%, since a 0% row now
drops started series anyway.

### Watched vs finished

The dashboard reports both, and they are different questions. **Watched** is Plex's own flag: a film
played, or a series with at least one finished episode. **Finished** is a film played, or a series
with **every** episode watched.

The gap is not a rounding error. On a real 47-user server, of the 158 show picks credited as watched,
only 21 had actually been finished — 31 were a single episode. A single "watched" count therefore
flatters television structurally, and a TV row will out-score a movie row on it without being any
better. Both numbers are shown side by side so that comparison stops being misleading; lists are
still SORTED by watched, because ranking on finished would bury every TV row under every movie row.

Three thresholds exist in Shortlist and they are deliberately not the same, because they answer
different questions:

| Where                 | Bar for a series                 | Question it answers                             |
| --------------------- | -------------------------------- | ----------------------------------------------- |
| Dashboard `watched`   | 1 episode (Plex's own)           | Did they start it?                              |
| Recommendation engine | `min(80%, max(3, 15%))` episodes | Are they engaged enough not to re-recommend it? |
| Dashboard `finished`  | every episode                    | Did they see it out?                            |

Plex publishes no show-level finished flag, so the last one is Shortlist's own threshold — the
strictest and least arguable of the options, and the same wording a person's page already uses per
title ("3 of 12 episodes" / "finished").

**Backfill.** `picks.finished_at` was added in migration 0072. Films were backfilled exactly, since
a film's watched flag IS completion. Series were deliberately left empty and fill in going forward:
which shows are past the bar today is knowable, but _when_ they crossed it is not, and inventing that
date would file old watches in the wrong week of the trend chart permanently. A series already
credited as watched is picked up on the first sync after it completes.

**Why a watched title can still appear:** the read is per-run, so a title marked watched _after_ the
last run stays eligible until the next run re-reads. Between runs, **Jobs → Sync history**
(`POST /api/report/sync`) re-reads every user's watched set on demand. It writes nothing to Plex,
only refreshes what Shortlist knows, so hit rates and the per-user "N titles watched" count stay
current without waiting for a scheduled run.

## Why you see everyone's rows (and the watching account)

If you own the server, the **Recommended shelf** inside each library shows you every person's row,
not just yours. This is a Plex limitation with no setting behind it: rows are hidden from other
people through the _share_ each of them has with your server, and you have no share with yourself,
so there is nothing for Plex to hide them behind. Your own **Home screen** is unaffected — Plex
tracks "on the owner's Home" separately from "on a friend's Home", so nobody else's row lands there.

**Users → You see everyone's rows** (`/watching-account`) lays out the three ways to deal with it:

1. **Take the rows off the library shelf.** Everyone still gets their row on their Home screen, and
   nobody — including you — sees anyone else's. You lose the row inside Movies and TV Shows. One
   click; it flips `placement_friends` on every per-person row and leaves the Home half alone.
2. **Leave it.** Some owners genuinely don't mind. Dismissing stops Shortlist mentioning it.
3. **Move your watching to a separate account.** Keep the library shelf _and_ stop seeing everyone
   else's rows. You create a Plex Home user (Plex → Settings → Home → Add user), share the same
   libraries with it, and Shortlist copies your watch history across so its picks are right from the
   first run.

The copy reads your account straight from Plex — per episode, including anything you are part-way
through — and writes the same state onto the new account. It does not read Shortlist's cache, so it
works during the setup wizard before anything has been synced.

### What "copies your watch history" means exactly

The new account ends up **matching** yours:

- **Per episode, not per show.** A series you are 400 episodes into arrives 400 episodes in. Earlier
  versions marked the whole show watched, because they wrote the show's rating key and Plex treats
  that as "mark every episode" — on a real account 342 of 535 watched shows were partial, so this was
  the common case rather than an edge.
- **Rewatch counts.** A film you have seen three times arrives at three.
- **Part-watched films and episodes.** They land at the same position and show up in Continue
  Watching. Nothing before this could carry them at all: the read behind it (`?unwatched=0`) only
  ever returned completions.
- **It removes as well as adds.** Anything watched on the target that you have not watched is
  un-marked. That is what makes it a replica rather than a merge — and it is what repairs an account
  an older version over-marked. The web UI previews the removals **by title** and requires them to be
  acknowledged before the real run.
- **It is reversible** — unless that account cannot see all of your libraries. The target's complete
  state is snapshotted before the first write, and **Undo** restores it exactly, counts and positions
  included. If a library is unreadable for that account the snapshot cannot be complete, so the undo
  is refused rather than restoring from a partial picture; the preview says so before you agree.
- **Your own account is never written to.** The copy reads yours and writes only with the target
  account's own token.

Afterwards Shortlist re-reads the target and reports what did not land, rather than reporting the
writes it sent.

### The date problem, and `source_viewed_at`

Plex cannot backdate a watch. Every write — `/:/scrobble`, `/:/progress`, `/:/timeline` — is stamped
**now**, and no endpoint accepts a date. Copying two thousand titles onto a new account therefore
tells Plex they were all watched today, and the next watch sync would write exactly that into
`watched_titles.viewed_at`.

That matters more than it sounds: Shortlist picks seeds from the **most recent** watches, so a set
where every row shares one timestamp orders arbitrarily and the new account's recommendations become
noise. The migration that gave someone their history back would be the one that broke their picks.

Three things keep the dates:

- **`watched_titles.source_viewed_at`** records the true date per cached title. The watch sync never
  overwrites it and never deletes a row carrying one, and every "how recently?" read prefers it.
  `NULL` — the value on every row Plex reported directly — means `viewed_at` is the truth, so nothing
  changes for anyone who never runs a transfer.
- **Your play log is copied** onto the new account's `watch_events` with its original timestamps, per
  episode. Those rows carry `source='transfer'` and are deliberately excluded from pick attribution:
  they are real watches for recommendation purposes, but they are not that person pressing play on a
  Shortlist row. This is the only dated history the account gets — a scrobble writes no entry to
  Plex's own history log.
- **Writes go oldest first.** The dates cannot be replicated, but the ORDER can, and `lastViewedAt`
  order is what Continue Watching and "recently watched" sort on. So the shelves come out right even
  though every date reads as today.

## How a pick is chosen (and why a row can be short)

Every candidate carries an **affinity**: how strongly the source that produced it vouched for it,
0..1:

- **TMDB** sets it from which endpoint suggested the title and how near the top of that list it sat
  (`/recommendations` is worth more than `/similar`, and both decay down their list), multiplied by
  a **genre-coherence** factor: the share of the candidate's own genres that the seed does not have.
  TMDB tags a medical drama simply "Drama" and so is nearly everything it suggests, so overlap alone
  discriminates nothing, but a suggestion also tagged "Sci-Fi & Fantasy" is measurably further away.
- **Sources with no ranking of their own** — `tmdb_discover`, `trakt`, `llm_web` —
  report the neutral `1.0`. That means "no ranking information", not "perfect match"; they are
  deliberate picks rather than the tail of a list, and `pre_rank`'s per-source round-robin is what
  keeps them competing fairly.

Ranking is `(1 + seed_frequency) × rating × (1 + seed_weight) × affinity`, so a well-rated but
distant title no longer beats an obviously similar one.

**A row is allowed to come up short.** Padding a partly-filled row only draws from candidates at or
above `MIN_FILLER_AFFINITY` (0.35). Four genuinely-similar titles beat ten where six are filler.
When that happens the run log says so at INFO, naming the closest rejected title, so a short row
reads as the filter working rather than as a failure.

Each delivered pick records its provenance (`sources`, `affinity`, returned by `GET /api/users` and
the run detail) and the UI shows it under the title, as _"suggested by TMDB · loosely related"_. At
DEBUG the run log prints the same per row: every pick with its seed, source and affinity.

## How rows stay private

Each row is a Plex collection labelled `shortlist_<userslug>`. Every _other_ account's share filter
gets a `label!=shortlist_<userslug>` exclusion (merged into their existing `filterMovies` /
`filterTelevision`, never rebuilt), so only its owner ever sees it. The write ordering is what keeps
this leak-safe: a run delivers rows **unpromoted**, merges all the exclusions, and only **then**
promotes rows onto Home, so a new row is never visible before the exclusion that hides it exists. Rows
Plex cannot hide (wrong media type for their library) are swept away first, before anything else.

Every row also carries a second, constant label — `shortlist` (Plex stores it title-cased, as
`Shortlist`). It hides nothing and excludes nothing; it exists so a co-managing tool can be told to
leave Shortlist's rows alone in **one** entry. Agregarr's _Exclude from Ordering (Plex Label)_ and
Kometa's equivalents take a list of labels, and the per-person ones are no use there: a 46-account
server has 46 of them plus one per shared row, and the list goes stale the moment somebody joins or
leaves. Existing rows pick the label up the next time they are built — there is nothing to run.

Before Shortlist first edits an account's filters it snapshots them (`restriction_snapshots`), so
**Uninstall** restores every share exactly as it found it. The one hard requirement is a Plex Media Server
**≥ 1.43.2.10687** (older builds ignore the label exclusion).

> Earlier versions ran an automatic _Privacy Check_ that verified the hiding before each write and
> refused to write if it couldn't confirm it. That check + its write gate were removed at the
> maintainer's request; the hiding above still happens on every run, but it is no longer verified
> after the fact.

## Files under /config

`shortlist.db` (SQLite: settings, users, runs, restriction snapshots, and the
durable plex-account-id → slug map a row's label is built from) · `secret.key` (Fernet, 600) ·
`session.secret` · `logs/`.
