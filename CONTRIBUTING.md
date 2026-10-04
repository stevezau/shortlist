# Contributing to Shortlist

Thanks for considering it. Shortlist is a small, safety-critical codebase — it modifies other
people's Plex views — so the bar for write-path changes is deliberately high.

## Dev setup

```bash
# Backend (the lock first, so you get the versions the image ships)
pip install -r requirements.lock && pip install -e ".[dev]"
pytest                            # unit + integration, parallel (no network, ever)
pytest tests/unit/test_foo.py     # while editing: just the file you're working on
pytest -m e2e                     # Playwright against an in-process app + fake Plex
ruff check . --fix && ruff format .

# Frontend
pnpm -C web install
pnpm -C web dev                   # Vite on :5173, proxies /api to :5959 (or SHORTLIST_API_PROXY)
pnpm -C web test && pnpm -C web build

# Run the app against a throwaway config in safe mode, on port 5960
bash scripts/devrun.sh
```

Run the whole suite once, when the work has settled, rather than after every edit. If you change
`pyproject.toml` dependencies, regenerate `requirements.lock` (the command is in `.claude/CLAUDE.md`).

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

- **`dev`** is the default branch. All work lands here, so open PRs against `dev`. Every push to
  `dev` runs the full CI suite and, once it's green, publishes the **`ghcr.io/stevezau/shortlist:dev`**
  image. That's the bleeding-edge build.
- **`master`** is the stable branch. It moves only by promoting `dev` → `master` via PR when cutting
  a release. Pushing to `master` builds nothing on its own.
- **Releases** are cut by pushing a semver tag (`vX.Y.Z`, or `vX.Y.Z-beta.N` for pre-releases). CI
  builds **`:latest`** + **`:X.Y.Z`** from the tag. The release commit bumps `shortlist/__init__.py` (and the OpenAPI snapshot) first.
- **Publish gate:** the image is only pushed after lint, tests (Python 3.12), the web build, and the
  Playwright e2e suite all pass in the same run — a red suite never ships an image.

Image tags: `:dev` (latest dev build) · `:latest` (latest stable release) · `:X.Y.Z` (pinned).

## Reporting bugs

Use the issue templates, or the **Have an issue?** page in the app, which opens a pre-filled bug
report with a secrets-free diagnostic. For anything privacy-related (a user saw a row that wasn't
theirs), please mark it clearly — those get fixed first, always.
