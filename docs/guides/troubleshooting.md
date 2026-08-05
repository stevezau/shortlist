---
title: Troubleshooting and backups
description: The common failures and what causes them, plus what Shortlist backs up and what it can't restore.
heading: Troubleshooting and backups
nav_order: 7
---

## Start here: "Have an issue?"

Before working through the list below, open **Have an issue?** in the sidebar. It runs nineteen
read-only checks against your own server and, for most of these problems, tells you the answer
outright — which library refused someone's token, which setting actually applied, why a row is
short, whether Plex matches what Shortlist thinks it delivered.

Nothing on that page changes anything: not your Plex server, not your rows, not your settings.

Three things worth knowing:

- **The checks are off until you switch them on**, and they switch themselves off after 24 hours.
  They read share filters and per-user tokens, so they stay closed on an install that isn't
  currently debugging something.
- **Every check has a "Copy for support" button.** What it copies is shown on screen first, so
  there is nothing to be surprised by after pasting. It contains no passwords, tokens or API keys.
- **The last section files the report.** It opens a pre-filled GitHub issue and gives you the full
  diagnostic to attach — as a paste, or as a file when a chat app would truncate it.

If someone is reporting a problem on a server you don't administer, sending them there is usually
faster than a list of questions: _"open /issue, switch the checks on, type the title, press Copy."_

## Troubleshooting

- **A run says "skipped" and no collections were made** — a skip is always a configuration
  outcome, and the run page now says which one. The two common ones: _every enabled row is a
  **shared** row_, so there is no per-person row to build for anybody (add one under Rows), or a
  **shared row can't reach its threshold**. A shared row is built only from titles several people
  have watched, so it needs at least 2 enabled users with viewing in common and will skip forever
  below that. Make it a per-person row instead if you want one person to get it.
- **A user says they can see someone else's row** — run Shortlist again (Run now): every run
  re-merges the `label!=` exclusions into each account's share filters. Check whether the share
  was edited by hand in plex.tv (Shortlist re-merges but never deletes filter conditions it
  didn't add), and confirm Plex Media Server is ≥ 1.43.2.10687 (older builds ignore the exclusion).
- **Rows not appearing for anyone** — promoted rows land in Plex's hub order; users may
  need to scroll, or pin the row via "Manage Home Screen" on their client.
- **A watched title keeps getting recommended** — run the **"They keep seeing something they've
  watched"** check on the Have an issue? page; it names the cause. The three real ones, in order of
  how often they turn out to be it:
  1. _Shortlist has no watched record for that person at all_ — usually a library that has never
     been readable with their token, which looks identical to someone who watches nothing. The check
     says which library.
  2. _You're looking at the wrong account._ Watched state in Plex is per person, so a title ticked
     off on your account says nothing about theirs.
  3. _Timing._ The watched set is read per run, so a title marked watched after the last run stays
     eligible until the next one. **Jobs → Sync history** re-reads everyone's set immediately
     (writes nothing to Plex); any run after that drops it. Note also that a row only re-picks its
     titles on a refresh night — at the default freshness, roughly weekly — so a change can take
     until then to show. The **"When does each row next rebuild?"** check gives the date.

  Before 1.2 there was a fourth cause: a 0% row excluded only shows you had _finished_, so one you
  were two episodes into could come back as a suggestion. It no longer can. See
  [what "already watched" means for a show](../reference.md#what-already-watched-means-for-a-show).

- **Everything broke, get me out** — Settings → Danger Zone → **Uninstall** restores every
  user's share filters from the pre-Shortlist snapshots and deletes every shortlist-labeled
  collection. Kometa and other tools' collections are never touched.
- **Did anything drift out of sync?** — Settings → Danger Zone → **What Shortlist has on your
  Plex** ("Check Plex") lists every shortlist-labeled collection read straight from the server (not
  the database), flagging any whose user/row no longer exists in the app. Every collection is
  labeled at creation, in one step, so a collection that can't be labeled is deleted rather than left
  as an orphan, so a cleanup always finds them all; this is how you confirm it.

## Backups

Shortlist copies its whole database to `/config/backups` on a schedule (Jobs → Backups; nightly at
3 AM by default), before every upgrade, and before any restore. It keeps the newest 10 by default.

A backup holds everything Shortlist knows: settings and connections, your rows and their audiences,
the people it tracks, run history and each run's picks, the request inbox, and most importantly
the `restriction_snapshots` of each user's original Plex share filters.

Because a backup holds your rows' **audiences**, restoring one also restores who could see which
rows at that moment. If you have narrowed a shared row's audience since the backup was taken,
restoring widens it again and those people will see the row after the next run. Shortlist says so
before you confirm and again afterwards, but it does not undo it for you, so check Rows before
restarting. Those snapshots are the only
record of how your server's sharing looked before Shortlist touched it, and **Uninstall restores
from them**. Everything else is rebuildable by hand; that isn't.

Two things worth knowing:

- Backups sit beside the database in the `/config` volume, so they survive removing and recreating
  the container, but not losing the volume. Copy them off the host if that matters to you.
- `/config/secret.key` is **not** in a backup. It's the key your Plex token and AI keys are
  encrypted with, so restoring a database without that same file leaves those credentials unreadable
  and you'll have to re-enter them. Keep a copy of it alongside your backups.
