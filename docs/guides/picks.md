---
title: Choosing what goes in a row
description: Where candidate titles come from, how often a row changes, how to override any of it per row or per person, and how to stop one watch skewing someone's picks.
heading: What goes in a row
nav_order: 3
---

## Recommendation sources

Settings → **Finding titles** controls where candidate titles come from. Shortlist pools every source
you enable, keeps only what's already in your library, then ranks them with a simple no-AI score and
writes each pick's "why" in code. More sources means wider reach. Available today:

- **TMDB, similar titles.** The baseline. Titles TMDB says are similar to what each person watched.
- **TMDB, discover by taste.** Widens into popular, well-rated titles in the genres each person leans
  toward, derived from their watch history.
- **Trakt, related titles.** Needs a Trakt API key, added in Connections. Uses Trakt's recommendation
  graph, which often surfaces "what to watch next" picks that TMDB's similar list misses.
- **AI web search for what to watch next.** Searches the live web for current, well-reviewed titles,
  then resolves each against your library. This reaches beyond TMDB and Trakt to fresh releases and
  critics' lists.

  It works on **every** provider, via the **Search backend** you pick in its card: your provider's
  own web search (Claude, GPT or Gemini), an **Exa** key (any provider, and the only path for a local
  Ollama model), or **Auto**, the default, which uses your provider's tool _and_ Exa together when
  both are set up, since they surface mostly different titles. If a backend needs a key you don't
  have yet, the card lets you enter it right there.

Each row also chooses **which libraries** it builds in, using the row editor's Libraries picker. A
Plex collection lives in one library, so a row builds one collection per library you tick. Leave them
all ticked, the default, to cover every library, or point a row at just one such as "4K Movies" on a
server with several libraries of a type. What the row recommends, movies or shows or both, follows
the libraries you pick.

### Freshness, already-watched, and cost

Settings → Finding titles has three more dials, each of which a row can override:

- **How often it changes**, called **Freshness** in Settings where the global lives. This sets how
  often a row's picks change, and it is a pace rather than a nightly shuffle. `1.0` refreshes every
  night, lower means every few days, and `0.0` means build once and never reshuffle.

  On most nights an unchanged row is left exactly as it is, with no rebuild and no Plex write, which
  is why a person's row stays familiar instead of being reshuffled daily. On a refresh night the
  strongest two-thirds or so of the picks stay and the weakest third rotates out. The default is
  `0.5`, about weekly. If you trigger two runs the same day, a row that isn't due won't change. That
  is expected.

- **Already-watched titles.** How much of a partly-watched title still counts as watched and gets
  filtered out. The default keeps anything finished out of the picks.
- **Watches the AI web search looks up.** How many of each person's recent titles the AI web-search
  source looks up, one cached search each, taken off the front of the list above. This is the main
  cost lever on that source. Lower it to spend fewer tokens and Exa searches.

### If a watched title still gets recommended

This is rare, because Shortlist reads each person's **complete** watched set from Plex every run,
including titles they only _marked_ watched, whether ticked off individually or a whole season at
once, rather than played.

It reads the library _as that user_, with the per-user server token Plex mints for every share, and
`viewCount > 0` covers both plays and marks at any depth. There is nothing to configure, and it works
whether or not Shortlist runs on the same machine as Plex. This replaced an older playback-history
read that saw plays only and capped at around 200. On one real server that hid **13,201** of a user's
watched titles behind the roughly 1,000 the API reported.

When it does happen, it is almost always timing. **The read is per-run, so a title you mark watched
after the last run stays eligible until the next one.** To fix it immediately without waiting for a
scheduled run, go to **Jobs → Sync history**. That re-reads every user's watched set right now,
writes nothing to Plex, and updates what Shortlist knows, including the "N titles watched" count on
the Users page. Any run after that leaves the title out.

### Any row can override all of this

Settings → Finding titles sets what a row uses **unless the row says otherwise**. Open any row
(Rows → Edit) and it defines its own recipe:

