---
title: A tour of the Shortlist web interface
description: What every page in the Shortlist web interface does, and what each number on the dashboard actually means.
heading: The web interface
nav_order: 1
---

Eight pages in the sidebar. This is what each one is for.

## Dashboard

The impact report: what Shortlist delivered versus what people actually watched, for a window you
choose (7 / 30 / 90 days, or all time, 30 by default). Each headline figure carries its change
against the previous equal period, so you can see direction rather than a running total. There's
also a **Sync watched now** button to refresh the numbers on demand.

[What each figure means](#reading-the-dashboard) is at the bottom of this page.

## Rows

Create, edit, and reorder your rows. Each card shows who sees it and how it differs from the
defaults (sources, libraries, rebuild cadence, placement). This is where the whole multi-row feature
lives. See [Rows and templates](rows.md).

## Users

Everyone the server is shared with, plus you (badged `owner`, because plex.tv's user list leaves the owner
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

### Leaving someone's Plex sharing alone

To keep their own row private, Shortlist adds `label!=` exclusions to every other account's Plex
restrictions. Occasionally that fights a restriction you set yourself — most often an **allow only**
label list on a child's account, where the whole point is that the account sees nothing but the
labels you named.

Open that person, go to **Settings → Plex sharing**, and turn off **Manage their Plex sharing
settings**. Shortlist takes back out the exclusions it added and never touches that account again.
Their row in the Users list is badged **Sharing untouched** so you can see it at a glance, and
Support → Sharing lists them separately instead of reporting them as a fault.

The trade-off, plainly: that account can then see other people's rows, unless — as with an allow-only
list — its own Plex restrictions already keep it away from them. Everyone else still hides _this_
person's row as normal, so leaving one account alone never exposes their row to the rest of the
server.

This is not the same as switching someone **off**. Off means "no row for them" and still rewrites
their filters so they stop seeing everyone else's rows — unless you have also left their sharing
alone, which wins, because it means "don't touch this account" full stop. The two are independent:
someone can have a row _and_ untouched sharing.

One thing is deliberately left in place: if you have a **shared row limited to certain people**, the
entry hiding it from everyone else stays. That entry is the only thing keeping that row away from
people you didn't pick, so removing it would undo a choice you made on the row itself. The catch is
that later changes to who a shared row is for stop reaching a left-alone account — turn management
back on if you need them to pick those up.

### When someone leaves your server

Removing a person from your Plex share (or deleting a Plex Home user) is picked up by the daily user
sync. Shortlist switches them off, deletes their rows from the server, and badges them **Left the
server** in the Users list — distinct from an account _you_ switched off, which is the same
`disabled` state but means something completely different.

Two safety limits keep that sweep from acting on a bad read of plex.tv, because it deletes
collections and runs unattended: an **empty** roster is ignored entirely, and if **more than half**
your enabled accounts appear to vanish at once, nothing happens and an error is recorded instead.
Both cases are far more likely to be a truncated response than a real mass departure.

A departed row stays in the list so you can see what happened. **Remove** clears it out: their pick
history and run history are deleted and the row disappears. What it deliberately keeps is that
account's _original Plex share settings_, recorded before Shortlist ever touched them — so
uninstalling Shortlist can still put the account back exactly as it found it. That record is the only
copy, which is why Remove archives rather than deletes.

You do not have to clean up their share filters. Once their row is gone from the server, the next
privacy pass drops the leftover `label!=` entry from everyone else's filters on its own — but only
once two independent checks agree the row is really gone, never on the strength of one read.

### Accounts Plex restricts

Accounts with a Plex **restriction profile** (Younger Kid / Older Kid / Teen) are badged with that
profile's name. Plex usually hides collections from them, so no row is built. Plex also refuses
the privacy filters Shortlist writes, so those accounts are left out of them.

Both go away by setting **Restriction Profile → None** in Plex → Settings → Users & Sharing. You can
still limit them by rating or label there, which Plex only permits once the profile is None. A Plex
Home account with **no** profile is an ordinary user: it gets a row and privacy filters like anybody
else.

**"Sees N rows of others'".** "Usually" is doing real work in that first paragraph: a Younger Kid
account sees no collections at all, but an Older Kid account can see them. Since Plex refuses a
privacy filter for any profiled account, such an account can end up seeing rows built for other
people — and nothing Shortlist writes can hide them, because hiding a row _is_ the filter Plex is
refusing. Every run now checks each profiled account with that account's own token and badges it
here, on the person's page, and as a dashboard alert if it finds any.

Two fixes, both yours to make. Set that account's **Restriction Profile → None**, which lets the
normal filter apply and hides everyone else's rows from them; or turn the person **off** in
Shortlist, which leaves them out of rows entirely so there is nothing of anyone else's to find.

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
each proposed title kept or dropped, or struck through when it resolved to no real match (a
hallucination). Long lists of returned titles expand in place.

A **cold-start** user, with too little history to search from, gets the same page, showing the
highest-rated titles pulled from the server as their fallback — or, when their rows are set to skip
instead (see [Rows → People without enough watch history](rows.md#people-without-enough-watch-history)),
the reason no row was built and how many titles they have watched so far.

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

**Automatic** holds the ones Shortlist queues for itself when something changes: removing a
disabled person's rows, hiding a paused one's, tidying up after a row edit. Those have no button by
design: each one is aimed at a specific person or row by the action that queued it.

Open any job for its description, its settings (the frequency picker on the scheduled ones, the
backup retention and restore list), what the last run reported, and **Previous runs**, that job's
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

Everything on the dashboard is scoped to the window selected at the top, **the last 30 days** by
default. That matters more than it sounds: these figures used to be lifetime totals, which made
every ratio a measure of how long Shortlist had been installed rather than of how good the picks
were. A pick can only ever be credited **while its row is still showing it**, but the old
denominator kept every pick ever delivered, for ever, so each night added ~60 permanently
uncreditable picks per person to the bottom of the fraction and the number could only sink.

**Watched** — picks people STARTED in the window. A pick delivered last month and watched this week
counts here, as long as the row was still showing it: this figure is about watching, not delivery.
For a series it counts from the **first finished episode**, because that is Plex's own definition and
Plex offers no other — see Finished.

A watch is credited only when the title was **in one of that person's rows at the time**. If a row
rebuilt and swapped a title out, and they watched it afterwards, it does not count — they found it
some other way. Once a pick is credited it stays credited, and finishing a series months later still
upgrades it from started to finished.

**Finished** — of those, the ones they saw out: a film played, or a series with every episode
watched. The two are worth reading together. On the maintainer's own server, of 158 series picks
credited as watched only 21 had actually been finished and 31 were a single episode — so a lone
"watched" count makes a TV row look better than a movie row for a structural reason rather than a
real one. A big Watched with a small Finished means people are sampling, not staying.

**People watching** — how many people watched at least one pick, out of everyone currently enabled.

**Avg to watch** — average days from a title first being recommended to it first being watched, over
titles first watched in the window. Lower is better, and the change arrow is coloured accordingly.

**Landing rate** — the one percentage, and the only one computed carefully enough to trust. It is the
share of picks watched while their row was still showing them, measured over a **settled group**:
picks delivered in the window _and_ at least 30 days ago. A pick delivered yesterday is still sitting
in the row — it has not yet had its chance to be watched and dropped — so counting it would drag the
rate toward zero for no reason. On a
7-day window there is usually no settled group at all, and the card says so instead of showing a
misleading number.

**By person / By row** — counts, not percentages, sorted by what was actually watched, with the
finished count beside each. At these sample sizes a percentage is noise: ranking by one put a person
with `1/31` above a person with `3/103`. Sorting stays on watched deliberately — ranking on finished
would bury every TV row under every movie row, which says more about the medium than about the row. People and rows with nothing in the window fold away behind a disclosure rather than filling
the list with empty bars, and rows you have since deleted are hidden the same way, and their picks still
count in the totals above.

Hiding a deleted row is the default because its picks are real history. If you want it actually gone
(a throwaway test row, say) expand the disclosure and choose **Delete their history**. That permanently
removes those picks from every total that counts them, here and on each person's page, and cannot be
undone. Rows that still exist are never affected, whichever slug is named: Shortlist recomputes what is
eligible on the server rather than trusting the request.

**Watches per week** is always the long view: the last 16 weeks, whatever window is
selected. Each column is split: the solid part is what got finished, the faded part what is still
going. A past week's solid part can GROW later, when someone finally finishes a series they started
back then — the bar answers "what became of what landed that week", and that answer genuinely
changes.
