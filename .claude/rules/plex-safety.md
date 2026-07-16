---
globs: "shortlist/engine/{privacy,delivery,verify}*.py,shortlist/engine/clients/plex*.py"
---

# Plex Safety Rules (non-negotiable)

Shortlist modifies other people's Plex views and share permissions. These rules govern every code
path that WRITES to a Plex server or plex.tv. The Architecture Review agent blocks commits that
violate them.

1. **Privacy gate.** No real collection/label/visibility/filter write happens unless the instance
   has a passing Privacy Check recorded (`privacy_checks` row). Three bounded exceptions, and only
   three:
   - **Probes** — the Privacy Check itself.
   - **The remedy pass** (`engine_run(ctx, [])` — the unhidable-row sweep plus merge-only exclude
     writes). The remedy runs precisely BECAUSE the gate is closed: a missing exclude, or a row Plex
     cannot hide, is what fails the check — so a gate that blocked the fix would block the only thing
     that can reopen it, and the leak would be permanent. The remedy may never create a collection,
     promote one, or remove an exclude; it may only make the server more private.
   - **Privacy-neutral reconciles** — an on-demand config-change cleanup that only ever makes the
     server _more_ private (deleting a stale collection when a row/user/audience is removed) or is
     _visibility-invariant_ (retitling a collection in place with `editTitle`, preserving its label).
     These cannot leak: a removal can only reduce what's visible, and a rename changes only the human
     title while the `label!=shortlist_<userslug>` exclusion that hides the row — keyed on the LABEL —
     is untouched. Such a reconcile may **never** create a collection, promote one, remove/alter an
     exclude or share filter, or change a collection's label; it may only delete an owned collection
     or retitle one in place. Anything else stays behind the gate.

   Anything not covered by these three stays behind the gate.

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
7. **Probes clean up in `finally`.** Privacy Check artifacts (probe collection, canary filter
   change) are removed/restored even when the check fails or raises.
8. **Dry-run everywhere.** Every write path takes `dry_run` and logs the would-be diff instead.
9. **Secrets.** Plex tokens and LLM keys: encrypted at rest (Fernet, `/config/secret.key`), never
   logged, never in exception messages, redacted in the UI after save.
10. **Audit everything.** Every write (real or dry-run) emits a structured `events` row with the
    diff — "what changed on whose share at 03:31" must always be answerable from the UI.
11. **Fixture-backed assumptions.** Any new assumption about PMS/plex.tv response shapes gets a
    recorded fixture in `tests/fixtures/` from a real server response.