| In the row editor                                    | What it overrides                                                                         |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **Recommendation sources**                           | Switch to "Choose for this row" and tick its own sources                                  |
| **Libraries**                                        | Which Plex libraries it builds in, which also sets what it recommends                     |
| **How often it changes**, **Already-watched titles** | How often it refreshes, and how much already-watched it allows                            |
| **Row size**, **Audience**                           | How many titles, and who gets it                                                          |
| **Watches the AI web search looks up**               | How many recent watches AI web search looks up for this row (shown only on rows using it) |
| **Request tag**                                      | The Radarr or Sonarr tag on titles requested for this row's audience                      |

So a "What to watch next" row can be Trakt-only, a "Hidden gems" row can use AI web search alone
pointed at just your 4K library, and your default "Picked for You" can stay on the global settings.
All on the same server, all at once. The Rows list shows each row's overrides on its card, so you can
see at a glance which rows differ.

A row left on "Use global default" stays in sync with Settings → Finding titles.

**You can go one step finer, per person.** Row size and recent-watches depth can also be set for a
single person on a single row. Open that person (Users → click them), find the row, and use
**Customize for this person**. Their value wins over the row's, which wins over the global default.
Leave a customization on "default" and that person follows the row like everyone else.

**There is one exception, the seeded "Picked for You" row.** Its **name** and **size** always follow
the global Settings under Row defaults so they stay in sync everywhere, and the row editor points you
there instead of offering its own. Its sources, libraries and audience are its own, exactly like any
other row.

**Changes clean up Plex right away.** You don't have to wait for a run:

- **Delete a row.** Its collections are removed from Plex immediately, for everyone who had it. That
  includes rows whose title is built from a person's top pick, which Shortlist finds by the exact
  title the last run delivered. The titles stay in your library; only the row goes.
- **Rename a row.** Its collection is retitled in place for every user, so nothing is orphaned.
- **Disable a user, or drop someone from a row's audience.** That person's now-stale collections are
  removed immediately.
- **Remove from Plex**, the button on each row. Clears a row's collections on demand without deleting
  the row's settings. Handy to force a rebuild on the next run.
- **Disable a row**, its on/off switch. Its collection comes off Plex Home on the next run. A row
  whose title is dynamic, built from a top pick, is left for that rebuild; use **Remove from Plex** if
  you want it gone right now. Everything left in place stays private, because the row's label keeps it
  excluded from everyone else.

## Blocking a seed

A **seed** is one of a person's recent watches that Shortlist searches from. When a watch isn't
really them, such as a film they put on for someone else or a genre they don't want more of, block
it. The watch stays in their history, it just stops shaping their picks.

The natural place to do it is a run's **How we picked** page, on the seeds list, since a bad seed is
usually what you noticed in the first place. There is also a search box on a person's detail page
(**Users → someone → Settings → Blocked seeds**) for a title you remember but can't find a run for.

Blocks are personal. A **shared** row is public, so one person's block does _not_ reshape what
everyone else sees. Otherwise an individual preference would become a server-wide edit nobody else
can see or undo. Shared rows use their own server-wide list,
`recommendations.blocked_shared_seeds`.

## Letting people block their own

Blocking is something only you can do, which means anyone who wants a bad seed gone has to message
you about it. **Respect Plex ratings** (Settings → Finding titles) closes that gap: when someone
rates a title 1 star or gives it a thumbs-down in Plex, it stops being used to find similar things
for them, exactly as if you had blocked it.

Nothing changes for them. They rate it in Plex, on the screen they just finished watching on. There
is no Shortlist account, no login, and nothing for you to do per person.

A few things worth knowing:

- **A rating they haven't given changes nothing.** Only a low rating acts, so this is silent for the
  majority of people, who rate nothing at all.
- **It takes up to a week.** Rating a title doesn't change when it was last watched, and the nightly
  sync only reads what changed — so a new rating is picked up by the weekly full re-read. Lower
  `sync.watch_full_days` if you want it sooner.
- **Shared rows ignore ratings**, for the same reason they ignore blocks.
- **Ratings written by another tool are ignored.** If you run Kometa's rating sync, or anything else
  that copies IMDb scores into Plex's user rating, Shortlist won't mistake those for your opinion —
  Plex's own star control only writes whole numbers, so a rating with a decimal in it wasn't typed by
  a person. A person's page says so plainly when this is happening to their account.

You can see all of this on **Users → someone → Watch history**: what they rated each title, and
which ones have stopped seeding as a result.
