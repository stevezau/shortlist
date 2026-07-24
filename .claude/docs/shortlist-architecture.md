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

| Asset                                                                                                                                                                                | Action                                                                                                                                      |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `.claude/rules/{python,testing,commenting,docker,docs,shell}.md`                                                                                                                     | Port near-verbatim; add `frontend.md` (React/TS) + `plex-safety.md` (Shortlist-specific, §8)                                                |
| `.claude/CLAUDE.md`                                                                                                                                                                  | Rewrite content, keep the proven section structure (Commands / Architecture / Code Style / Conventions / Security / Test Fixtures)          |
| `.claude/agents/architecture-review.md`                                                                                                                                              | Port — pre-commit arch-review agent, blocking on HIGH findings (this caught 8 production-bug shapes in MPG; keep the discipline from day 1) |
| `.claude/skills/release`                                                                                                                                                             | Port release skill                                                                                                                          |
| `.claude/settings.json`                                                                                                                                                              | Port permission-allowlist pattern (+ pnpm/vitest/playwright allows, same `.env` denies)                                                     |
| `.github/workflows/ci.yml`                                                                                                                                                           | Adapt: ruff + pytest/codecov jobs stay; add `web` job (pnpm lint/typecheck/vitest/build); docker buildx multi-arch publish                  |
| `.github/workflows/docker-pr.yml` + `docker-pr-cleanup.yml`                                                                                                                          | Port — **PR preview images** (each PR gets a pullable tag; this is how Steve already tests MPG `:dev` on the plex host)                     |
| `.github/workflows/architecture-review.yml`                                                                                                                                          | Port                                                                                                                                        |
| `.github/ISSUE_TEMPLATE/`, `PULL_REQUEST_TEMPLATE.md`                                                                                                                                | Port                                                                                                                                        |
| `.pre-commit-config.yaml`, `.codecov.yml`, `.gitattributes`, `.dockerignore`                                                                                                         | Port                                                                                                                                        |
| `README.md` structure                                                                                                                                                                | Port the shape: shields (+ AI-Assisted badge), logo, About/Problem/Solution, screenshots table, Quick Start, docs-hub table                 |
| `docs/` hub (`README/getting-started/guides/reference/faq`)                                                                                                                          | Port structure                                                                                                                              |
| `docker-compose.example.yml`, `unraid-templates/`                                                                                                                                    | Port patterns (Unraid = big homelab reach)                                                                                                  |
| `llms.txt`                                                                                                                                                                           | Port (AI-readable repo summary)                                                                                                             |
| `CONTRIBUTING.md`                                                                                                                                                                    | Port + adapt                                                                                                                                |
| Code patterns: `logging_config.py` (loguru+Rich), `version_check.py` (GitHub release check → UI banner), env-seed→persisted-config migration, PUID/PGID init, never-log-tokens rules | Reimplement in Shortlist shape                                                                                                              |

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
├── web/                          # React 18 + Vite + TypeScript + Tailwind + shadcn/ui
│   └── src/
│       ├── features/wizard/      # steps 0–7 (see design doc §3), state machine, resumable
│       ├── features/dashboard/ · users/ · runs/ · settings/
│       ├── api/                  # typed client generated from OpenAPI (openapi-typescript)
│       ├── components/           # shadcn + PlexRowPreview, PosterGrid, LiveLog (SSE), CapabilityChecklist
│       └── lib/                  # sse.ts, theme, format
├── tests/
│   ├── conftest.py               # mock_plex, mock_tautulli, mock_tmdb, mock_curator fixtures (MPG discipline: ALL external I/O mocked)
│   ├── unit/ · integration/
│   ├── fakes/fake_plex.py        # FastAPI stub emulating PMS+plex.tv endpoints Shortlist touches → enables full-wizard e2e with NO real server
│   └── e2e/                      # Playwright vs built image + fake_plex
├── docs/                         # hub: README, getting-started, guides, reference, faq (MPG structure)
├── unraid-templates/
├── Dockerfile                    # multi-stage: node:22 build web → python:3.12-slim runtime; PUID/PGID init; HEALTHCHECK
├── docker-compose.example.yml
├── pyproject.toml                # ruff config, pytest config (cov target 80%), hatchling
├── Makefile                      # dev, test, lint, e2e, build
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
runs                  id · trigger(schedule|manual|wizard) · started_at · finished_at · status · dry_run BOOL · stats JSON
run_users             run_id FK · user_id FK · status · error · duration_ms · llm_tokens · diff JSON (added/removed/kept) · trace JSON (per-user pipeline trace: seeds, per-source queries/returns, web-search+RAG prompts; {} when none)
picks                 id · run_id FK · user_id FK · tmdb_id · rating_key · rank · reason · seed_tmdb_id · seed_title
                      · created_at · watched_at NULL          ← watched_at backfilled nightly = hit-rate
