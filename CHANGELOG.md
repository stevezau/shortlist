# Changelog

All notable changes to this project are documented here. This project follows
[Conventional Commits](https://www.conventionalcommits.org/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Engine** — the full nightly pipeline: watch history (Tautulli, with a per-user
  fallback to Plex's own history) → TMDB similar/recommended candidates → heuristic pre-rank
  → optional LLM curation (Anthropic / OpenAI / Google / Ollama / none) → per-user collection
  delivery → merge-only share-filter privacy sync with snapshots.
- **Leak-safe row privacy** — each row's collection is labelled `shortlist_<userslug>`, and a
  `label!=shortlist_<userslug>` exclusion is merged (read-modify-write, never rebuilt) into every
  other account's share filter. Rows Plex cannot hide (wrong media type for their library) are
  swept away first, rows are delivered **unpromoted**, all the exclusions are merged, and only
  then are rows promoted onto Home — so a row is never visible before the exclusion that hides it
  exists.
- **Web app** — FastAPI backend (SQLite + Alembic, APScheduler, SSE) and a React SPA:
  dashboard, users, runs with per-user diffs, settings, and a first-run onboarding wizard.
- **Login with Plex** — PIN flow, owner-only sessions, CSRF-protected mutations, tokens
  encrypted at rest and redacted in the UI.
- **Requests** — an approval inbox for wanted-but-missing titles, optionally auto-sent to
  Sonarr/Radarr. Each entry shows its full provenance — which person and row wanted it and why (the
  seed behind it, "because they watched …") — and a **Sent** log records what went out, when, and the
  app's answer. Rejected titles are never re-queued; per-user + per-row tags apply on send.
- **Uninstall** — restores every user's share filters from the snapshot taken before Shortlist's
  first restriction write and deletes only shortlist-labeled collections; dry-run preview included.
- **Packaging** — multi-arch Docker image (GHCR), compose example, Unraid template,
  healthcheck, PUID/PGID.

### Notes

- The label-based share exclusions require PMS **≥ 1.43.2.10687** (older builds ignore the
  exclusion). The setup wizard shows the server version but never blocks a run over it.
- Collections without a `shortlist_*` label are never modified or deleted (Kometa coexistence).
