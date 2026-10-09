---
title: Kometa per-user recommendations
description: Kometa builds collections that every user sees. What it can and can't do for per-user recommendations on Plex, and how to run Shortlist alongside it without the two overwriting each other.
heading: Can Kometa give each Plex user their own recommendations?
updated: 2026-10-09
byline: true
---

**Short answer:** not on its own. Kometa builds collections from rules you write in YAML, and those
collections are **library-wide**: everyone with access to the library sees the same ones. It doesn't
read each person's watch history to decide what to suggest, so it isn't a per-user recommender.

That isn't a flaw. Kometa organises the library you already have, and it does that very well. It just
answers a different question from "what should Alice watch next?".

## What Kometa is good at

- Collections by genre, decade, studio, Trakt list or almost any other rule.
- Artwork, overlays and metadata.
- Keeping all of that in sync on a schedule.

## Why per-user rows need something else

A row that only one person sees takes two things Kometa doesn't do:

1. **Choose titles for one person**, from that person's own watch history.
2. **Hide the row from everyone else**, using a label on the collection and a matching exclude on
   every other account's share filter. The steps are in
   [how to make a Plex collection visible to only one user](plex-per-user-collections.md).

You can approximate the second part by hand. Doing it for every person, keeping it correct as people
are added, and writing it in the right order so a row never shows to the wrong person is what
Shortlist automates.

## Running both

They work side by side. Shortlist only changes collections that carry its own `shortlist_*` label.
Your Kometa collections, overlays and anything you made yourself are detected and left alone. Placing
Shortlist's rows can move other shelves, but it keeps their order relative to each other. See the
[FAQ](faq.md#will-it-fight-with-kometa) for the details.

A sensible split:

- **Kometa** for the shared shelves: genres, franchises, "Top 250", seasonal collections.
- **Shortlist** for the rows that are different for each person: "Picked for You", "Because you
  watched", "Watch it again".

## Related

- [Plex recommendation tools compared](plex-recommendation-tools.md)
- [Recommendations from each person's watch history](plex-recommendations-watch-history.md)
- [Getting started](getting-started.md)
