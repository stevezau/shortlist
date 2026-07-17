---
globs: "shortlist/engine/{privacy,delivery}*.py,shortlist/engine/clients/plex*.py"
---

# Plex Safety Rules (non-negotiable)

Shortlist modifies other people's Plex views and share permissions. These rules govern every code
path that WRITES to a Plex server or plex.tv. The Architecture Review agent blocks commits that
violate them.

> **Note (2026-07-16, owner decision):** the automatic _Privacy Check_ + write gate that used to
> verify hiding before each write was **removed** at the owner's request. Rows are still made private
> the same way — the share-filter excludes below — but nothing verifies it after the fact anymore.
> That makes the leak-safe **write ordering** (rule 1) the load-bearing guarantee: get it wrong and a
> row can be briefly visible to the wrong person with no check to catch it.

1. **Leak-safe write ordering.** A per-person row must never be visible to another user before the
   exclusion that hides it exists. Every run therefore: (a) sweeps rows Plex cannot hide (wrong type
   for their library) BEFORE anything else, (b) delivers all rows UNPROMOTED, (c) merges the
   `label!=shortlist_<userslug>` excludes into every account's share filter, and only THEN (d)
   promotes rows onto shared Home. Never promote a row before its excludes are merged. A run with no
   users (`engine_run(ctx, [])`) still does the sweep + merge — it only ever makes the server more
   private, never creates or promotes.

2. **Snapshot first.** Before the first restriction mutation for a user, persist a
   `restriction_snapshots` row with their current filters. Uninstall restores from these.
3. **Merge, never rebuild.** Share-filter writes are read-modify-write: parse the user's current
   `filterMovies`/`filterTelevision`, union our `shortlist_*` excludes into the existing `label!=`
   values, leave every other condition byte-identical. Never construct a filter string from scratch.
4. **Touch only what we own.** Only collections titled/labeled by Shortlist (`shortlist_*` label) may be
   modified or deleted. Detect and skip anything else — Kometa and other tools manage collections
   on the same servers; coexistence is mandatory.
5. **Owner + managed users.** The server owner is never restricted (Plex limitation — skip, don't
   error). Managed users' restriction _profiles_ (parental controls) are never modified by Shortlist.
6. **Throttle plex.tv.** ≤1 write/s with exponential backoff on 429; runs must be resume-safe
   (per-user transactionality — a crash mid-run never leaves a half-applied user).
7. **Scaffolding cleans up in `finally`.** If a code path ever creates a temporary artifact on a
   real server as scaffolding (a probe collection, a canary filter change), it must be
   removed/restored in a `finally`, even when the operation fails or raises — never leave
   scaffolding behind on someone's server.
8. **Dry-run everywhere.** Every write path takes `dry_run` and logs the would-be diff instead.
9. **Secrets.** Plex tokens and LLM keys: encrypted at rest (Fernet, `/config/secret.key`), never
   logged, never in exception messages, redacted in the UI after save.
10. **Audit everything.** Every write (real or dry-run) emits a structured `events` row with the
    diff — "what changed on whose share at 03:31" must always be answerable from the UI.
11. **Fixture-backed assumptions.** Any new assumption about PMS/plex.tv response shapes gets a
    recorded fixture in `tests/fixtures/` from a real server response.
