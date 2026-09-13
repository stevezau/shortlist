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
   promotes rows onto shared Home. Never promote a row before its excludes are merged.

   "Unpromoted" is NOT "invisible". Collection mode "hide" keeps a row out of library browse, but a
   shared account still sees it in the library's **Collections tab** until its exclude is on that
   account's filter (measured: `tests/fixtures/pms_collections_tab_filter_visibility.json`). Rows of
   people who already have one were excluded by an earlier merge; a person's FIRST row is not. So when a
   person with no row on the server before the run gets one, `_exclude_first_rows` merges their exclude
   into every other account from inside the same hold of the write lock that wrote the row (additive
   only), so no other row is written in between; (c) still runs at the end, reads every filter fresh, and
   gates promotion. A run with no
   users (`engine_run(ctx, [])`) still does the sweep + merge — it only ever makes the server more
   private, never creates or promotes.

2. **Snapshot first.** Before the first restriction mutation for a user, persist a
   `restriction_snapshots` row with their current filters. Uninstall restores from these.
3. **Merge, never rebuild.** Share-filter writes are read-modify-write: parse the user's current
   `filterMovies`/`filterTelevision`, add our `shortlist_*` excludes, leave every other condition
   byte-identical. Never construct a filter string from scratch.

   **Where they go is load-bearing (#116, owner decision 2026-09-13).** A real PMS reads `|` between
   conditions as OR and `&` as AND (`tests/fixtures/pms_share_filter_boolean_semantics.json`). So
   `contentRating!=R|label!=shortlist_x` hides nothing of ours and switches the owner's own rating
   exclude off — and plex.tv stores it perfectly, so every read-back passes. Our labels may only sit
   in a `label!=` clause with no `|` joining it or anything after it: fold into such a clause if one
   exists, otherwise append a new one with `&`. Never fold into a clause a `|` joins, and never append
   with `|`. The merge moves any of our labels it finds in such a position (that repair only ever
   hides more). It refuses any filter with a literal `&` inside one of the owner's labels — Plex itself
   fails that account's Home with HTTP 500 — and that account is REPORTED (`unreadable_filters`), not a
   promotion blocker: one label name must not take every other person's rows off Home (#14's shape).
   Presence is not proof: every "is this row hidden" check asks `unenforced_excludes`, never "is the
   label in the string".

   The same rule governs the one write that goes the other way. `users.manage_sharing=0` ("leave this
   account's Plex sharing alone", discussion #92) makes the run REMOVE our excludes from that one
   account rather than merge into it — `privacy.clear_our_excludes`, which takes out exactly the
   `shortlist_*` values and byte-preserves everything else, including the owner's own `label=`
   allow-list and their own `label!=` entries. It is the only write here that widens what an account
   can see, it is per-account and owner-chosen, and it never touches anyone else's filter, so every
   other account still excludes that person's row. It takes no snapshot: an account carrying our
   labels was already snapshotted before the first merge, and snapshotting now would capture our own
   pollution as if it were the user's original setting (rule 2).

4. **Touch only what we own.** Only collections titled/labeled by Shortlist (`shortlist_*` label) may be
   modified or deleted. Detect and skip anything else — Kometa and other tools manage collections
   on the same servers; coexistence is mandatory.

   **One bounded exception, and only this one: shelf POSITION** (owner decision 2026-09-12).
   `PlexClient.place_rows` rewrites the position of every promoted hub on a library's Recommended
   shelf, foreign hubs included, because Plex offers no way to place a row without doing so: the only
   two inserts that do not halve a float gap are "to the very top" and "after the hub currently last",
   and ~50 halvings exhaust double precision, after which Plex answers 200 to every move and applies
   none — for every client, including Plex Web. Measured on a real server: one top-rebuild collapsed
   72 of 94 hubs onto the single value `1000`. Arranging the shelf from the bottom is what avoids that,
   and it necessarily moves the backbone.

   The exception covers position and nothing else. A foreign collection's items, title, labels,
   artwork and promotion flags stay untouched, and foreign hubs keep their order **relative to each
   other** — they shift only as far as seating our rows among them requires. A row of ours whose
   anchor is unusable is treated as foreign for this purpose: left in place, not dropped. Anything
   beyond position needs its own decision; do not extend this by analogy.

   Every row also carries a constant `shortlist` label beside its `shortlist_<userslug>` one, so a
   co-managing tool can exclude all of ours with a single entry. It is ADDITIVE and names nobody.
   Everything that resolves an owner from a label matches `shortlist_` **with the underscore** — and
   that underscore is load-bearing: match on `shortlist` alone and the constant label yields an empty
   slug, which is in no roster, so every row on the server classifies as an ORPHAN and orphan
   handling is the one path here that deletes. Pinned by
   `test_pipeline.py::TestOrphanDeletion::test_the_constant_label_does_not_turn_a_live_row_into_an_orphan`.

   **An empty label read never authorises a delete.** A real PMS returns NO `<Label>` children in
   the collections listing (recorded: `tests/fixtures/pms_collections_listing.json`) — labels arrive
   only because plexapi silently re-reads each collection behind `collection.labels`. A re-read that
   FAILS raises; the dangerous case is one that SUCCEEDS carrying no `<Label>`, which is
   indistinguishable from a genuinely unlabelled row. Since every row carries the invisible title
   marker, and `delete_owned_collection` accepts the marker alone as proof of ownership, one empty
   read would delete every Shortlist row on the server and the run would still report success.

   Two guards, because one is not enough. Per collection: confirm with a fresh read
   (`PlexClient.confirm_unlabelled`) before deleting, and a read that fails means leave it alone. In
   aggregate: if rows of ours exist and NOT ONE reads as labelled, that is a failed read, not a
   server full of orphans — a systemic empty answer would pass the per-collection check by agreeing
   with itself.

5. **Owner + managed users.** The server owner is never restricted (Plex limitation — skip, don't
   error). Managed users' restriction _profiles_ (parental controls) are never modified by Shortlist.
6. **Throttle plex.tv adaptively.** Writes fire at a floor pace (`plextv.throttle_s`, default 0 = as
   fast as plex.tv accepts) and back off on a 429: the pace jumps to ≥1s, then doubles (capped 30s),
   and eases back toward the floor on each clean write. (Owner decision 2026-07-17: the old fixed
   ≤1 write/s was needlessly slow on a healthy server; the adaptive 429 backoff keeps it polite under
   load without pacing the happy path. Do NOT reinstate a hard 1/s floor.) Runs must be resume-safe
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
