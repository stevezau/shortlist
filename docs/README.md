<!--
Browsing this folder on github.com? You're in the right place — these .md files
are the docs. They're also published as a website, which is easier to read:

    https://stevezau.github.io/shortlist/

This file exists only for the github.com folder view. The website's home page is
index.md, and _config.yml excludes this file from the build so the two don't
fight over the `/` URL.
-->

# Shortlist documentation

**[Read these as a website →](https://stevezau.github.io/shortlist/)**

| Guide                                                     | What's in it                                                       |
| --------------------------------------------------------- | ------------------------------------------------------------------ |
| [Getting started](getting-started.md)                     | Docker install, first login, the setup wizard, your first run      |
| [Guides](guides.md)                                       | Rows, schedules, requests, AI cost, troubleshooting                |
| [Reference](reference.md)                                 | Configuration keys, API endpoints, environment variables, defaults |
| [FAQ](faq.md)                                             | Privacy model, Plex requirements, Kometa coexistence, uninstalling |
| [Other Plex tools](comparison.md)                         | How Shortlist relates to netplexflix, SuggestArr, Seerr, Kometa     |
| [Per-user Plex collections](plex-per-user-collections.md) | The label + share-filter mechanism everything above is built on    |

**The short version:** run the container, log in with Plex, pick your users, and every night each
user gets a personal "✨ Picked for You" row built from their own watch history — visible only to
them. Each row is delivered hidden, the exclusions that keep it private are merged into everyone
else's share, and only then is it promoted onto Home — so a row is never visible before it's
private. Your share filters are snapshotted first, so uninstalling puts them back exactly.
