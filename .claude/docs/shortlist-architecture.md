# Shortlist — Architecture & Execution Plan

**Status:** ready to execute (gated on Phase 0 privacy test) · **Date:** 2026-07-12 ·
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
| `.claude/skills/release`                                                                                                                                                             | Port release skill                                                                                                                                       |
| `.claude/settings.json`                                                                                                                                                              | Port permission-allowlist pattern (+ pnpm/vitest/playwright allows, same `.env` denies)                                                                  |
| `.github/workflows/ci.yml`                                                                                                                                                           | Adapt: ruff + pytest/codecov jobs stay; add `web` job (pnpm lint/typecheck/vitest/build); docker buildx multi-arch publish                               |
| `.github/workflows/docker-pr.yml` + `docker-pr-cleanup.yml`                                                                                                                          | **Not ported.** PR preview images were dropped — `ci.yml` publishes on `dev` pushes and `v*` tags only. Revisit if per-PR pullable tags are wanted again |
| `.github/workflows/architecture-review.yml`                                                                                                                                          | **Not ported.** The Architecture Review runs as an on-demand agent, not a workflow — see the dispatch criteria in CLAUDE.md                              |
| `.github/ISSUE_TEMPLATE/`, `PULL_REQUEST_TEMPLATE.md`                                                                                                                                | Port                                                                                                                                                     |
| `.pre-commit-config.yaml`, `.codecov.yml`, `.gitattributes`, `.dockerignore`                                                                                                         | Port                                                                                                                                                     |
| `README.md` structure                                                                                                                                                                | Port the shape: shields (+ AI-Assisted badge), logo, About/Problem/Solution, screenshots table, Quick Start, docs-hub table                              |
| `docs/` hub (`README/getting-started/guides/reference/faq`)                                                                                                                          | Port structure                                                                                                                                           |
| `docker-compose.example.yml`, `unraid-templates/`                                                                                                                                    | Port patterns (Unraid = big homelab reach)                                                                                                               |
| `llms.txt`                                                                                                                                                                           | Port (AI-readable repo summary)                                                                                                                          |
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
│   ├── agents/architecture-review.md
│   └── skills/release/
├── .github/                      # ported: ci.yml, docker-pr(+cleanup).yml, architecture-review.yml, templates
├── shortlist/                       # Python package (backend + engine)
│   ├── engine/                   # PURE library — zero FastAPI/DB imports; talks to clients only
│   │   ├── pipeline.py           # per-user stage orchestration (history→candidates→filter→rank→curate→deliver→privacy)
│   │   ├── models.py             # dataclasses: Seed, Candidate, Pick, UserProfile, RunReport
│   │   ├── history.py            # HistorySource protocol; ShareTokenWatchSource (reads PMS per-user watched set), seed derivation
│   │   ├── candidates.py         # TMDB similar/recommended pooling + seed tagging
│   │   ├── ranking.py            # heuristic pre-rank (seed_freq × rating × recency)
│   │   ├── curator/              # LLM providers behind Curator protocol
│   │   │   ├── base.py           # curate(profile, candidates, k) -> [Pick]; strict JSON schema; validates output ⊆ input
│   │   │   ├── anthropic.py · openai.py · google.py · ollama.py · null.py (heuristic+template reasons)
│   │   ├── delivery.py           # collection upsert, custom sort, label, poster, visibility promote
│   │   ├── privacy.py            # filter parse/merge/serialize, snapshot, diff, throttled apply
│   │   ├── acquire.py            # Radarr/Sonarr/Seerr, capped
│   │   ├── posters.py            # PIL branded collection posters (3 templates)
│   │   └── clients/              # plex.py (plexapi + raw plex.tv: pins, users, filters, home-switch), tautulli.py, tmdb.py, arr.py
│   ├── server/                   # FastAPI app
│   │   ├── main.py               # app factory; serves web/dist; /api mount; healthz
│   │   ├── auth.py               # PIN flow, owner-only session, signed httpOnly cookie
│   │   ├── db/                   # SQLAlchemy models, session, alembic/
│   │   ├── api/                  # routers: auth, setup, users, runs, settings, system, events (SSE)
│   │   ├── scheduler.py          # APScheduler; run rows are the durable queue (resume on restart)
│   │   ├── services/             # run_service (engine adapter + SSE emit), snapshot_service, hit_rate, secrets (Fernet @ /config/secret.key)
│   │   └── settings_store.py     # typed settings table access; env-var seeding on first boot (MPG pattern)
│   └── logging_config.py         # loguru + Rich (ported)
├── web/                          # React 19 + Vite + TypeScript + Tailwind + shadcn/ui
│   └── src/
│       ├── features/wizard/      # steps 0–7 (see design doc §3), state machine, resumable
│       ├── features/dashboard/ · users/ · runs/ · settings/
│       ├── api/                  # typed client generated from OpenAPI (openapi-typescript)
│       ├── components/           # shadcn + PlexRowPreview, PosterGrid, LiveLog (SSE), CapabilityChecklist
│       └── lib/                  # sse.ts, theme, format
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
└── README.md · CONTRIBUTING.md · LICENSE(MIT) · llms.txt
```

**The contract that keeps this honest:** `shortlist/engine/` imports nothing from `shortlist/server/`.
Engine functions take plain config dataclasses + client instances and return report objects. The
FastAPI service is a thin adapter over the engine; its APScheduler fires the same engine run nightly,
so the scheduled build and a manual "Run now" run byte-identical logic.

---

## 3. Data model (SQLAlchemy, SQLite at `/config/shortlist.db`)

```
settings              key TEXT PK · value JSON · updated_at            (typed access via settings_store)
server                id · machine_id · name · url · token_enc · version · owner_account_id · plex_pass BOOL · capabilities JSON
users                 id · plex_account_id · username · slug · avatar_url · user_type(shared|managed|owner)
                      · enabled BOOL · cold_start BOOL · label ("shortlist_<slug>") · prefs JSON
                      (row_name_tpl, row_size, excluded_genres, max_rating, paused)
