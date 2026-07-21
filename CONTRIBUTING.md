# Contributing to Shortlist

Thanks for considering it! Shortlist is a small, safety-critical codebase — it modifies other
people's Plex views — so the bar for write-path changes is deliberately high.

## Dev setup

```bash
pip install -e ".[dev]"          # backend
pnpm -C web install               # frontend
pytest                            # unit + integration (no network, ever)
pnpm -C web test && pnpm -C web build
ruff check . --fix && ruff format .
SHORTLIST_CONFIG=./devconfig uvicorn --factory shortlist.server.main:create_app --reload --port 5959
pnpm -C web dev                   # Vite on :5173, proxies /api to :5959
```

## The rules that matter

1. **`shortlist/engine/` never imports from `shortlist/server/`.** The engine is a pure library.
2. **Read `.claude/rules/plex-safety.md` before touching any code path that writes to Plex
   or plex.tv.** Highlights: snapshot before restriction writes; share filters are
   read-modify-write merges, never rebuilt; only `shortlist_*`-labeled collections may be
   touched; every write path takes `dry_run`; tokens never in logs or exceptions.
3. **Tests are required.** No test may touch the network — use the conftest fixtures,
   recorded fixtures in `tests/fixtures/`, or `tests/fakes/fake_plex.py`. Privacy/merge code
   changes need property tests.
4. **Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
5. **Docs ship with the feature** — README/docs updated in the same PR.

## Branches & releases

- **`dev`** is the default branch — all work lands here (open PRs against `dev`). Every push to
  `dev` runs the full CI suite and, once it's green, publishes the **`ghcr.io/stevezau/shortlist:dev`**
  image. That's the bleeding-edge build.
- **`master`** is the stable branch. It moves only by promoting `dev` → `master` via PR when cutting
  a release. Pushing to `master` builds nothing on its own.
- **Releases** are cut by pushing a semver tag (`vX.Y.Z`, or `vX.Y.Z-beta.N` for pre-releases). CI
  builds **`:latest`** + **`:X.Y.Z`** from the tag. Bump `shortlist/__init__.py` first, then tag.
- **Publish gate:** the image is only pushed after lint, tests (3.12 + 3.13), the web build, and the
  Playwright e2e suite all pass in the same run — a red suite never ships an image.

Image tags: `:dev` (latest dev build) · `:latest` (latest stable release) · `:X.Y.Z` (pinned).

## Reporting bugs

Use the issue templates. For anything privacy-related (a user saw a row that wasn't
theirs), please mark it clearly — those get fixed first, always.
