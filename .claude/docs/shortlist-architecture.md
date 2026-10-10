# Shortlist — Architecture & Execution Plan

**Status:** shipped (1.x, in production); the layout in section 2 is a map, not a contract, so read the tree when it matters · **Date:** 2026-07-12 ·
**Companions:** [`shortlist-design.md`](shortlist-design.md) (product/UX design) · media_preview_generator
(MPG, `stevezau/media_preview_generator`) — the donor repo for release infrastructure.

---

## 1. Verdict on reusing MPG's chassis

Reviewed MPG in full (July 2026): README/docs structure, `.claude/`, `.github/`, packaging, tests.
MPG is a mature shipping app (1,321 tests, ~79% coverage, codecov, multi-arch Docker, Unraid
templates, PR preview images). **Port the chassis wholesale; write the app fresh.** The chassis is
framework-agnostic; the app layer (Flask+SocketIO+Jinja in MPG) is NOT what Shortlist needs (see §3).

### Reuse manifest (port from MPG → shortlist)

| Asset                                                                                                                                                                                | Action                                                                                                                                                   |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.claude/rules/{python,testing,commenting,docker,docs,shell}.md`                                                                                                                     | Port near-verbatim; add `frontend.md` (React/TS) + `plex-safety.md` (Shortlist-specific, §8)                                                             |
| `.claude/CLAUDE.md`                                                                                                                                                                  | Rewrite content, keep the proven section structure (Commands / Architecture / Code Style / Conventions / Security / Test Fixtures)                       |
| `.claude/agents/architecture-review.md`                                                                                                                                              | Port — pre-commit arch-review agent, blocking on HIGH findings (this caught 8 production-bug shapes in MPG; keep the discipline from day 1)              |
| `.claude/settings.json`                                                                                                                                                              | Port permission-allowlist pattern (+ pnpm/vitest/playwright allows, same `.env` denies)                                                                  |
| `.github/workflows/ci.yml`                                                                                                                                                           | Adapt: ruff + pytest/codecov jobs stay; add `web` job (pnpm lint/typecheck/vitest/build); docker buildx multi-arch publish                               |
| `.github/workflows/docker-pr.yml` + `docker-pr-cleanup.yml`                                                                                                                          | **Not ported.** PR preview images were dropped — `ci.yml` publishes on `dev` pushes and `v*` tags only. Revisit if per-PR pullable tags are wanted again |
| `.github/workflows/architecture-review.yml`                                                                                                                                          | **Not ported.** The Architecture Review runs as an on-demand agent, not a workflow — see the dispatch criteria in CLAUDE.md                              |
| `.github/ISSUE_TEMPLATE/`, `PULL_REQUEST_TEMPLATE.md`                                                                                                                                | Port                                                                                                                                                     |
| `.pre-commit-config.yaml`, `.codecov.yml`, `.dockerignore`                                                                                                         | Port                                                                                                                                                     |
| `README.md` structure                                                                                                                                                                | Port the shape: shields (+ AI-Assisted badge), logo, About/Problem/Solution, screenshots table, Quick Start, docs-hub table                              |
| `docs/` hub (`README/getting-started/guides/reference/faq`)                                                                                                                          | Port structure                                                                                                                                           |
| `docker-compose.example.yml`, `unraid-templates/`                                                                                                                                    | Port patterns (Unraid = big homelab reach)                                                                                                               |
| `docs/llms.txt`                                                                                                                                                                         | Port (AI-readable repo summary)                                                                                                                          |
| `CONTRIBUTING.md`                                                                                                                                                                    | Port + adapt                                                                                                                                             |
| Code patterns: `logging_config.py` (loguru+Rich), `version_check.py` (GitHub release check → UI banner), env-seed→persisted-config migration, PUID/PGID init, never-log-tokens rules | Reimplement in Shortlist shape                                                                                                                           |

### Deliberate deltas from MPG

| MPG                                  | Shortlist                         | Why                                                                                                                   |
| ------------------------------------ | --------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Flask 3 + Jinja2 + Flask-SocketIO    | **FastAPI + React SPA + SSE**     | Wizard-heavy, live-progress UX is SPA-shaped; typed OpenAPI for free; SSE is simpler than SocketIO and FastAPI-native |
| `settings.json` sole source of truth | **SQLite (SQLAlchemy + Alembic)** | Shortlist's state is relational (users×runs×picks×snapshots); settings live in a `settings` table                     |
| Auth token from container logs       | **Login with Plex (PIN)**         | Better UX; owner-only authorization comes free (account id must match server owner)                                   |
| Gunicorn gthread                     | **uvicorn**                       | FastAPI-native, async                                                                                                 |

---

## 2. Repo layout (`stevezau/shortlist`, fresh, MIT)

```
shortlist/
├── .claude/                      # ported chassis (see manifest)
│   ├── CLAUDE.md · settings.json
│   ├── rules/ (python, testing, commenting, docker, docs, shell, frontend, plex-safety)
│   └── agents/architecture-review.md
├── .github/                      # ci.yml, dockerhub.yml, dependabot.yml, FUNDING.yml, issue/PR templates, dockerhub-overview.md
├── shortlist/                       # Python package (backend + engine)
│   ├── engine/                   # PURE library — zero FastAPI/DB imports; talks to clients only
│   │   ├── pipeline.py           # per-user stage orchestration (history→candidates→filter→rank→deliver→privacy)
│   │   ├── models.py · context.py # dataclasses (Seed, Candidate, Pick, RunReport…) and the run context
│   │   ├── history.py            # per-user watched set read from the PMS with each share's token; seed derivation
│   │   ├── candidates.py         # TMDB similar/discover, Trakt and AI web-search pooling + seed tagging
│   │   ├── ranking.py · picker.py # scoring, and the picks with the plain-English reason written in code
│   │   ├── rows.py · limits.py   # row kinds, per-row settings, library limits
│   │   ├── seasons.py · themes.py · over_time.py  # seasonal rows, AI-row themes, refresh share and repeat cooldown
│   │   ├── requests.py · requests_row.py · request_*.py  # Radarr/Sonarr/Seerr requests and the "Your requests" row
│   │   ├── curator/              # LLM providers behind a protocol: anthropic, openai, openai_compatible, google, null
│   │   ├── delivery.py           # collection upsert, custom sort, label, poster, visibility promote
│   │   ├── placeholders.py       # row-name placeholders ({user}, {top_seed}, {season}…)
│   │   ├── privacy.py            # filter parse/merge/serialize, snapshot, diff, throttled apply
│   │   └── clients/              # plex_pms.py, plextv.py, tmdb.py, tautulli.py, arr.py, seerr.py, trakt.py, mdblist.py, search.py, poster.py
│   ├── server/                   # FastAPI app
│   │   ├── main.py               # app factory; serves web/dist; /api mount; healthz
│   │   ├── auth.py               # PIN flow, owner-only session, signed httpOnly cookie
│   │   ├── db/                   # SQLAlchemy models, session, alembic/
│   │   ├── api/                  # routers: setup, users, runs, collections (rows), settings, system, privacy, events (SSE)…
│   │   ├── assistant/ · assistant_auth/  # optional MCP assistant access (#141): tools, owner-approved connections, OAuth, budgets
│   │   ├── scheduler.py          # APScheduler; run rows are the durable queue (resume on restart)
│   │   ├── services/             # run_service (engine adapter + SSE emit), jobs, watch_* (live watch tracking), secrets (Fernet @ /config/secret.key)
│   │   └── settings_store.py     # typed settings table access; env-var seeding on first boot
│   └── logging_config.py         # loguru + Rich (ported)
├── web/                          # React 19 + Vite + TypeScript + Tailwind + shadcn/ui
│   └── src/
│       ├── pages/                # one file per screen; setup/ is the 7-step wizard (steps 0–6, see design doc §3)
│       ├── components/           # shadcn + shared and per-screen components
│       ├── lib/                  # api.ts, api-schema.d.ts (generated from OpenAPI), sse.ts, format, row-kinds
│       └── test/                 # vitest + testing-library
├── tests/
│   ├── conftest.py               # mock_plex, mock_plextv, mock_tmdb, mock_curator fixtures (MPG discipline: ALL external I/O mocked)
│   ├── unit/ · integration/
│   ├── fakes/fake_plex.py        # FastAPI stub emulating PMS+plex.tv endpoints Shortlist touches → enables full-wizard e2e with NO real server
│   └── e2e/                      # Playwright vs an in-process app (uvicorn + built SPA) + fake_plex
├── docs/                         # hub: README, getting-started, guides, reference, faq (MPG structure)
├── unraid-templates/
├── Dockerfile                    # multi-stage: node:22 build web → python:3.12-slim runtime; PUID/PGID init; HEALTHCHECK
├── docker-compose.example.yml
├── pyproject.toml                # ruff config, pytest config (cov target 80%), hatchling
└── README.md · CONTRIBUTING.md · LICENSE(MIT)
```

A row is one shape: one `collections` table and one `RowPolicy`, not a class hierarchy of row types. What
a row "is" (seasonal, watch-it-again, requests…) is worked out from its fields (`web/src/lib/row-kinds.ts`
on the UI side), so there is no stored kind to drift from the settings.

**The contract that keeps this honest:** `shortlist/engine/` imports nothing from `shortlist/server/`.
Engine functions take plain config dataclasses + client instances and return report objects. The
FastAPI service is a thin adapter over the engine; its APScheduler fires the same engine run nightly,
so the scheduled build and a manual "Run now" run byte-identical logic.

---

## 3. Data model (SQLAlchemy, SQLite at `/config/shortlist.db`)

```
Columns live in `shortlist/server/db/models.py` (the assistant tables in `shortlist/server/assistant*/`);
this block is the purpose of each table, with the few columns whose meaning is not obvious.

