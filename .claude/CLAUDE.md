# Rowarr

A private, AI-curated "Picked for You" row for every user on a Plex server. One Docker container:
FastAPI backend + React SPA + SQLite, with a pure-Python engine (Tautulli/Plex history → TMDB
similar-titles → LLM curate/explain → per-user Plex collection + label-restriction privacy).

**Status: beta.** Engine + CLI are in production on the maintainer's server (nightly cron);
FastAPI server, React SPA, automated Privacy Probe and Docker packaging are built. Read these
before any feature work:

- [.claude/docs/rowarr-design.md](docs/rowarr-design.md) — product/UX design (wizard, screens, engine, privacy system)
- [.claude/docs/rowarr-architecture.md](docs/rowarr-architecture.md) — repo layout, DB schema, API surface, testing, CI, phases

Personal deployment details (Steve's servers) live in `CLAUDE.local.md` — gitignored, never commit
it, and never leak environment-specific hostnames/IPs/paths into the public repo or docs.

## Commands

```bash
# Backend
pip install -e ".[dev]"
pytest                       # unit+integration, parallel, coverage (target ≥80%)
pytest -m e2e                # Playwright vs built image + tests/fakes/fake_plex.py
ruff check . --fix && ruff format .

# Frontend
pnpm -C web install
pnpm -C web dev              # Vite dev server (proxies /api to :5959)
pnpm -C web test             # vitest
pnpm -C web build

# Run (dev) — module-level `app` only exists when ROWARR_CONFIG is set
ROWARR_CONFIG=./devconfig uvicorn --factory rowarr.server.main:create_app --reload --port 5959

# CLI (same engine the server uses)
rowarr run [--user <slug>] [--dry-run] · rowarr verify [--probe] · rowarr uninstall

# Docker
docker build -t rowarr:dev .   # multi-stage: node web build → python runtime
```

## The write gate (both adapters)

Real (non-dry-run) writes are refused unless a passing Privacy Check is on record (≤7 days)
and the PMS is ≥ 1.43.2.10687. CLI: `privacy_check.json` written by `rowarr verify`.
Server: `privacy_checks` rows + `RunService._privacy_gate_error()`, sharing its
latest-per-tier definition with the dashboard badge (`server/services/privacy_state.py`).
Rows are delivered UNPROMOTED, all filters merged, and only then promoted — a new row is
never visible before the exclusions that hide it exist.

## Architecture (the one contract that matters)

```
rowarr/engine/    pure library — NO imports from rowarr/server/; takes config dataclasses + clients, returns reports
rowarr/server/    FastAPI + SQLAlchemy/Alembic + APScheduler + SSE; thin adapter over engine
rowarr/cli.py     second thin adapter over the same engine
web/              React + Vite + TS + Tailwind + shadcn/ui; API types generated from OpenAPI
tests/fakes/      fake_plex.py — stub PMS + plex.tv; e2e runs the full wizard with no real server
```

See rowarr-architecture.md §2–§4 for the full tree, DB schema, and API surface.

## Code style

- `ruff format` / `ruff check` (config in pyproject.toml), 120-char lines, 4-space indent
- Type hints on all params and returns; modern annotations (`list[str]`, not `typing.List`)
- Google-style docstrings on public APIs
- Logging: `from loguru import logger` — never stdlib `logging`
- Imports: stdlib → third-party → local
- Frontend rules: `.claude/rules/frontend.md`

## Conventions

- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `chore:`)
- **Before creating any commit, dispatch the Architecture Review agent**
  (`.claude/agents/architecture-review.md`) against the staged diff. Block on HIGH findings.
- Settings live in the DB (`settings` table via `settings_store`); env vars are one-time seeds
  migrated on first boot (infrastructure vars like `PORT`, `TZ`, `PUID/PGID`, `APP_BASE_PATH` stay live)
- Every schema change ships an Alembic migration
- Docs follow `.claude/rules/docs.md` — README/docs/reference updated in the same PR as the feature

## Security & Plex safety (non-negotiable)

`.claude/rules/plex-safety.md` governs every code path that writes to Plex or plex.tv. Highlights:
snapshot before restriction writes; share filters are read-modify-write MERGES, never rebuilt;
never touch collections/labels Rowarr didn't create (Kometa coexistence); owner never restricted;
tokens encrypted at rest, never logged; everything supports `--dry-run`.

## Key dependencies

Python ≥3.12 | FastAPI | SQLAlchemy 2 + Alembic | APScheduler | plexapi | httpx | loguru | Pydantic v2
| React 18 + Vite + TypeScript + Tailwind + shadcn/ui | pytest (+xdist, hypothesis) | Playwright
