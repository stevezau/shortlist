---
title: A tour of the Shortlist web interface
description: What every page in the Shortlist web interface does, and what each number on the dashboard actually means.
heading: The web interface
nav_order: 1
---

Eight pages in the sidebar. This is what each one is for.

## Dashboard

The impact report: what Shortlist delivered versus what people actually watched, for a window you
choose (7 / 30 / 90 days, or all time — 30 by default). Each headline figure carries its change
against the previous equal period, so you can see direction rather than a running total. There's
also a **Sync watched now** button to refresh the numbers on demand.

[What each figure means](#reading-the-dashboard) is at the bottom of this page.

## Rows

Create, edit, and reorder your rows. Each card shows who sees it and how it differs from the
defaults (sources, libraries, freshness, placement). This is where the whole multi-row feature
lives — see [Rows and templates](rows.md).

## Users

Everyone the server is shared with, plus you (badged `owner` — plex.tv's user list leaves the owner
out, so Shortlist adds you itself).

### Keeping the list current

**Sync from Plex** pulls the roster again after you invite someone new, or to pick up your own owner
row on an install that predates it. If it finds somebody who no longer has access to the server,
Shortlist turns them off and cleans up their rows; their history is kept, so you can switch them
back on if they return.

### Turning people on and off

Enable or disable each person, or use **Enable all / Disable all** at once.

- **Off** removes their rows from Plex and rewrites the share filters so they stop seeing the shared
  rows too. Turning them back **on** undoes the second half straight away, and their own row returns
  on the next run.
- **Pause** keeps their row but skips them on runs. It takes their rows off every shelf, and
  **unpause** puts them straight back. Neither waits for a run.

All of this runs as background jobs, visible on the **Jobs** page.

### Per-person settings

Set a request tag, or add per-person row overrides: mute a row, resize it, or set its watch-history
depth just for them. Opening a person shows their recent watch history (distinct titles, with season
and episode numbers for TV), their picks grouped by row (long lists collapse behind a "show more"),
and a **Run now** button to rebuild just that person.

### Accounts Plex restricts

Accounts with a Plex **restriction profile** (Younger Kid / Older Kid / Teen) are badged with that
profile's name. Plex hides every collection from them, so no row is built — and Plex also refuses
the privacy filters Shortlist writes, so those accounts are left out of them.

Both go away by setting **Restriction Profile → None** in Plex → Settings → Users & Sharing. You can
still limit them by rating or label there, which Plex only permits once the profile is None. A Plex
Home account with **no** profile is an ordinary user: it gets a row and privacy filters like anybody
else.

## Runs

A live **Activity** log streams each user through history → candidates → ranking → delivering as the
run happens, seeded from the server so a reload replays it. Per-user diffs are grouped by row then
library ("added X to Movies, Y to TV Shows"), each library showing its own ranked picks. Errors are
first-class rows with copy-for-GitHub buttons, alongside LLM token usage.

### How we picked

Open a person and click **How we picked** to read the full pipeline for them as one flow, per
library:

1. The watch history and seeds it started from.
2. Every candidate source's query and what it returned, each title tagged with whether it made the
   shortlist or the plain reason it fell out (already watched, not in your libraries, lost the
   ranking cut).
3. **How the shortlist was ordered**. The plain-code score plus the two fair-share passes. No AI
   ranks.
4. What was finally delivered, and why.

The AI web-search card shows the exact Exa queries and the prompt the model searched from, and marks
each proposed title kept or dropped — or struck through when it resolved to no real match (a
hallucination). Long lists of returned titles expand in place.

A **cold-start** user, with too little history to search from, gets the same page, showing the
highest-rated titles pulled from the server as their fallback.

## Logs

What this instance has been doing, with a level filter (this level _and louder_), a text filter,
live follow, **Copy**, and **Download .zip** for attaching to a bug report.

Tokens, API keys and passwords are stripped out server-side before anything reaches the page or the
zip, so it's safe to share. The file keeps the last 10 × 10 MB and always records at DEBUG,
regardless of the console level in Settings → Advanced.

## Requests

The approval inbox for titles your picks wanted but the library doesn't have yet. Approve to send to
Radarr or Sonarr, or reject so they never come back. See [Requests](requests.md).

## Jobs

Every piece of background maintenance Shortlist does, in two areas.

### The Jobs list

One per line: the name, how the last run went, when the next one fires, and the button.

**Run now** holds the six you start yourself:

| Job                            | What it does                                    |
| ------------------------------ | ----------------------------------------------- |
| **Sync people from Plex**      | Pull the roster from plex.tv and Tautulli       |
| **Sync watch history**         | Re-read everyone's watched set                  |
| **Check and fix rows on Plex** | Preview, then fix, rows left on the wrong shelf |
| **Privacy sync**               | Re-merge every share filter                     |
| **Back up the database**       | Write a backup to `/config/backups`             |
| **Clear out old records**      | Drop run history past the limit you set         |

A tag on the line says what a job changes on your server: **Can delete** on the one that can remove
a collection, **Changes Plex** on the ones that write. Anything untagged only reads, or only touches
Shortlist's own records.

**Automatic** holds the ones Shortlist queues for itself when something changes — removing a
disabled person's rows, hiding a paused one's, tidying up after a row edit. Those have no button by
design: each one is aimed at a specific person or row by the action that queued it.

Open any job for its description, its settings (the frequency picker on the scheduled ones, the
backup retention and restore list), what the last run reported, and **Previous runs** — that job's
own history. Anything a run is doing right now, such as a progress bar or a drift preview and its
Fix button, stays visible on the line without opening it.

### Activity

Every job run across every kind, newest first, filterable by All / In flight / Failed. Open a row
for what it was asked to do, what came back, how long it took, and the error if it failed.

Anything that fails is retried with backoff and survives a container restart. If it finally gives
up, it reaches the notification bell.

### How long history is kept

Clearing run history lives on the Runs page. It clears the browsable history but preserves your
dashboard metrics (delivered, watched and hit rate survive indefinitely), and doesn't affect
Shortlist's ability to tidy up rows on Plex.

How long each of the two histories is kept is set in Settings → Advanced:

- **Runs kept** (three months by default) for the browsable run detail.
- **Change log kept** for the record of what Shortlist changed on Plex and in these settings. This
  defaults to **Forever**, because it is the only lasting answer to "what changed on whose account",
  so it outlives the runs around it.

Both are applied by the nightly **Clear out old records** job.

## Settings

One scrolling page, organised into a grouped sidebar sub-nav that jumps to each section and tracks
where you are:

- **Connect** — Connections
- **Rows** — Finding titles, Row defaults, Row placement
- **Add-ons** — Requests
- **System** — Advanced, API access, Danger Zone

Each section is walled off by a rule, and its own sub-headings sit a clear rank below the section
title. Every connection is re-testable in place.

Each row's run schedule lives in that row's editor, not here. See [Schedules](schedules.md).

## Reading the dashboard

Everything on the dashboard is scoped to the window selected at the top — **the last 30 days** by
default. That matters more than it sounds: these figures used to be lifetime totals, which made
every ratio a measure of how long Shortlist had been installed rather than of how good the picks
were. A pick can only ever be credited as watched within **30 days** of being delivered, but the old
denominator kept every pick ever delivered, for ever — so each night added ~60 permanently
uncreditable picks per person to the bottom of the fraction and the number could only sink.

**Watched** — picks people watched in the window. A pick delivered last month and watched this week
counts here: this figure is about watching, not delivery.

**People watching** — how many people watched at least one pick, out of everyone currently enabled.

**Avg to watch** — average days from a title first being recommended to it first being watched, over
titles first watched in the window. Lower is better, and the change arrow is coloured accordingly.

**Landing rate** — the one percentage, and the only one computed carefully enough to trust. It is the
share of picks watched within 30 days of delivery, measured over a **settled group**: picks
delivered in the window _and_ at least 30 days ago. A pick delivered yesterday cannot have been
"watched within 30 days" yet, so counting it would drag the rate toward zero for no reason. On a
7-day window there is usually no settled group at all, and the card says so instead of showing a
misleading number.

**By person / By row** — counts, not percentages, sorted by what was actually watched. At these
sample sizes a percentage is noise: ranking by one put a person with `1/31` above a person with
`3/103`. People and rows with nothing in the window fold away behind a disclosure rather than filling
the list with empty bars, and rows you have since deleted are hidden the same way — their picks still
count in the totals above.

Hiding a deleted row is the default because its picks are real history. If you want it actually gone —
a throwaway test row, say. Expand the disclosure and choose **Delete their history**. That permanently
removes those picks from every total that counts them, here and on each person's page, and cannot be
undone. Rows that still exist are never affected, whichever slug is named: Shortlist recomputes what is
eligible on the server rather than trusting the request.

**Watches per week** is always the long view: the last 16 weeks, whatever window is
selected.
