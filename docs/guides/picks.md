---
title: Choosing what goes in a row — sources, freshness and overrides
description: Where candidate titles come from, how often a row changes, how to override any of it per row or per person, and how to stop one watch skewing someone's picks.
heading: What goes in a row
nav_order: 3
---

## Recommendation sources

Settings → **Finding titles** controls where candidate titles come from. Shortlist pools every
source you enable, keeps only what's already in your library, then ranks them (a simple, no-AI
score) and writes each pick's "why" in code. More sources = wider reach. Available today:

- **TMDB — similar titles**: the baseline — titles TMDB says are similar to what each person watched.
- **TMDB — discover by taste**: widens into popular, well-rated titles in the genres each person
  leans toward (derived from their watch history).
- **Trakt — related titles** (needs a Trakt API key, added in Connections): uses Trakt's
  recommendation graph, which often surfaces "what to watch next" picks TMDB's similar list misses.
- **AI — web search for what to watch next**: searches the live web for current, well-reviewed titles
  to watch next, then resolves each against your library — reaching beyond TMDB/Trakt to fresh releases
  and critics' lists. Works on **every** provider, via the **Search backend** you pick in its card:
  your provider's own web search (Claude, GPT, or Gemini), an **Exa** key (any provider — the only path
  for a local Ollama model), or **Auto** (the default), which uses your provider's tool _and_ Exa
  together when both are set up, since they surface mostly different titles. If a backend needs a key
  you don't have yet, the card lets you enter it right there.

Each row also chooses **which libraries** it builds in (the row editor's Libraries picker). A Plex
collection lives in one library, so a row builds one collection per library you tick — leave them all
ticked (the default) to cover every library, or point a row at just one (e.g. "4K Movies") on a
server with several libraries of a type. What the row recommends (movies, shows, or both) follows the
libraries you pick.

### Freshness, already-watched, and cost

Settings → Finding titles has three more dials (each per-row overridable):

- **How often it changes** (called **Freshness** in Settings, where the global lives) — how
  often a row's picks change. This is a **cadence, not a nightly shuffle**:
  `1.0` refreshes every night, lower means every few days, and `0.0` means "build once, then never
  reshuffle". On most nights an unchanged row is left exactly as-is — no rebuild, no Plex write
  — which is why a person's row stays familiar instead of being reshuffled daily. On a refresh night
  the strongest ~two-thirds of picks stay and the weakest third rotates out. Default `0.5`
  (about weekly). If you trigger two runs the same day, a row that already isn't due won't change —
  that's expected.
- **Already-watched titles** — how much of a partly-watched title still counts as "watched" and gets
  filtered out. Default keeps anything finished out of the picks.
- **Watches the AI web search looks up** — how many of each person's recent titles the AI web-search
  source looks up (one cached search each), taken off the front of the list above. It's the main
  **cost lever** on that source — lower it to spend fewer tokens/Exa searches.

### If a watched title still gets recommended

Shortlist reads each person's **complete** watched set from Plex every run — including titles they
only _marked_ watched (ticked off, or a whole season marked) rather than played — so this is rare.
It reads the library _as that user_, with the per-user server token Plex mints for every share, and
`viewCount > 0` covers both plays and marks at any depth. Nothing to configure, and it works whether
or not Shortlist runs on the same machine as Plex. (This replaced the old playback-history read,
which saw plays only and capped at ~200 — on one real server that hid **13,201** of a user's watched
titles behind the ~1,000 the API reported.)

When it does happen, it's almost always timing: **the read is per-run, so a title you mark watched
after the last run stays eligible until the next one.** To fix it immediately without waiting for a
scheduled run, go to **Jobs → Sync history** — it re-reads every user's watched set right now,
writes nothing to Plex, and updates what Shortlist knows (and the "N titles watched" count on the
Users page). Any run after that leaves the title out.

### Everything above is only the _default_ — rows override it

Settings → Finding titles sets what a row uses **unless the row says otherwise**. Open any row
(Rows → Edit) and it defines its own recipe:

| In the row editor                                    | What it overrides                                                                         |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **Recommendation sources**                           | Switch to "Choose for this row" and tick its own sources                                  |
| **Libraries**                                        | Which Plex libraries it builds in — which also sets what it recommends                    |
| **How often it changes**, **Already-watched titles** | How often it refreshes, and how much already-watched it allows                            |
| **Row size**, **Audience**                           | How many titles, and who gets it                                                          |
| **Watches the AI web search looks up**               | How many recent watches AI web search looks up for this row (shown only on rows using it) |
| **Request tag**                                      | The Radarr/Sonarr tag on titles requested for this row's audience                         |

So a "What to watch next" row can be Trakt-only, a "Hidden gems" row can be AI-web-search-only
pointed at just your 4K library, and your default "Picked for You" can stay on the global settings —
all on the same server, all at once. The Rows list shows each row's overrides on its card, so you can
see at a glance which rows differ.

A row left on "Use global default" stays in sync with Settings → Finding titles.

**And one step finer — per person.** Row size and recent-watches depth can also be set for a single
person on a single row: open that person (Users → click them), find the row, and use **Customize for
this person**. Their value wins over the row's, which wins over the global default. Leave a
customization on "default" and that person follows the row like everyone else.

**The one exception is the seeded "Picked for You" row**: its **name** and **size** always follow the
global Settings (Row defaults) so they stay in sync everywhere — the row editor points you there
instead of offering its own. Its sources, libraries and audience are its own, exactly like any other
row.

**Changes clean up Plex right away.** You don't have to wait for a run:

- **Delete a row** → its collections are removed from Plex immediately, for everyone who had it
  (including rows whose title is built from a person's top pick — Shortlist finds them by the exact
  title the last run delivered). The titles stay in your library; only the row goes.
- **Rename a row** → its collection is retitled in place for every user, so nothing is orphaned.
- **Disable a user, or drop someone from a row's audience** → that person's now-stale collections are
  removed immediately.
- **Remove from Plex** (the button on each row) → clears a row's collections on demand, without
  deleting the row's settings — handy to force a rebuild on the next run.
- **Disable a row** (its on/off switch) → its collection comes off Plex Home on the next run. A row
  whose title is dynamic (built from a top pick) is left for that rebuild; use **Remove from Plex** if
  you want it gone right now. Everything left in place stays private — the row's label keeps it
  excluded from everyone else.

## Blocking a seed

A **seed** is one of a person's recent watches that Shortlist searches from. When a watch isn't
really them — a film they put on for someone else, a genre they don't want more of — block it: the
watch stays in their history, it just stops shaping their picks.

The natural place to do it is a run's **How we picked** page, on the seeds list, where a bad seed is
usually what you noticed in the first place. There's also a search box on a person's detail page
(**Users → someone → Settings → Blocked seeds**) for a title you remember but can't find a run for.

Blocks are personal. A **shared** row is public, so one person's block deliberately does _not_
reshape what everyone else sees — otherwise an individual preference would become a server-wide edit
nobody else can see or undo. Shared rows use their own server-wide list
(`recommendations.blocked_shared_seeds`).
