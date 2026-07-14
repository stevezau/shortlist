# Reference

## Environment variables (container)

| Variable                                                                   | Default   | Live or seed                                                          |
| -------------------------------------------------------------------------- | --------- | --------------------------------------------------------------------- |
| `PORT`                                                                     | `5959`    | live                                                                  |
| `TZ`                                                                       | `Etc/UTC` | live                                                                  |
| `PUID` / `PGID`                                                            | `1000`    | live                                                                  |
| `ROWARR_CONFIG`                                                            | `/config` | live                                                                  |
| `PLEX_URL`, `PLEX_TOKEN`, `TAUTULLI_URL`, `TAUTULLI_APIKEY`, `TMDB_APIKEY` | —         | **seed once**: copied into settings on first boot, ignored afterwards |

## Settings keys (DB-backed; Settings UI or `PUT /api/settings`)

| Key                                                   | Default                            | Notes                                                                  |
| ----------------------------------------------------- | ---------------------------------- | ---------------------------------------------------------------------- |
| `plex.url` / `plex.token`                             | —                                  | token stored Fernet-encrypted, redacted in API                         |
| `tautulli.url` / `tautulli.apikey`                    | —                                  | optional                                                               |
| `tmdb.apikey`                                         | —                                  | required for personal mode                                             |
| `curator.provider`                                    | `none`                             | `anthropic` \| `openai` \| `google` \| `ollama` \| `none`              |
| `curator.api_key` / `curator.model`                   | —                                  | BYO key; sensible default model per provider                           |
| `curator.prompt_tone`                                 | `balanced`                         | `balanced`\|`warm`\|`concise`\|`cinephile`\|`playful`                  |
| `curator.prompt_guidance`                             | —                                  | free-text notes injected into the curation prompt                      |
| `curator.prompt_template`                             | —                                  | full custom system prompt; blank = built-in skeleton                   |
| `row.name_template`                                   | `✨ Picked for You`                | `{top_seed}` and `{user}` placeholders                                 |
| `row.size`                                            | `15`                               | 10/15/20 in the UI; budget across a user's rows                        |
| `schedule.cron`                                       | `30 3 * * *`                       | full cron, applied live                                                |
| `staleness_runs`                                      | `3`                                | prefer titles not picked in the last N runs                            |
| `candidates.sources`                                  | `["tmdb_similar","tmdb_discover"]` | candidate sources to pool: `tmdb_similar`, `tmdb_discover`             |
| `plextv.throttle_s`                                   | `1.0`                              | plex.tv write spacing (rule: ≤1 write/s)                               |
| `requests.enabled`                                    | `false`                            | ask Radarr/Sonarr for picks the library lacks                          |
| `requests.radarr.url` / `.apikey`                     | —                                  | Radarr (movies); key stored Fernet-encrypted, redacted                 |
| `requests.radarr.quality_profile_id` / `.root_folder` | `0` / —                            | picked from dropdowns in the UI (fetched from Radarr)                  |
| `requests.sonarr.url` / `.apikey`                     | —                                  | Sonarr (shows); key stored Fernet-encrypted, redacted                  |
| `requests.sonarr.quality_profile_id` / `.root_folder` | `0` / —                            | picked from dropdowns in the UI (fetched from Sonarr)                  |
| `requests.rating_source`                              | `tmdb`                             | `tmdb` (no setup) \| `imdb` (needs an OMDb key)                        |
| `requests.omdb.apikey`                                | —                                  | free OMDb key; required for the `imdb` source; encrypted               |
| `requests.min_rating`                                 | `7.0`                              | score floor (0–10) on the chosen source                                |
| `requests.min_votes`                                  | `100`                              | vote-count floor on the chosen source                                  |
| `requests.min_demand`                                 | `1`                                | request only titles wanted by ≥ N distinct people                      |
| `requests.min_year`                                   | `0`                                | `0` = any; else request only titles released ≥ this year               |
| `requests.max_per_run`                                | `5`                                | hard cap on titles **auto**-requested per run, both apps               |
| `requests.auto_send`                                  | `true`                             | `false` = fully manual; every qualifying title is queued               |
| `requests.auto_min_demand`                            | `3`                                | auto-send only titles wanted by ≥ N distinct people                    |
| `requests.auto_min_rating`                            | `8.0`                              | ...and rated ≥ this on the chosen source; rest are queued              |
| `requests.tag`                                        | `shortlist`                        | tag added to every requested title (created in the app; `""` = no tag) |

