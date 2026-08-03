---
title: Schedules — when rows rebuild and jobs run
description: Every row runs on its own schedule. How to set it, how to write a custom one, and which background jobs matter.
heading: Schedules and runs
nav_order: 4
---

## Schedules

**The Jobs page** lists everything on a timer. Each job carries its own next run on its line, and
opening one reveals its frequency picker; underneath, **Rows** lists the rows that build on a
schedule, grouped by the cron they share — three rows on the same schedule are one trigger that
builds all three, not three timers. That list is read-only: a row's schedule is edited in the row
editor, so each setting has exactly one home.

It lives with Jobs rather than in its own nav entry because "what background work exists" and "when
does it run" are two views of one thing — as separate pages, every job was listed twice and neither
page could answer a whole question. `/schedule` still redirects here.

Two jobs are worth knowing about there:

- **Sync watch history** reads only what changed since last night, then does a complete re-read
  weekly (it is the only thing that can notice a title being un-watched or removed). If a library
  cannot be read incrementally, that library falls back to a complete read on its own rather than
  serving a stale watched set.
- **Privacy sync** runs nightly (05:15 by default). It re-merges every account's share filter and
  builds, delivers and promotes nothing — so it can only ever make your server _more_ private. It is
  the cheapest safety net against drift.
- **Check and fix rows on Plex** runs nightly at **05:45** — after the rows build and after the privacy pass, so it
  checks the state those actually left behind. Drift is the failure nobody notices: a row left on the
  wrong shelf stays there until somebody happens to look, so the thing that repairs it is on by
  default. It is also the **only** schedule you can switch off completely — it _writes corrections_
  to Plex, so its frequency picker offers **Off** where every other job offers **Daily**, and Off
  means off rather than "fall back to the default" the way every other blank cron does. **Check now**
  still works by hand with the schedule off.

**Every row runs on its own schedule** — there is no single server-wide one. Open a row (Rows → edit)
and set its **Schedule**: **Nightly** or **Weekly** presets (just pick a run time), **Custom** for
anything else, or **Off** to only run that row by hand. New rows default to nightly at 03:30 server-local;
on upgrade, existing rows keep whatever your old global schedule was. Rows that share a cron run
together. To skip a person entirely, pause them on their detail page.

### Writing a custom schedule

Every **Custom** schedule box in Shortlist — a row's schedule, and the watch-history / user-sync /
backup pickers on Jobs — takes either form:

- **Plain English**: `every 30 minutes`, `every 4 hours`, `every 4 hours at 17 past`, `hourly`,
  `nightly at 3:30am`, `daily at 21:15`, `mondays at 9pm`, `weekdays at 6am`, `weekends at 10am`.
- **A cron expression**, if you already think that way: five fields — minute, hour, day-of-month,
  month, day-of-week. `0 */6 * * *` is every six hours; `0 4 * * 1` is Mondays at 4am.

Whichever you type, the line underneath tells you what it will actually do and what gets saved, and
nothing saves until it parses — so a typo can't quietly leave you on the built-in default. Times are
the server's, not your browser's.
