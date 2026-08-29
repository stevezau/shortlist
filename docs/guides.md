---
title: Shortlist guides
description: How to do the things people actually want to do with Shortlist, from changing how often a row refreshes to sending missing films to Radarr.
heading: Guides
---

Eight short pages instead of one long one. If you know what you want to do, start here.

## What do you want to do?

| I want to…                                       | Go to                                                                   |
| ------------------------------------------------ | ----------------------------------------------------------------------- |
| Work out what a page in the app is for           | [The web interface](guides/interface.md)                                |
| Give someone a different kind of row             | [Rows and templates](guides/rows.md)                                    |
| Name a row after the film that inspired it       | [Naming a row](guides/rows.md#naming-a-row)                             |
| Change the order titles appear in                | [The order titles appear in](guides/rows.md#the-order-titles-appear-in) |
| Move a row to the top of the shelf               | [Row placement](guides/rows.md#row-placement-recommended-shelf)         |
| Give a row its own artwork                       | [Row posters](guides/rows.md#row-posters)                               |
| Change where the suggestions come from           | [What goes in a row](guides/picks.md)                                   |
| Stop one film skewing someone's picks            | [Blocking a seed](guides/picks.md#blocking-a-seed)                      |
| Change how often a row refreshes                 | [Schedules and runs](guides/schedules.md)                               |
| Use AI, or keep it cheap                         | [AI and cost](guides/ai.md)                                             |
| Send missing films and shows to Radarr or Sonarr | [Requests](guides/requests.md)                                          |
| Find out why a row didn't turn up                | [Troubleshooting](guides/troubleshooting.md)                            |
| Work out what's wrong, or file a bug report      | [Have an issue?](guides/troubleshooting.md#start-here-have-an-issue)    |
| Know what's in a backup                          | [Backups](guides/troubleshooting.md#backups)                            |
| Put Shortlist on the internet safely             | [Putting it on the internet](guides/security.md)                        |

## The pages

| Page                                                     | What's in it                                                                |
| -------------------------------------------------------- | --------------------------------------------------------------------------- |
| [The web interface](guides/interface.md)                 | What every page does, and what each dashboard figure means                  |
| [Rows and templates](guides/rows.md)                     | Starting from a template, naming, ordering, where a row shows, posters      |
| [What goes in a row](guides/picks.md)                    | Recommendation sources, rebuild cadence, per-row and per-person overrides   |
| [Schedules and runs](guides/schedules.md)                | Each row's own schedule, custom schedules, the jobs worth knowing about     |
| [AI and cost](guides/ai.md)                              | What AI does, which search backend to pick, how to keep the bill down       |
| [Requests (Radarr and Sonarr)](guides/requests.md)       | Setting it up, the approval inbox, guardrails, why a title is still waiting |
| [Troubleshooting and backups](guides/troubleshooting.md) | The common failures, and what a backup does and doesn't hold                |
| [Putting it on the internet](guides/security.md)         | TLS, proxies, the API token, and what's in `/config/backups`                |

New here? [Getting started](getting-started.md) covers the install and the setup wizard first.
Looking for a specific setting or API endpoint? That's [Reference](reference.md).

## Your rows sit at the bottom of the shelf

Check what the row is anchored to. **Rows → the row → Placement**, or **Settings → Row placement**
for the default, can be set to sit right after a collection — and that collection has to be on one of
the library's own Plex shelves, or there is no position to sit after.

Open the library in Plex → **Manage Recommendations**. If the collection you anchored to is in that
list with every toggle off, turn one on, or pick a different anchor. Shortlist leaves the rows where
they are until then. You'll see it on the **Logs** page, naming the library and the anchor —
`run.hub_unplaced` after a nightly run, or `shelf.unplaced` after **Check and fix rows on Plex** or a
privacy sync. Plex's own rows — "Recently Added" and the like — always work as anchors.

The same thing happens with a row that has never been built in that library yet: there is nothing to
position until it exists. Run it once and it lands in place.

## Another tool keeps moving your rows

Agregarr, Kometa and similar tools reorder the same Plex Recommended shelf Shortlist does, so rows
can appear to shuffle between runs. Both sides can be told to leave the other alone.

In Agregarr, put `shortlist` in **Settings → General → Exclude from Ordering (Plex Label)**. That one
word covers every Shortlist row: the field matches a label exactly OR as a prefix followed by `_`, so
it catches the constant `shortlist` label and each person's `shortlist_<name>` one. It keeps working
as people join and leave, and needs no updating.

That field is newer than Agregarr's v2.9.1 release — at the time of writing it is on the maintained
fork's `:develop` image (`bitr8/agregarr:develop`).

Shortlist's own side is **Settings → Row placement**, which decides where it puts rows and whether
it manages shelf order at all — turning that off leaves the order entirely to the other tool.
