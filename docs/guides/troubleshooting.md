---
title: Troubleshooting and backups
description: The common failures and what causes them, plus what Shortlist backs up and what it can't restore.
heading: Troubleshooting and backups
nav_order: 7
---

## Troubleshooting

- **A run says "skipped" and no collections were made** — a skip is always a configuration
  outcome, and the run page now says which one. The two common ones: _every enabled row is a
  **shared** row_, so there is no per-person row to build for anybody (add one under Rows), or a
  **shared row can't reach its threshold** — a shared row is built only from titles several people
  have watched, so it needs at least 2 enabled users with viewing in common and will skip forever
  below that. Make it a per-person row instead if you want one person to get it.
- **A user says they can see someone else's row** — run Shortlist again (Run now): every run
  re-merges the `label!=` exclusions into each account's share filters. Check whether the share
  was edited by hand in plex.tv (Shortlist re-merges but never deletes filter conditions it
  didn't add), and confirm Plex Media Server is ≥ 1.43.2.10687 (older builds ignore the exclusion).
- **Rows not appearing for anyone** — promoted rows land in Plex's hub order; users may
  need to scroll, or pin the row via "Manage Home Screen" on their client.
- **A watched title keeps getting recommended** — the watched set is read per run, so a title you
  mark watched _after_ the last run stays eligible until the next one. **Jobs → Sync history**
  re-reads everyone's watched set immediately (writes nothing to Plex); any run after that drops it.
- **Everything broke, get me out** — Settings → Danger Zone → **Uninstall** restores every
  user's share filters from the pre-Shortlist snapshots and deletes every shortlist-labeled
  collection. Kometa and other tools' collections are never touched.
- **Did anything drift out of sync?** — Settings → Danger Zone → **What Shortlist has on your
  Plex** ("Check Plex") lists every shortlist-labeled collection read straight from the server (not
  the database), flagging any whose user/row no longer exists in the app. Every collection is
  labeled at creation (in one step — a collection that can't be labeled is deleted rather than left
  as an orphan), so a cleanup always finds them all; this is how you confirm it.

## Backups

Shortlist copies its whole database to `/config/backups` on a schedule (Jobs → Backups; nightly at
3 AM by default), before every upgrade, and before any restore. It keeps the newest 10 by default.

A backup holds everything Shortlist knows: settings and connections, your rows and their audiences,
the people it tracks, run history and each run's picks, the request inbox — and, most importantly,
the `restriction_snapshots` of each user's original Plex share filters.

Because a backup holds your rows' **audiences**, restoring one also restores who could see which
rows at that moment. If you have narrowed a shared row's audience since the backup was taken,
restoring widens it again and those people will see the row after the next run. Shortlist says so
before you confirm and again afterwards, but it does not undo it for you — check Rows before
restarting. Those snapshots are the only
record of how your server's sharing looked before Shortlist touched it, and **Uninstall restores
from them**. Everything else is rebuildable by hand; that isn't.

Two things worth knowing:

- Backups sit beside the database in the `/config` volume, so they survive removing and recreating
  the container — but not losing the volume. Copy them off the host if that matters to you.
- `/config/secret.key` is **not** in a backup. It's the key your Plex token and AI keys are
  encrypted with, so restoring a database without that same file leaves those credentials unreadable
  and you'll have to re-enter them. Keep a copy of it alongside your backups.