## CLI config file (`<config-dir>/config.yml`)

See the docstring in `shortlist/cli.py` — same knobs as above in YAML form, plus `users:`
(list or `all`), `user_overrides:` and `canary:`.

## API

Interactive docs at `/api/docs` (OpenAPI at `/api/openapi.json`). Highlights:

```
POST /api/auth/pin · GET /api/auth/pin/{id} · GET /api/auth/session · POST /api/auth/logout
POST /api/setup/probe · POST /api/setup/link · GET/PUT /api/setup/state
GET  /api/users · PATCH /api/users/{id} · POST /api/users/sync
GET  /api/users/{id}/rows · PUT /api/users/{id}/rows/{collection_id} {muted?, row_size?, prompt_*?}
GET  /api/users/{id}/runs · GET /api/users/{id}/history
GET/POST /api/collections · PATCH/DELETE /api/collections/{id}
GET  /api/runs · GET /api/runs/{id} · POST /api/runs {user_ids?, dry_run?}
GET  /api/requests · POST /api/requests/send {ids, dry_run?} · POST /api/requests/reject {ids}
GET  /api/events (SSE) · GET /api/events/log (audit feed)
GET  /api/privacy/status · POST /api/privacy/check {probe?} · GET /api/privacy/snapshots
GET/PUT /api/settings · POST /api/settings/test/{plex|tautulli|tmdb|llm|radarr|sonarr|omdb}
GET  /api/settings/arr/{radarr|sonarr}/options -> {quality_profiles, root_folders}
POST /api/settings/prompt-preview {tone?, guidance?, template?, shared?} -> {system, user}
GET  /api/system/health · POST /api/system/uninstall {confirm: "UNINSTALL"}
```

The curation prompt is tunable: a `tone` preset + free-text `guidance` + an optional full custom
`template` (the fixed output contract is always re-appended, so edits can't break a run). Set the
global recipe via the `curator.prompt_*` settings; override any field per user via
`PATCH /api/users/{id}` prefs (`prompt_tone` / `prompt_guidance` / `prompt_template`, empty =
inherit). `prompt-preview` assembles the prompt against sample data so the UI can show the effect.

All endpoints except `/api/system/health` require the owner session; mutations require the
`x-rowarr-csrf: 1` header.

## The write gate

Shortlist refuses real (non-dry-run) writes unless **both** hold:

1. A **passing Privacy Check** is on record and is at most **7 days old** (every tier's most
   recent result must pass — a stale T2 failure can't be masked by a newer T1-only pass).
2. The linked PMS is **≥ 1.43.2.10687**.

The web app records checks in the `privacy_checks` table. **Every real run runs the check itself as
its first phase** — if the gate isn't already open, the run performs the check, records the result,
and only then decides whether to write; the owner never runs it by hand. A manual re-check is still
available (Settings → Re-check privacy), and the CLI records checks in `privacy_check.json` via
`shortlist verify`. A refused run is recorded as an errored run with a plain-English reason — it
never half-applies.

## Privacy Check tiers

| Tier      | What it does                                                                                                                                                                                           | Cost                   |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------- |
| **T1**    | Reads back the share filters of EVERY account the server is shared with and asserts the expected exclusions are present                                                                                | seconds, read-only     |
| **T2**    | Fetches a canary Home user's own Home hubs and asserts no other user's collection id appears                                                                                                           | seconds, read-only     |
| **PROBE** | Creates a throwaway labeled collection, promotes it, confirms the canary can see it, excludes it, confirms it disappears — then restores filters byte-identically and deletes the probe (in `finally`) | ~90s, fully reversible |

Each real run auto-runs PROBE (when a canary Home user exists, else T1/T2) before it writes. The
weekly scheduled check runs T1 + T2. `rowarr verify --probe` runs PROBE from the CLI.

## Files under /config

`rowarr.db` (SQLite) · `secret.key` (Fernet, 600) · `session.secret` · `logs/` ·
`privacy_check.json` (CLI gate record) · `snapshots/` (CLI mode) · `slugs.json` (CLI mode: the
durable plex-account-id → slug map a row's label is built from — never reassigned).