restriction_snapshots id · user_id FK · taken_at · reason(initial|sync|uninstall_restore) · filters_before JSON · filters_after JSON
caches                kind(tmdb|library_index) · key · value JSON · expires_at
events                id · ts · level · scope · message JSON   ← audit trail surfaced in UI
```

Alembic from migration 0001 — never ship schema changes without one (MPG's `upgrade.py` lesson,
done relationally).

---

## 4. API surface (FastAPI, all under `/api`, OpenAPI auto-docs)

```
POST /auth/pin                 create PIN → {id, code}          GET  /auth/pin/{id}    poll → token exchange
GET  /auth/session · POST /auth/logout                          (owner-only: account.id == server.owner_account_id)
POST /setup/probe              capability probe (version/pass/libraries/tautulli-detect)
GET/PUT /setup/state           wizard progress (resumable)
GET  /users                    list + badges (history depth, cold-start, managed-flag)
PATCH /users/{id}              enable/prefs
GET  /runs · GET /runs/{id}    list/detail (diffs, errors)      POST /runs {user_ids?, dry_run?} → run_id
GET  /events                   SSE stream: run.progress, run.user.stage, version.update
GET/PUT /settings              typed settings                   POST /settings/test/{plex|tautulli|llm|radarr|sonarr|seerr}
GET  /system/health · /system/version (+ GitHub release check)
POST /system/uninstall {confirm} → restore snapshots, delete collections/labels, report
```

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

| Layer         | Tooling                                                                                      | Rules                                                                                                                                                                         |
| ------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Engine unit   | pytest, `-n auto`, cov ≥ 80%                                                                 | ALL external I/O mocked via conftest fixtures; recorded real plex.tv/PMS XML+JSON as fixture files                                                                            |
| Privacy logic | dedicated suite                                                                              | filter parse/merge round-trips property-tested (hypothesis); snapshot/restore invariants; **the merge code is the highest-consequence code in the repo — test it like money** |
| Server        | pytest + httpx AsyncClient                                                                   | API contract tests against the OpenAPI schema                                                                                                                                 |
| Frontend      | vitest + testing-library                                                                     | wizard state machine fully unit-tested                                                                                                                                        |
| E2E           | Playwright vs built Docker image + `tests/fakes/fake_plex.py`                                | full wizard → first run → dashboard, no real Plex needed; CI-shardable (MPG's e2e sharding pattern)                                                                           |
| Live smoke    | A dry-run **Run now**, then a manual view-check from a non-owner account (rows stay private) | run against Steve's real server pre-release                                                                                                                                   |

`fake_plex.py` is a deliberate investment (~300 lines): stubs `/identity`, `/library/sections`,
`/status/sessions/history/all`, `/hubs`, collection CRUD, plus plex.tv `/api/v2/pins`, `/api/users`,
`/api/v2/home/users/switch`. It makes onboarding + privacy sync fully testable in CI — the thing no
competitor tests.

---

## 7. CI/CD (ported ci.yml shape)

`lint (ruff)` → `test-python (pytest+codecov)` → `test-web (pnpm typecheck/vitest/build)` →
`e2e (playwright, sharded)` → `docker (buildx multi-arch)` → publish by ref (`master→dev`,
`tag→latest+semver`, `PR→pr-<n>`); `architecture-review.yml` on PRs; `docker-pr-cleanup` on close.
Release via the ported `.claude/skills/release` skill: Conventional Commits → changelog → tag →
GitHub Release → images.

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
