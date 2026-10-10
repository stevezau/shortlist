---
title: "Schedules: when Plex recommendation rows run"
description: Every row runs on its own schedule. How to set it, how to write a custom one, and which background jobs matter.
heading: Schedules and runs
---

## Schedules

**Activity → Jobs** lists everything on a timer. Each job carries its own next run on its line, and
opening one reveals its frequency picker; underneath, **Rows** lists the rows that build on a
schedule, grouped by the cron they share. Three rows on the same schedule are one trigger that
builds all three, not three timers. That list is read-only: a row's schedule is edited in the row
editor, so each setting has exactly one home.

It lives with Jobs rather than in its own nav entry because "what background work exists" and "when
does it run" are two views of one thing. As separate pages, every job was listed twice and neither
page could answer a whole question. `/schedule` still redirects here.

These jobs are worth knowing about there:

- **Sync watch history** re-reads every watched title in every library, every time it runs — and so
  does a run's own history top-up, so pressing Run now sees the same thing. Reading everything
  rather than only what changed is what catches a series you marked as watched by hand: Plex can
  leave a show's own date behind when its episodes move, so a "what's new since last time" read
  sorts it out of view and never sees it. Reading everything costs nothing measurable — 27.4s
  against 27.3s on a 47-user server — because Plex sends a whole page either way.

  **Un-marking something takes effect on the very next sync.** The title leaves that person's
  watched set, and any credit it was giving a pick is withdrawn with it. The one thing that stays
  deliberately rare is sweeping a library that has disappeared from your server: that acts on a
  single answer from Plex about everybody at once, so it runs weekly rather than nightly.
- **Privacy sync** runs every 30 minutes by default. It re-merges every account's share filter and
  builds, delivers and promotes nothing. So it can only ever make your server _more_ private. It reads
  the list of accounts from Plex each time, so someone you have just shared your server with stops
  seeing other people's rows within half an hour. A scheduled pass that changes nothing takes seconds
  and is kept out of the **Recent** list; one that fails, changes something, or that a setting you
  changed started still shows there. It is the cheapest safety net against drift.
- **Check and fix rows on Plex** runs nightly at **05:45**, after the rows build and after the privacy pass, so it
  checks the state those actually left behind. Drift is the failure nobody notices: a row left on the
  wrong shelf stays there until somebody happens to look, so the thing that repairs it is on by
  default. It is also the **only** schedule you can switch off completely, because it _writes corrections_
  to Plex, so its frequency picker offers **Off** where every other job offers **Daily**, and Off
  means off rather than "fall back to the default" the way every other blank cron does. **Check now**
  still works by hand with the schedule off.
- **Show and hide rows for today** runs at midnight by default (`rows.visibility_cron`), and again
  whenever you change which days or seasons a row appears in. It puts each row on or off Plex's shelves
  to match its day schedule and seasons. A hidden row keeps its titles, so it comes straight back
  without being built again, and everybody's privacy filters are re-merged before anything is shown.
- **Pick new row themes** runs daily at 01:30 by default (`themes.rotate_cron`). It gives each person
  on an AI row set to Explore their next theme, and changes nothing on Plex. Like **Check and fix rows
  on Plex**, its frequency picker offers **Off**. See [AI and cost](ai.md).

**Every row runs on its own schedule**. There is no single server-wide one. Open a row (Rows → edit)
and set its **Schedule**: **Nightly** or **Weekly** presets (just pick a run time), **Custom** for
anything else, or **Off** to only run that row by hand. New rows default to nightly at 03:30 server-local;
on upgrade, existing rows keep whatever your old global schedule was. Rows that share a cron run
together. To skip a person entirely, pause them on their detail page.

**A row run is never skipped for starting late.** If Shortlist is busy at the moment a row is due, the run
starts when it can. The other timers on Jobs are different: one that starts more than 30 seconds late is
skipped until its next time, and the bell says "A scheduled job didn't run". A run that was due while
Shortlist was stopped is not replayed when it starts again.

**A scheduled run cut short by a restart is finished once.** If the container restarts part-way
through a scheduled run (an auto-updater such as Watchtower replacing it, a host reboot), Shortlist
starts a run as soon as it is back up for the people that run never reached, on the same rows. It
shows on Runs as **Resumed after a restart**. It happens once: a resumed run that is itself cut short
is not resumed again, and neither is a run you started by hand or one more than 20 hours old. Shared
rows rebuild on the next full run. To avoid the interruption in the first place, schedule your
auto-updater for after your rows finish.

### Writing a custom schedule

Every **Custom** schedule box in Shortlist (a row's schedule, and the watch-history, user-sync and
backup pickers on Jobs) takes either form:

- **Plain English**: `every 30 minutes`, `every 4 hours`, `every 4 hours at 17 past`, `hourly`,
  `nightly at 3:30am`, `daily at 21:15`, `mondays at 9pm`, `weekdays at 6am`, `weekends at 10am`.
- **A cron expression**, if you already think that way: five fields: minute, hour, day-of-month,
  month, day-of-week. `0 */6 * * *` is every six hours; `0 4 * * 1` is Mondays at 4am; `0 4 * * sat-sun`
  is weekends at 4am. As in standard cron, when you set both a day of the month and a day of the week,
  it runs on either: `0 4 1 * 1` is every Monday and the 1st of every month.

Whichever you type, the line underneath tells you what it will actually do and what gets saved, and
nothing saves until it parses. So a typo can't quietly leave you on the built-in default. Times are
the server's, not your browser's.
