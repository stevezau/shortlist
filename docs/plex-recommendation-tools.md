---
title: Plex recommendation tools compared
description: Kometa, SuggestArr, Recommendarr, Curatarr and Shortlist compared — what each actually does, and which ones give every Plex user their own recommendations.
heading: Plex recommendation tools compared
---

There are a lot of these now, they overlap confusingly, and the READMEs all say "personalized
recommendations". This page is an attempt at a straight comparison of what each one actually does.

**Disclosure:** Shortlist is my project, so read the row about it with that in mind. I've tried to be
accurate about everything else and to say plainly where another tool is the better pick. Facts
checked **14 August 2026** against each project's repository — this space moves fast, so verify
anything that matters to you.
[Corrections welcome.](https://github.com/stevezau/shortlist/issues/new/choose)

## Sent here after trying Kometa or SuggestArr?

If you asked for "a tool that gives each Plex user recommendations based on their watch history",
the three names you were almost certainly given first are **Kometa**, **SuggestArr** and
**Recommendarr**. They're the biggest and best-known projects near this problem, and it's worth
being precise about why none of them answer that particular question — because working it out by
installing all three takes an evening.

**[Kometa](https://kometa.wiki/)** (3,400+ stars, formerly Plex Meta Manager) is a metadata and
collection builder. It creates collections from rules you write in YAML — by genre, by decade, by
Trakt list, by almost anything — and manages artwork and metadata beautifully. It is the right tool
for shaping a library, and it's excellent at it.

It is not a recommender. It doesn't read anyone's watch history to decide what to suggest, and its
collections are library-wide: everyone with access sees the same ones. Shortlist is built to run
alongside Kometa rather than instead of it — it never touches a collection it didn't create.

**[SuggestArr](https://github.com/giuseppe99barchetta/SuggestArr)** (1,200+ stars) watches what you
recently played and automatically requests similar content through Jellyseerr/Overseerr, so your
library keeps growing with things you'll probably like. Works with Plex, Jellyfin and Emby.

It's about **acquisition**, not presentation. Its output is new files on your disk, not a row on
anyone's home screen — and the "recently watched" trigger is server-wide rather than a per-person
profile. If your complaint is "my library never has anything new", it's a great fit. If it's "my
library is huge and nobody can find anything in it", it's solving the opposite problem.

**Recommendarr** is AI-driven and works from Radarr/Sonarr library data. Be careful searching for it
— there are several unrelated repos under that name and near-spellings, so check what you're
installing. Like SuggestArr it's oriented toward what to _add_, not toward per-user rows in Plex.

**The common thread:** all three are server-wide. None of them produces a different row for
different people, and none of them hides one person's row from everyone else. That's not a flaw —
it's just a different problem, and it's the problem the rest of this page is about.

## First, decide which problem you have

The tools split into four groups, and picking from the wrong group is the usual mistake.

**"Keep my library stocked with things we'll like."** You want new content acquired automatically.
→ SuggestArr, Recommendarr.

**"Organise the library I already have."** You want well-built collections, artwork and metadata.
→ Kometa.

**"Help me decide what to watch tonight."** You want a dashboard you open when you can't choose.
It doesn't need to touch your Plex library at all. → SeekAndWatch.

**"Give each person on my server their own rows."** You share with family or friends and want each
of them to get suggestions from their own viewing — ideally without everyone else seeing them.
→ the rest of this page.

That last one is where the privacy question appears, and it's the axis the remaining tools differ
on most.

## The comparison

| Project                                                                                             | Shape                    | Per-user rows | Private per user               | AI            | Last updated |
| --------------------------------------------------------------------------------------------------- | ------------------------ | ------------- | ------------------------------ | ------------- | ------------ |
| [Shortlist](https://github.com/stevezau/shortlist)                                                  | Docker + web UI          | Yes           | Yes — share-filter labels      | Optional      | Active       |
| [Immaculaterr](https://github.com/ohmzi/Immaculaterr)                                               | Docker + web UI          | Yes           | Not claimed                    | Yes           | Active       |
| [Curatarr](https://github.com/OrchestratedChaos/curatarr)                                           | Binary / Python + web UI | Yes           | UI-level only, by its own docs | Scoring-based | Active       |
| [SeekAndWatch](https://github.com/softerfish/seekandwatch)                                          | Docker + web UI          | Dashboard     | n/a                            | No            | Apr 2026     |
| [TV-Show-Recommendations-for-Plex](https://github.com/netplexflix/TV-Show-Recommendations-for-Plex) | CLI script               | Via labels    | No                             | No            | Mar 2025     |
| [plex-recommendations-ai](https://github.com/rocstack/plex-recommendations-ai)                      | Docker                   | No            | No                             | Required      | May 2023     |
| [Kometa](https://kometa.wiki/)                                                                      | Docker / Python + YAML   | No            | No                             | No            | Active       |
| [SuggestArr](https://github.com/giuseppe99barchetta/SuggestArr)                                     | Docker + web UI          | No            | No                             | Optional      | Active       |

Kometa and SuggestArr are in the table because people arrive having tried them, not because they
compete on per-user rows — see [above](#sent-here-after-trying-kometa-or-suggestarr) for what each
is genuinely for.

### Shortlist

Per-user "Picked for You" rows built from each person's own watch history, made private with Plex's
label restrictions — every other account's share filter gets `label!=shortlist_<user>` merged into it,
so a row is visible only to its owner. Rows are delivered unpromoted, exclusions merged, and only then
promoted, so a row is never visible before the rule hiding it exists.

Share filters are snapshotted before the first write and restored exactly on uninstall; it merges
rather than rebuilds them, skips the owner, and never modifies a collection it didn't create.
Everything supports `--dry-run`. AI is optional — the built-in picker needs no keys.

Plex-only, and it will stay that way: the privacy model depends on Plex's label-based share filters,
which Jellyfin and Emby don't have. Needs Plex Media Server 1.43.2.10687+ and a Plex Pass on the
admin account. MIT.

### Immaculaterr

The most feature-dense of these. Event-driven rather than purely scheduled — it reacts when someone
finishes something and can build rows immediately, alongside off-peak refresh, discovery and library
cleanup. Builds a lot of named collections ("Based on your recently watched", "Change of Taste",
"Fresh Out Of The Oven"), supports profiles with their own users, media types and filters, and
integrates with Radarr/Sonarr.

Gives each monitored viewer separate rows and separate history, and pins rows to surfaces that viewer
can see — but it doesn't claim per-user _privacy_, which is a different thing from per-user content.
Ships on both GHCR and Docker Hub. Licensed under custom terms rather than a standard OSI licence,
which is worth reading if that matters to you.

**Pick this if** you want the richest set of automatic collections and a tool that reacts in real
time, and you're relaxed about other users seeing each other's rows.

### Curatarr

Analyses each user's watch history and scores unwatched library content by keyword, genre, cast and
director similarity, creating per-user collections that update automatically. Also generates external
watchlists so you know what to acquire next, and integrates streaming-service availability.

Its per-user separation is explicit in its own documentation about being a UI-level split rather than
access control — users' collections are separated in Browse and Search, not hidden from each other by
Plex permissions. That's a reasonable design choice, clearly disclosed, and fine on a server where
everyone's relaxed about it.

Distributed as self-updating signed binaries for Windows, macOS (Apple Silicon) and Linux as well as
from source — no Docker required, which is genuinely the easiest install here. MIT.

**Pick this if** you want per-user recommendations without running Docker, care about what to acquire
next as much as what to watch, and don't need rows hidden from each other.

### SeekAndWatch

A different category: a "what should we watch?" dashboard rather than a row builder. Connects Plex,
Tautulli, TMDB, Radarr and Sonarr in one place, with Smart Discovery from your watch history, a Kometa
config builder that saves you writing YAML, Overseerr requests and Tautulli trending. There's a hosted
Cloud beta so friends can request without access to your apps.

**Pick this if** the real problem is you and your household staring at the library unable to choose,
or if you want a Kometa config builder — that feature has little competition.

### TV-Show-Recommendations-for-Plex

A well-documented Python script rather than a service. Builds a taste profile from watch history —
genres, cast, crew, keywords, weighted by your Plex ratings — scores unwatched shows, and can label
them in Plex or push new titles to Sonarr via Trakt. Reads other users' history through Tautulli, and
runs attended (confirm each pick) or unattended. A companion
[Movie Recommendations](https://github.com/netplexflix/Movie-Recommendations-for-Plex) script covers
films.

Last updated March 2025, and no licence file — which by default means all rights reserved, worth
knowing before you build on it.

**Pick this if** you want something small you can read end to end and drive from cron, and you like
approving picks by hand.

### plex-recommendations-ai

Creates a single "Recommended" collection using OpenAI over your watch history, with a generated
summary explaining the choices. Simple, cheap to run, and one of the earliest tools in this space.

Not per-user, and unmaintained since May 2023 — listed here because it still ranks well in search
results and people find it first. No licence file.

## The three questions that actually narrow the list

**1. Do you want content acquired, or content surfaced?**

The most common wasted evening in this space is installing an acquisition tool to solve a
presentation problem. SuggestArr and Recommendarr add files to your library. Kometa organises the
files you have. Neither changes what any individual sees on their home screen. If your library is
already big and the complaint is "nobody can find anything", you want the surfacing group.

**2. Do you need rows to be private, or just personal?**

Those get conflated constantly and the difference is the whole architecture. _Personal_ means the
titles are chosen for one person. _Private_ means nobody else can see the row. Most tools do the
first. Doing the second requires Plex's label restrictions on share filters, a minimum server
version, a Plex Pass, and careful write ordering — which is why most tools don't.

On a two-person household, personal is plenty and you should pick on features. On a server shared
with fifteen friends, twelve rows called "Picked for Dave" on your home screen is a clutter problem
before it's a privacy one — and a row built from someone's viewing habits is more revealing than
people expect.

**3. Does it need to coexist with what you already run?**

Most people asking this question already have Kometa, Radarr and Sonarr in place. A recommender that
rewrites collections it didn't create, or that fights another tool over shelf order, costs more than
it adds. Check that whatever you pick scopes its writes to its own collections — and if you run
Kometa, that the two won't overwrite each other.

Answer those three and the list narrows to two or three.

## Related

- [How to improve Plex recommendations](improve-plex-recommendations.md) — before installing anything
- [AI recommendations for Plex](plex-ai-recommendations.md) — which tools need an API key and why
- [Per-user collections](plex-per-user-collections.md) — the privacy mechanism, and how to do it by hand
- [Recommendations from watch history](plex-recommendations-watch-history.md) — what to look for in any approach
- [Getting started](getting-started.md) — installing Shortlist
- [FAQ](faq.md) — Kometa coexistence, what the owner can see, uninstalling
