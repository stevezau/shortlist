---
title: Shortlist — per-user recommendations for Plex
description: Documentation for Shortlist, a self-hosted container that gives every user on your Plex server a private, personalized "Picked for You" row built from their own watch history.
permalink: /
---

# Shortlist documentation

| Guide                                 | What's in it                                                       |
| ------------------------------------- | ------------------------------------------------------------------ |
| [Getting started](getting-started.md) | Docker install, first login, the setup wizard, your first run      |
| [Guides](guides.md)                   | The web interface, schedules, per-user overrides, troubleshooting  |
| [Reference](reference.md)             | Configuration keys, API endpoints, environment variables, defaults |
| [FAQ](faq.md)                         | Privacy model, Plex requirements, Kometa coexistence, uninstalling |

**Background reading:**
[How to make a Plex collection visible to only one user](plex-per-user-collections.md) — the label +
share-filter mechanism Shortlist is built on, the version requirements, and the ordering mistake
that leaks a "private" collection to your whole server. Useful whether or not you run Shortlist.

**The short version:** run the container, log in with Plex, pick your users, and every night each
user gets a personal "✨ Picked for You" row built from their own watch history — visible only to
them. Each row is delivered hidden, the exclusions that keep it private are merged into everyone
else's share, and only then is it promoted onto Home — so a row is never visible before it's
private. Your share filters are snapshotted first, so uninstalling puts them back exactly.
