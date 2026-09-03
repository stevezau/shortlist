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
| Send missing films and shows to Radarr, Sonarr or Overseerr | [Requests](guides/requests.md)                               |
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
| [Requests (Radarr, Sonarr, Overseerr)](guides/requests.md) | Setting it up, the approval inbox, guardrails, why a title is still waiting |
| [Troubleshooting and backups](guides/troubleshooting.md) | The common failures, and what a backup does and doesn't hold                |
| [Putting it on the internet](guides/security.md)         | TLS, proxies, the API token, and what's in `/config/backups`                |

New here? [Getting started](getting-started.md) covers the install and the setup wizard first.
Looking for a specific setting or API endpoint? That's [Reference](reference.md).

## A row disappeared, or you want rows to take turns

A row can be given its own days: **Rows → the row → When it appears → Only on these days.** On the
days it is off, the row is hidden rather than deleted — it keeps its titles, so it comes straight
back on its next day without being built again.

Two rows can cover a week between them: set one to Mon/Wed/Fri and another to the remaining days, and
the Home screen alternates.

**If a row is missing and you did not expect it**, check the Rows page first — a row with a schedule
carries a **Hidden today** or **Showing today** badge, which answers it without opening anything.
There are three other reasons a row can be absent: it is switched off, the person is paused, or they
have too little watch history and the row's cold-start setting is **skip**.

Two things worth knowing:

- Days turn over at **midnight on the server**, on the server's clock. Somebody watching from another
  timezone sees the change at your midnight, not theirs.
- Some Plex apps cache the Home screen. A Roku re-reads it on its own; a Shield needs you to leave
  the Home screen and come back before the change shows.

Changing the days takes effect immediately — you do not have to wait for midnight or for the next
nightly run.

## Your rows sit at the bottom of the shelf

Check what the row is anchored to. **Rows → the row → Placement**, or **Settings → Row placement**
for the default, can be set to sit right after a collection — and that collection has to be on one of
the library's own Plex shelves, or there is no position to sit after.

Open the library in Plex → **Manage Recommendations**. If the collection you anchored to is in that
list with every toggle off, turn one on, or pick a different anchor. Plex's own rows — "Recently
Added" and the like — always work as anchors.

Shortlist leaves the rows where they are until then, and says so. On the **Logs** page, search for
`hub order`: the line names the library and the anchor. The same outcome is recorded in the change
log as well, which has no screen yet — read it at `/api/events/log?scope=run.hub_unplaced` after a
nightly run, or `?scope=shelf.unplaced` after **Check and fix rows on Plex** or a privacy sync.

A row that has never been built in that library yet looks the same from the shelf, but is not the
same thing: there is nothing to position until the row exists. Run it once and it lands in place.
Nothing is recorded in the change log for this, and usually nothing on the Logs page either — the
exception is a library where another row has its own placement, which puts a `hub order` line there
naming the row that is missing.

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

## Your AI web search finds nothing new

The `llm_web` source asks the web what to watch next. Which backend it asks is
**Settings → AI web search**, and the three choices behave very differently.

**Gemini answers from memory, not from the web.** Google's grounding tool is attached on every
call, and Gemini decides for itself whether to use it — for "what should I watch next" it almost
never does. It answers from training data instead, so the titles are real and well-chosen but often
years old. Nothing can force it: an explicit instruction to search, a search-shaped prompt and the
API's own "always call a tool" setting all leave it at zero searches. If you are on Gemini and want
genuinely current picks, point the backend at Exa or SearXNG. The logs say
`Gemini answered without searching the web` whenever this happens, so you can tell at a glance.

Claude and GPT both really search. GPT is much the cheaper of the two — about a cent per person per
night against Claude's ten, for the same quality — because Claude needs five searches to reliably
report release years where GPT needs one.

**Exa has a depth setting** (**Search depth**, next to the API key). It defaults to *Thorough*,
which costs $0.012 a search instead of $0.007. That is deliberate: the cheap modes are erratic — on
the same two searches, *Balanced* found 13 and 8 usable titles where *Thorough* found 47 and 36, and
once found none at all. Every search is cached for a fortnight and shared across everyone on your
server, so the extra half-cent buys candidates for the whole roster rather than one person.

**SearXNG needs its JSON API turned on**, or it answers Shortlist with a 403 — add `json` to
`search.formats` in its `settings.yml` and restart. Its upstream engines rate-limit self-hosted
instances, so expect a few to fail on any given search; the **Test** button names the ones that did.
