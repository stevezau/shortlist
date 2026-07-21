# Shortlist

A private, AI-curated "Picked for You" row for every user on a Plex server. One Docker container:
FastAPI backend + React SPA + SQLite, with a pure-Python engine (Tautulli/Plex history → TMDB
similar-titles → LLM curate/explain → per-user Plex collection + label-restriction privacy).

**Status: beta.** In production on the maintainer's server: the FastAPI server runs the engine on
its own nightly schedule (APScheduler), with the React SPA and Docker
packaging. Read these
before any feature work:

- [.claude/docs/shortlist-design.md](docs/shortlist-design.md) — product/UX design (wizard, screens, engine, privacy system)
- [.claude/docs/shortlist-architecture.md](docs/shortlist-architecture.md) — repo layout, DB schema, API surface, testing, CI, phases

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

# Run (dev) — module-level `app` only exists when SHORTLIST_CONFIG is set
SHORTLIST_CONFIG=./devconfig uvicorn --factory shortlist.server.main:create_app --reload --port 5959

# Docker
docker build -t shortlist:dev .   # multi-stage: node web build → python runtime
```

## Row privacy (leak-safe ordering)

Rows are made private by share-filter excludes: each account's `filterMovies`/`filterTelevision`
gets `label!=shortlist_<otheruser>` for every row that isn't theirs. The ordering is what keeps it
leak-safe — rows are delivered UNPROMOTED, all filters merged, and only then promoted, so a new row
is never visible before the exclusions that hide it exist.

The old automatic Privacy Check + write gate that _verified_ this before each write was **removed at
the owner's request** (2026-07-16). Writes are no longer gated on a recorded check; the hiding still
happens, but nothing verifies it after the fact — see `.claude/rules/plex-safety.md`.

## Architecture (the one contract that matters)

```
shortlist/engine/    pure library — NO imports from shortlist/server/; takes config dataclasses + clients, returns reports
shortlist/server/    FastAPI + SQLAlchemy/Alembic + APScheduler + SSE; the thin adapter over the engine
web/              React + Vite + TS + Tailwind + shadcn/ui; API types generated from OpenAPI
tests/fakes/      fake_plex.py — stub PMS + plex.tv; e2e runs the full wizard with no real server
```

See shortlist-architecture.md §2–§4 for the full tree, DB schema, and API surface.

## Code style

- `ruff format` / `ruff check` (config in pyproject.toml), 120-char lines, 4-space indent
- Type hints on all params and returns; modern annotations (`list[str]`, not `typing.List`)
- Google-style docstrings on public APIs
- Logging: `from loguru import logger` — never stdlib `logging`
- Imports: stdlib → third-party → local
- Frontend rules: `.claude/rules/frontend.md`

## Conventions

- **Branch model** (mirrors media_preview_generator): `dev` is the default/working branch — commit
  and push here; every green `dev` push publishes `ghcr.io/stevezau/shortlist:dev`. `master` is the
  stable branch, advanced only by promoting `dev` → `master` via PR at release time. Releases are cut
  by tagging `vX.Y.Z` (CI builds `:latest` + `:X.Y.Z`). Publishing is gated on lint+tests+e2e green.
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
never touch collections/labels Shortlist didn't create (Kometa coexistence); owner never restricted;
tokens encrypted at rest, never logged; everything supports `--dry-run`.

## Key dependencies

Python ≥3.12 | FastAPI | SQLAlchemy 2 + Alembic | APScheduler | plexapi | httpx | loguru | Pydantic v2
| React 18 + Vite + TypeScript + Tailwind + shadcn/ui | pytest (+xdist, hypothesis) | Playwright
