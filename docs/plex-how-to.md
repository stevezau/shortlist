---
title: Plex how-to guides
description: How Plex itself handles recommendations, per-user rows and label restrictions — what it can do, what it can't, the manual methods, and where they stop scaling.
heading: Plex how-to
---

These pages are about **Plex**, not about Shortlist. They explain what the server actually does,
what it doesn't, and how far you can get by hand — because most of the advice on this subject is
either years out of date or quietly wrong about what leaks.

If you already run Shortlist and want to know which button does what, you want the
[Guides](guides.md) instead.

## Start with the question you have

| The question                                          | The page                                                                         |
| ----------------------------------------------------- | -------------------------------------------------------------------------------- |
| Can Plex recommend things based on what I've watched? | [Recommendations from watch history](plex-recommendations-watch-history.md)      |
| Can each user have their own home screen rows?        | [A different home screen per user](plex-per-user-home-screen.md)                 |
| Can I make a collection only one person can see?      | [Per-user collections](plex-per-user-collections.md)                             |
| How do I get rows that feel like Netflix's?           | [Netflix-style rows for your own library](plex-netflix-style-recommendations.md) |
| Which tool should I actually install?                 | [Plex recommendation tools compared](plex-recommendation-tools.md)               |

## The short version of all of it

Plex has two separate things people mean by "recommendations", and confusing them is the source of
most of the bad advice out there.

**Discover** is Plex's account-level feature. It suggests things to watch across streaming services
and it is genuinely personal to your Plex account — but it mostly points at content you don't own,
and it doesn't build rows from your server's library.

**The rows on your server** — Recommended, Home, and anything you pin — come from the library, not
from the person looking at it. Two people opening the same shared library see the same rows in the
same order, whatever either of them has watched. There is no per-account personalisation of library
shelves anywhere in Plex's settings, and that's the gap every tool in this space is trying to fill.

The one Plex primitive that _is_ evaluated per account is the **share filter**, and specifically its
label restrictions. That's the lever. It's not a recommendation feature — it's an access-control
feature — but pointed the right way it's what makes a per-user row possible at all. The
[per-user collections](plex-per-user-collections.md) page is the full explanation, including the
version requirements and the ordering mistake that leaks a private row to everyone.

## A note on old advice

Label-based hiding didn't reliably work until 2026. Plex applied the restriction in the library
view but not on the Home shelf, the Recommended shelf, or in Related rows, so a "private" collection
was visible in at least one place. Threads from 2019–2025 saying this can't be done were correct
when they were written.

Plex Media Server **1.43.1** fixed Home and Recommended; **1.43.2.10687** fixed Related. Anything
built on this needs those versions. If a page or a script doesn't mention a minimum server version,
it predates the fix and you should assume it leaks.