settings              key TEXT PK · value JSON · updated_at            (typed access via settings_store)
server                id · machine_id · name · url · token_enc · version · owner_account_id · plex_pass BOOL · capabilities JSON
users                 one row per Plex account: identity (plex_account_id, username, nickname, slug, user_type
                      shared|managed|owner), state (enabled, paused, departed_at, removed_at, manage_sharing),
                      restriction_profile, the row `label` ("shortlist_<slug>"), request tags, and `prefs` JSON
                      (row_name_tpl, excluded_genres, paused, history_depth, blocked_seeds)
collections           one row per Shortlist ROW (the table keeps its old name): slug, name, build(per_person|shared),
                      audience, enabled, its OWN cron `schedule` ("" = manual only; there is no global one),
                      size, media, library_keys, name_template, placement, poster, candidate sources, seed and
                      refresh settings, season settings, request settings (`req_*`), AI-row settings
                      (theme_id, ai_paused, ai_tokens, explore/over-time controls). See `Collection` in models.py.
theme_history         id · collection_id FK · user_id FK · theme_id FK(SET NULL) · theme_name · state(current|next|past)
                      · started_at · due_at   ← which theme a row showed a person and which is queued; drives
                        Up next, Recent themes and the no-repeat window. Written by the `themes.rotate` job.