collections           id · slug · name · build(per_person|shared) · audience(everyone|subset) · enabled BOOL
                      · schedule (this row's OWN 5-field cron; "" = manual only — there is no global one)
                      · size · media(movie|show|both) · library_keys JSON · name_template · min_watchers
                      · placement / placement_friends (both|home|library|off) · pin_top BOOL · hub_anchor JSON
                      · poster JSON · candidate_sources JSON · watched_pct · freshness · recency · recent_count · max_seeds · pick_order
collection_audience   collection_id FK · user_id FK          (a `subset` row's members)
collection_user_overrides  collection_id FK · user_id FK · muted BOOL · row_size · history_depth
poster_assets         id · collection_id FK · kind(upload|preview) · bytes · created_at
deliveries            collection_slug · user_slug · library_key  (composite PK) · rating_key · title · updated_at
                      ← the DELIVERY LEDGER: which Plex collection is which row, for whom, in which
                        library. Written per delivery, read by every on-demand reconcile. Keyed by SLUG
                        not FK on purpose — the row it describes is usually the one being deleted.
                        It exists because a title cannot answer that question: a `{top_seed}` row
                        renders differently every run. See jobs-and-runs-design.md §13.
jobs                  id · kind · payload JSON · status(queued|running|done|failed) · attempts · max_attempts
                      · detail · error · result JSON · created_at · started_at · finished_at
                      ← the durable queue for maintenance that must not be lost. APScheduler is only
                        the trigger; this table is what survives a restart. See §5 of that doc.
runs                  id · trigger(schedule|manual|wizard) · started_at · finished_at · status · dry_run BOOL · stats JSON
run_users             run_id FK · user_id FK · status · error · reason · duration_ms · llm_tokens · exa_searches
                      · diff JSON (added/removed/kept) · breakdown JSON (per row+library: titles, ratingKey, picks)
                      · trace JSON (per-user pipeline trace: seeds, per-source queries/returns, web-search+RAG prompts; {} when none)
picks                 id · run_id FK · user_id FK · tmdb_id · rating_key · rank · reason · seed_tmdb_id · seed_title
                      · collection_slug · section_key · library · sources · affinity
                      · created_at · watched_at NULL          ← watched_at backfilled nightly = hit-rate
request_candidates    id · tmdb_id · media_type · title · year · imdb_id · poster_path · rating · demand
                      · status(waiting|sent|rejected) · why JSON · first_seen_run_id
restriction_snapshots id · user_id FK · taken_at · reason(initial|sync|uninstall_restore) · filters_before JSON · filters_after JSON
caches                kind(tmdb|trakt|library_index) · key · value JSON · expires_at
events                id · ts · level · scope · message JSON   ← audit trail surfaced in UI
```

Alembic from migration 0001 — never ship schema changes without one (MPG's `upgrade.py` lesson,
done relationally).

---

## 4. API surface (FastAPI, all under `/api`, OpenAPI auto-docs)

**[docs/reference.md](../../docs/reference.md) is the authoritative list** — it ships with the app and
is updated in the same PR as any endpoint change (`.claude/rules/docs.md`). This section is the
architectural shape only; a second copy of ~60 endpoints in a design doc drifts, and did.

```
/auth/*        PIN → token exchange, session, logout   (owner-only: account.id == server.owner_account_id)
/setup/*       capability probe + resumable wizard state
/users/*       roster, enable/pause/prefs, per-person row overrides, sync from plex.tv + Tautulli
/collections/* the multi-row surface: CRUD, audience, placement, posters, rename (SSE), cleanup
/runs/*        list/detail/trace/cancel, POST to run (optionally scoped to users and/or rows)
/requests/*    the approval inbox — send to Radarr/Sonarr, reject, restore
/settings/*    typed settings + per-service connection tests
/system/*      health · version · logs · libraries · backups · api-token · uninstall
               · jobs  ← the durable maintenance queue (GET history, POST to trigger the two safe kinds)
/events        SSE: run.progress, run.user.stage, sync.progress, version.update
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
- **Images:** GHCR primary + Docker Hub mirror; tags `latest`, `X.Y.Z`, `dev` (master),
  `pr-<n>` (PR previews, auto-cleaned). Multi-arch amd64/arm64. HEALTHCHECK → `/api/system/health`.
- **Steve's deployment:** the `dev` tag on the plex host (exactly how MPG runs there today as
  `stevezau/media_preview_generator:dev`).

---

## 6. Testing strategy (MPG discipline, adapted)

| Layer         | Tooling                                                                                      | Rules                                                                                                                                                                                                                                                                          |
| ------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Engine unit   | pytest, `-n auto`, cov ≥ 80%                                                                 | ALL external I/O mocked via conftest fixtures; recorded real plex.tv/PMS XML+JSON as fixture files                                                                                                                                                                             |
| Privacy logic | dedicated suite                                                                              | filter parse/merge round-trips property-tested (hypothesis); snapshot/restore invariants; **the merge code is the highest-consequence code in the repo — test it like money**                                                                                                  |
| Server        | pytest + httpx AsyncClient                                                                   | API contract tests against the OpenAPI schema                                                                                                                                                                                                                                  |
| Frontend      | vitest + testing-library                                                                     | wizard state machine fully unit-tested                                                                                                                                                                                                                                         |
| E2E           | Playwright vs the app in-process (uvicorn + built SPA) + `tests/fakes/fake_plex.py`          | full wizard → first run → dashboard, no real Plex needed; the built Docker image itself is untested — the `docker` CI job builds and pushes it but never runs it, so the PUID/PGID drop, the `HEALTHCHECK`, and `web/dist` landing where the app expects it are all unverified |
| Live smoke    | A dry-run **Run now**, then a manual view-check from a non-owner account (rows stay private) | run against Steve's real server pre-release                                                                                                                                                                                                                                    |

`fake_plex.py` is a deliberate investment (~300 lines): stubs `/identity`, `/library/sections`,
`/status/sessions/history/all`, `/hubs`, collection CRUD, plus plex.tv `/api/v2/pins`, `/api/users`,
`/api/v2/home/users/switch`. It makes onboarding + privacy sync fully testable in CI — the thing no
competitor tests.

---

## 7. CI/CD

One workflow, `.github/workflows/ci.yml`. Six jobs:

`lint (ruff)`, `test-python (pytest + codecov)`, `test-web (pnpm lint/vitest/build)` and `e2e`
(playwright) all run in parallel — `e2e` deliberately does NOT wait on `test-web`'s `web-dist`
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

`docker` (buildx, linux/amd64 + linux/arm64) waits on all five and is the publish gate.

The two image builds use **separate** `type=gha` cache scopes (`scope=smoke` / `scope=publish`), and
that is load-bearing rather than tidiness. Sharing the default key made the amd64-only smoke build
write `mode=max` over a cache holding both platforms, so every publish rebuilt arm64 from scratch
under QEMU — measured at 1m04s → 4m51s the day the smoke job landed. Note a scope change costs one
cold run before the saving shows up.

What runs, by event:

| Event         | Jobs                | Publishes                         |
| ------------- | ------------------- | --------------------------------- |
| push `dev`    | all six             | `:dev`                            |
| push `master` | the five test jobs  | nothing                           |
| pull request  | the five test jobs  | nothing                           |
| push tag `v*` | all six             | `:latest` + `:<version>` + `:dev` |

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
3. plex.tv writes: ≤1 req/s, exponential backoff on 429, resume-safe.
4. The owner account is never restricted; managed-user restriction profiles are never modified.
5. Tokens: encrypted at rest, never logged, never in exceptions.
6. Every schema or filter-format assumption gets a recorded-fixture test from a real server response.

---

## 9. Execution phases (updated with chassis port)

| Phase                                 | Scope                                                                                                                                                                       | Exit criteria                                                   |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| **0 — Gate + scaffold** (~1–2 d)      | Manual privacy test on Steve's server. `gh repo create stevezau/shortlist` + port MPG chassis (.claude, .github, pre-commit, docs skeleton, Dockerfile skeleton, pyproject) | Privacy test passes; CI green on empty skeleton                 |
| **1 — Engine + pilot** (~1 wk + soak) | `engine/` + `clients/` + unit suite. Runs nightly on plex host (`error_checker.sh`). Rollout 5→15→40 users                                                                  | 1–2 wks nightly runs, zero privacy incidents, hit-rate baseline |
| **2 — Server + UI core** (~2 wks)     | FastAPI + DB + scheduler + SSE; dashboard/users/runs/settings                                                                                                               | Steve manages his instance via UI, cron retired                 |
| **3 — Onboarding** (~1 wk)            | PIN auth, wizard 0–7, uninstall/restore, fake_plex e2e                                                                                                                      | Clean-server `docker run` → rows with zero docs                 |
| **4 — Ship-ready** (~1 wk)            | README/docs/screenshots/GIF, Unraid template, issue templates, 3–5 external beta testers                                                                                    | Beta onboards unassisted                                        |
| **5 — Launch**                        | r/selfhosted + r/PleX posts, Awesome-Selfhosted PR                                                                                                                          | v1.0 public                                                     |

---

## 10. Decisions locked by this document

Stack (FastAPI/React/SQLite/SSE) · MPG chassis port list (§1) · engine/server import contract (§2) ·
DB schema v1 (§3) · API surface v1 (§4) · fake_plex e2e investment (§6) · plex-safety rules (§8).
Remaining open (Phase-1 picks): cadence default, acquisition default. Naming: **Shortlist** (verified
free 2026-07-12).
