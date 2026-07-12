# Changelog

All notable changes to this project are documented here. This project follows
[Conventional Commits](https://www.conventionalcommits.org/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Engine + CLI** — the full nightly pipeline: watch history (Tautulli, with a per-user
  fallback to Plex's own history) → TMDB similar/recommended candidates → heuristic pre-rank
  → optional LLM curation (Anthropic / OpenAI / Google / Ollama / none) → per-user collection
  delivery → merge-only share-filter privacy sync with snapshots.
- **Privacy Check** — T1 (filter read-back), T2 (canary's own Home hubs), and the full
  **Privacy Probe** (throwaway labeled collection, verified hidden for a canary, cleaned up
  in `finally`). `rowarr verify [--probe]`.
- **The write gate** — real writes are refused without a passing Privacy Check (≤7 days) on
  PMS ≥ 1.43.2.10687, in both the CLI and the server.
- **Web app** — FastAPI backend (SQLite + Alembic, APScheduler, SSE) and a React SPA:
  dashboard, users, runs with per-user diffs, settings, and a first-run onboarding wizard.
- **Login with Plex** — PIN flow, owner-only sessions, CSRF-protected mutations, tokens
  encrypted at rest and redacted in the UI.
- **Uninstall** — restores every user's share filters from the pre-Rowarr snapshot and
  deletes only rowarr-labeled collections; dry-run preview included.
- **Packaging** — multi-arch Docker image (GHCR), compose example, Unraid template,
  healthcheck, PUID/PGID.

### Notes

- Rows are delivered **unpromoted**, all share filters are merged, and only then are rows
  promoted — a new row is never visible before the exclusions that hide it exist.
- Collections without a `rowarr_*` label are never modified or deleted (Kometa coexistence).