themes · seasons      AI-row themes and seasonal-row definitions (presets and custom rules)
collection_audience   collection_id FK · user_id FK          (a `subset` row's members)
collection_user_overrides  collection_id FK · user_id FK · muted BOOL · row_size · recent_count (1..25)
poster_assets         key PK ("upload:<collection_id>" | "gen:<prompt_hash>") · image BLOB · content_type · updated_at
deliveries            collection_slug · user_slug · library_key  (composite PK) · rating_key · title · season · updated_at
                      · summary_written · title_sort_written  (what Shortlist last wrote; NULL = nothing — a cleared
                        field hands back only a value Plex still holds exactly as written)
                      ← the DELIVERY LEDGER: which Plex collection is which row, for whom, in which
                        library. Written per delivery, read by every on-demand reconcile. Keyed by SLUG
                        not FK on purpose — the row it describes is usually the one being deleted.
                        It exists because a title cannot answer that question: a `{top_seed}` row
                        renders differently every run. See jobs-and-runs-design.md §13.
row_delivery_snapshots  confirmed row contents and audience as half-open intervals, independent of run retention
jobs                  id · kind · payload JSON · operation_id · effect_key · status(queued|running|done|failed)
                      · attempts · max_attempts · detail · error · result JSON · created_at · started_at · finished_at
                      ← the durable queue for maintenance that must not be lost. APScheduler is only
                        the trigger; this table is what survives a restart. See §5 of that doc.
                      The registered kinds are the `@handler` functions in `services/jobs.py`; `themes.rotate`
                        (schedule setting `themes.rotate_cron`, daily by default) moves each Explore person to
                        their next theme and writes the following one a day early; it changes nothing on Plex.
runs                  id · trigger(schedule|manual|wizard|resume|assistant) · started_at(QUEUED at) · began_at(engine
                      start; NULL = never ran) · finished_at · status(queued|running|ok|error|aborted) · dry_run BOOL · stats JSON
run_users             run_id FK · user_id FK · status · error · reason · duration_ms · llm_tokens · llm_tokens_by_step
                      · exa_searches · rows_considered · cost JSON
#                                                    ^ exa_searches counts EVERY external web search (Exa or SearXNG);
#                                                      the column keeps its original name so historic runs read back
                      · diff JSON (added/removed/kept) · breakdown JSON (per row+library: titles, ratingKey, picks)
                      · trace JSON (per-user pipeline trace: seeds, per-source queries/returns, web-search+RAG prompts; {} when none)
run_shared_rows       the same per-row result for a SHARED row (one set delivered to N people)
run_log_lines         a run's activity feed, kept (narration; `events` stays the audit trail)
picks                 id · run_id FK · user_id FK · tmdb_id · media_type · title · year · rating_key · rank · reason · recipe
                      · seed_tmdb_id · seed_title · lead_seed_tmdb_id · lead_seed_title · collection_slug · section_key
                      · library · sources · affinity · built_at · created_at · watched_at NULL ← watched_at backfilled nightly = hit-rate
                      · finished_at NULL · max_percent NULL   ← finished = the stricter count; percent is FILMS ONLY
watch_events          id · plex_account_id · rating_key · show_rating_key · media_type · viewed_at · source · history_key · created_at
                      ← the PMS play log (`/status/sessions/history/all`), completions only; 6-month ceiling
watch_sessions        id · plex_account_id · session_key · rating_key · show_rating_key · media_type
                      · started_at · last_seen_at · ended_at · max_offset_ms · duration_ms · end_reason
                      ← live playback off the PMS notification socket; the ONLY source that sees a partial watch
watched_titles · watch_sync_state · watch_state_snapshots
                      each person's watched set cached per library, the incremental sync cursor, and the
                      pre-transfer snapshots that make a watching-account transfer undoable
shared_row_watches    user_id FK · collection_slug · tmdb_id · media_type (composite PK) · title
                      · watched_at NULL · finished_at NULL · max_percent NULL
                      ← shared rows write no `picks`, so their credits land here; folded into the same
                        person-title outcome by `resolve_outcomes`. Survives retention, like `picks`.
request_candidates    the Radarr/Sonarr/Seerr approval inbox: one row per wanted title with demand, `wanters`,
                      `why` JSON and status(pending|sent|rejected); the rest is in models.py
restriction_snapshots id · user_id FK · taken_at · reason(initial|sync|uninstall_restore) · filters_before JSON · filters_after JSON
caches                kind(tmdb|trakt|library_index|assistant_connection) · key · value JSON · expires_at
events                id · ts · level · scope · message JSON   ← audit trail surfaced in UI
assistant_*           the optional MCP assistant (#141): grants, local credentials, OAuth clients/codes/tokens,
                      consent flows, budgets, operations and their dispatch/call ledgers
```

Alembic from migration 0001 — never ship schema changes without one.

---

## 4. API surface (FastAPI, all under `/api`, OpenAPI auto-docs)

**[docs/reference/api.md](../../docs/reference/api.md) is the authoritative list** — it ships with the app and
is updated in the same PR as any endpoint change (`.claude/rules/docs.md`); `web/openapi.snapshot.json` is
the machine-readable one. This section is the architectural shape only; a second copy of ~150 endpoints in a
design doc drifts, and did.

```
/auth/*        PIN → token exchange, session, logout   (owner-only: account.id == server.owner_account_id)
/setup/*       capability probe + resumable wizard state
/users/*       roster, enable/pause/prefs, per-person row overrides, sync from plex.tv + Tautulli
/collections/* the multi-row surface: CRUD, audience, placement, posters, rename (SSE), cleanup
/runs/*        list/detail/trace/cancel, POST to run (optionally scoped to users and/or rows)
/requests/*    the approval inbox — send to Radarr/Sonarr, reject, restore
/settings/*    typed settings + per-service connection tests
/system/*      health · version · logs · libraries · backups · api-token · uninstall
               · jobs  ← the durable maintenance queue (GET history, POST to trigger the allow-listed kinds)
/events        SSE: run.progress, run.finished, run.user.stage, sync.progress, sync.finished, uninstall.progress
/picks, /report, /schedule, /seasons, /themes, /notifications, /privacy, /watching-account, /catalogs, /ai
               the read models and settings behind the other screens
/support/*     the issue helper (diagnostic bundle, redacted)
/assistant/*   optional MCP assistant access: grants, OAuth, consent (off unless SHORTLIST_MCP_URL is set)
```

Two rules that are not obvious from the routes:

- **Mutations that change who can SEE what queue a job rather than writing Plex inline** — see
  `jobs-and-runs-design.md` §12. A handler that returns 200 having only written the database is the
  bug class that doc exists to close.
- **`POST /system/jobs` takes an allow-list, not the handler registry.** `user.cleanup` deletes a
  person's rows and `user.restore` makes rows visible; neither may be reachable from a generic button.

Security: session cookie (signed, httpOnly, SameSite=Lax), CSRF token on mutations, admin Plex
token encrypted at rest (Fernet, key file `/config/secret.key`, chmod 600), tokens never logged
(ported MPG rule), rate-limit on /auth. `X-Api-Key` header alternative for automation (Settings →
API), same as the *arr convention.

---

## 5. Runtime & packaging

- **One container.** uvicorn serves API + built SPA. APScheduler in-process; scheduled + manual runs
  insert a `runs` row first, so a container restart resumes cleanly (idempotent stages, per-user
  transactionality).
- **Volumes/env:** `/config` (db, secret key, logs, posters). Env: `PORT`, `TZ`, `PUID/PGID`,
  `APP_BASE_PATH` (subpath support), optional seed vars (`PLEX_URL`, `TAUTULLI_URL`, …) migrated
  into settings on first boot then ignored (MPG's proven pattern).
- **Images:** GHCR primary + Docker Hub mirror; `latest` and `X.Y.Z` on a `v*` tag, `dev` on every `dev`
  push, no PR previews. Multi-arch amd64/arm64. HEALTHCHECK → `/api/system/health`.
- **The maintainer's deployment:** the `dev` tag, recreated by an image-update watcher.

---

## 6. Testing strategy (MPG discipline, adapted)

| Layer         | Tooling                                                                                      | Rules                                                                                                                                                                                                                                                                          |
| ------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Engine unit   | pytest, `-n auto`, cov ≥ 80%                                                                 | ALL external I/O mocked via conftest fixtures; recorded real plex.tv/PMS XML+JSON as fixture files                                                                                                                                                                             |
| Privacy logic | dedicated suite                                                                              | filter parse/merge round-trips property-tested (hypothesis); snapshot/restore invariants; **the merge code is the highest-consequence code in the repo — test it like money**                                                                                                  |
| Server        | pytest + httpx AsyncClient                                                                   | API contract tests against the OpenAPI schema                                                                                                                                                                                                                                  |
| Frontend      | vitest + testing-library                                                                     | wizard state machine fully unit-tested                                                                                                                                                                                                                                         |
| E2E           | Playwright vs the app in-process (uvicorn + built SPA) + `tests/fakes/fake_plex.py`          | full wizard → first run → dashboard, no real Plex needed; the built Docker image is exercised by `docker-smoke` (§7), not here |
| Live smoke    | A dry-run **Run now**, then a manual view-check from a non-owner account (rows stay private) | run against the maintainer's real server pre-release                                                                                                                                                                                                                                    |

`fake_plex.py` is a deliberate investment (~1,700 lines): stubs `/identity`, `/library/sections`,
`/status/sessions/history/all`, `/hubs`, collection CRUD, plus plex.tv `/api/v2/pins`, `/api/users`,
`/api/v2/home/users/switch`. It makes onboarding + privacy sync fully testable in CI — the thing no
competitor tests.

---

## 7. CI/CD

`.github/workflows/ci.yml` is the pipeline; `.github/workflows/dockerhub.yml` only syncs the Docker Hub
overview. The test jobs are `lint` (ruff, the migration-freeze check and the lockfile check), `docs` (the website
Jekyll build and link check), `test-python-shard` (a matrix) behind the `test-python` gate, `coverage`,
`test-web` (pnpm lint/vitest/build), and `e2e-shard` (a matrix, Playwright) behind the `e2e` gate. They all
run in parallel — `e2e` deliberately does NOT wait on `test-web`'s `web-dist`
artifact; it builds its own copy of the SPA so it can start at t=0 instead of queuing behind
`test-web`, trading one extra `vite build` for keeping that wait off the critical path.

`docker-smoke` is the only job that runs the actual IMAGE. Everything else tests the source — `e2e`
boots uvicorn in-process — so nothing else would notice the PUID/PGID drop failing, `web/dist`
landing where the app doesn't look, or a runtime package missing from the image. It builds
linux/amd64 with `load: true` (no push), boots the container, waits for the Dockerfile's own
HEALTHCHECK, then asserts `/` serves the SPA and that every provider SDK imports. Those last two
matter because `/api/system/health` is answered by Python and passes with no SPA in the image at
all, and because the providers are imported lazily — the container is healthy right up until
someone picks one, which is how `549631f` shipped.

`docker` (buildx, linux/amd64 + linux/arm64) waits on `lint`, `test-python`, `coverage`, `test-web`, `e2e` and
`docker-smoke`, and is the publish gate. On a `v*` tag the `release` job then creates the GitHub Release from
the matching `CHANGELOG.md` section, and fails the tag build when that section is missing.

The two image builds use **separate** `type=gha` cache scopes (`scope=smoke` / `scope=publish`), and
that is load-bearing rather than tidiness. Sharing the default key made the amd64-only smoke build
write `mode=max` over a cache holding both platforms, so every publish rebuilt arm64 from scratch
under QEMU — measured at 1m04s → 4m51s the day the smoke job landed. Note a scope change costs one
cold run before the saving shows up.

What runs, by event:

| Event         | Jobs                                     | Publishes                         |
| ------------- | ---------------------------------------- | --------------------------------- |
| push `dev`    | every test job, `docker-smoke`, `docker` | `:dev`                            |
| pull request  | every test job, `docker-smoke`           | nothing                           |
| push tag `v*` | all of the above, then `release`         | `:latest` + `:<version>` + `:dev` |

A push to `master` runs nothing: `master` only advances by a PR that already ran this workflow.

`docker` is gated on `github.event_name == 'push' && (ref == refs/heads/dev || ref starts with
refs/tags/v)`. The ref half is load-bearing, not defensive: `master` is a push trigger so the stable
branch gets CI, but every tag rule in `metadata-action` is gated on dev-or-tag, so a master push
reaching `build-push-action` would arrive with `push: true` and an **empty tag list**.

Concurrency: pull requests supersede their own older runs; pushes key the group on the SHA so they
run in parallel. Only `docker` serialises, via its own job-level group — two overlapping builds can
interleave a partial manifest push.

Both branches are protected: force-pushes and deletions are blocked on each, and `master`
additionally requires `lint`/`test-python`/`test-web`/`e2e` to pass, so it can only advance through
a green promotion PR. `dev` deliberately has no required checks — they would block the direct
pushes that are the normal way to work on it.

Releases are cut by hand: promote `dev` → `master` via PR, then tag `vX.Y.Z` on `master`.

---

## 8. `.claude/rules/plex-safety.md` (new, Shortlist-specific — the rule that matters)

1. Any code path that WRITES to Plex or plex.tv (collections, labels, visibility, share filters)
   must: (a) follow the leak-safe write ordering (deliver rows unpromoted → merge `label!=` excludes
   into other accounts → promote last), (b) snapshot before first mutation per user,
   (c) support `--dry-run`, (d) log a structured diff to `events`.
2. Share-filter writes are READ-MODIFY-WRITE merges. Never construct a filter string from scratch.
   Never touch conditions Shortlist didn't add.
3. plex.tv writes: adaptive throttle with 429 backoff, resume-safe (see `.claude/rules/plex-safety.md` rule 6).
4. The owner account is never restricted; managed-user restriction profiles are never modified.
5. Tokens: encrypted at rest, never logged, never in exceptions.
6. Every schema or filter-format assumption gets a recorded-fixture test from a real server response.

---

## 9. Execution phases (historical)

| Phase                                 | Scope                                                                                                                                                                       | Exit criteria                                                   |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| **0 — Gate + scaffold** (~1–2 d)      | Manual privacy test on the maintainer's server. `gh repo create stevezau/shortlist` + port MPG chassis (.claude, .github, pre-commit, docs skeleton, Dockerfile skeleton, pyproject) | Privacy test passes; CI green on empty skeleton                 |
| **1 — Engine + pilot** (~1 wk + soak) | `engine/` + `clients/` + unit suite. Runs nightly from a script on the maintainer's server. Rollout 5→15→40 users                                                                  | 1–2 wks nightly runs, zero privacy incidents, hit-rate baseline |
| **2 — Server + UI core** (~2 wks)     | FastAPI + DB + scheduler + SSE; dashboard/users/runs/settings                                                                                                               | The maintainer manages their instance via the UI, cron retired                 |
| **3 — Onboarding** (~1 wk)            | PIN auth, wizard (steps 0–6), uninstall/restore, fake_plex e2e                                                                                                                      | Clean-server `docker run` → rows with zero docs                 |
| **4 — Ship-ready** (~1 wk)            | README/docs/screenshots/GIF, Unraid template, issue templates, 3–5 external beta testers                                                                                    | Beta onboards unassisted                                        |
| **5 — Launch**                        | r/selfhosted + r/PleX posts, Awesome-Selfhosted PR                                                                                                                          | v1.0 public                                                     |

---

## 10. Decisions locked by this document

Stack (FastAPI/React/SQLite/SSE) · MPG chassis port list (§1) · engine/server import contract (§2) ·
DB schema v1 (§3) · API surface v1 (§4) · fake_plex e2e investment (§6) · plex-safety rules (§8).
Remaining open (Phase-1 picks): cadence default, acquisition default. Naming: **Shortlist** (verified
free 2026-07-12).
