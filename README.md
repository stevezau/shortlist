# Shortlist ✨

> **Per-user movie & TV recommendations for Plex.** A private, personalized **"Picked for You"** row
> on every user's Plex home screen — built from their own watch history, visible only to them.
> Self-hosted, one Docker container, no AI key required.

[![CI](https://github.com/stevezau/shortlist/actions/workflows/ci.yml/badge.svg)](https://github.com/stevezau/shortlist/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/codecov/c/github/stevezau/shortlist)](https://codecov.io/gh/stevezau/shortlist)
[![Latest release](https://img.shields.io/github/v/release/stevezau/shortlist?include_prereleases&label=release)](https://github.com/stevezau/shortlist/releases)
[![Stars](https://img.shields.io/github/stars/stevezau/shortlist)](https://github.com/stevezau/shortlist/stargazers)
[![Forks](https://img.shields.io/github/forks/stevezau/shortlist)](https://github.com/stevezau/shortlist/network/members)
[![Open issues](https://img.shields.io/github/issues/stevezau/shortlist)](https://github.com/stevezau/shortlist/issues)
[![Contributors](https://img.shields.io/github/contributors/stevezau/shortlist)](https://github.com/stevezau/shortlist/graphs/contributors)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![AI-Assisted](https://img.shields.io/badge/AI-assisted%20development-8A2BE2)

📖 **[Documentation](https://stevezau.github.io/shortlist/)** · [Getting started](https://stevezau.github.io/shortlist/getting-started/) · [How Plex per-user rows work](https://stevezau.github.io/shortlist/plex-per-user-collections/) · [Tools compared](https://stevezau.github.io/shortlist/plex-recommendation-tools/)

> [!IMPORTANT]
> **Shortlist 1.0.** It modifies other people's Plex views and share permissions, so it is built to
> be careful: every write is previewed by `--dry-run`, your share filters are snapshotted before
> they're touched, and a full uninstall puts everything back. **Please report bugs** — the
> **Have an issue?** page runs nineteen read-only checks that often name the cause outright, and
> then opens a pre-filled issue with a secrets-free diagnostic to attach.

## The problem: "what should I watch next?"

Everyone on your Plex server faces the same blank-screen problem — a huge library and no idea what
to put on. Plex's built-in recommendation rows are the same for everyone and ignore what _you've_
actually watched.

**Shortlist gives every user their own recommendations.** For each person it reads their own Plex
watch history and builds a personalized collection — "Picked for You" — of titles from your library
they haven't seen but probably want to, then puts it on their Plex home screen. Netflix-style
per-user recommendations, self-hosted on your own server. It's **private**: each person sees only
their own row, nobody else's. It refreshes automatically. It turns your library into something
everyone can actually discover from.

![A "Movies Picked for You" row on Plex](docs/images/plex-picked-for-you.jpg)

<sub>A "Picked for You" row on Plex — private to that user, built from their watch history.</sub>

## Why this couldn't exist before 2026

A row that only one person can see was simply impossible until recently. Plex had no per-user
collections, and the "hide this by label" setting it does have wasn't applied everywhere — so a
row meant for one person still showed up for others.

Plex fixed that in 2026: label hiding now works on the Home and Recommended shelves (v1.43.1) and
on Related rows (v1.43.2). Shortlist is built on that fix — each row is labelled, and every other
account is told to hide that label, so only its owner ever sees it.

## Features

**Personalized discovery**

- 👤 **A private row for every user** — built from _their_ watch history, visible only to them. One
  container serves your whole server. **Including you**: the server owner gets a row like anyone
  else, so Shortlist is just as useful on a one-person server.
- 🧠 **Smart picks, no hallucinations** — every pick is a title verified to exist in your library,
  never invented. **No AI key required**: the built-in picker runs entirely in code. An optional LLM
  (Claude / GPT / Gemini, or any local server: Ollama, llama.cpp, LM Studio, vLLM, LocalAI) adds one
  extra source — a live web search for what to watch next.
- 🌐 **Finds what to watch next from everywhere** — pools candidates from TMDB, Trakt, and an optional
  **live web search** for current, well-reviewed titles.
- 🔎 **Web search that works with _any_ model, even offline ones** — Shortlist runs the search
  itself, so your model never needs internet access. Works with a local Ollama box just as well as
  with Claude — via your provider's own web search, an [Exa](https://exa.ai) key, your own
  self-hosted [SearXNG](https://docs.searxng.org), or a combination.
  [How it works →](docs/guides/ai.md#the-one-ai-powered-source)
- 💬 **Explains itself** — every pick says "Because you watched X".
- 📚 **Watches whole shows, not episodes** — a 20-episode binge counts as one show, and it looks
  back through your full history so both movies and TV shape the picks.

**Make it yours**

- 🎞️ **Multiple rows per person + shared rows** — e.g. a personal row, a "New this week" shared
  row, per-library rows — each with its own sources, size, libraries, rebuild cadence, and audience.
  **Start from a template** — _Because you watched…_, _Happy to see again_, _Fresh finds_, _Popular on
  this server_ and more — rather than a blank form, then change anything you like.
- 🚫 **Block a bad seed** — a film someone put on for a friend shouldn't shape their picks. Block it
  from a run's "How we picked" page; the watch stays in their history, it just stops seeding.
- 🗓️ **A rebuild cadence you control** — set it in days (nightly, weekly, monthly, or never), so
  people aren't shown a totally reshuffled row every day.
- 📍 **Row placement** — choose which Plex shelf each row lands on (Home, the library's Recommended
  tab, or both) and where it sits, per row.
- 🎨 **Custom row posters (optional)** — upload artwork or generate it from text, reusing your AI key.

**Grow your library**

- 📥 **Fills its own gaps (optional)** — when a great pick isn't in your library, Shortlist can ask
  **Radarr/Sonarr** to grab it. Off by default and cautious: the strongest picks auto-send (a few a
  night); the rest wait in a **Requests** inbox for one-click approval.

**Trust & safety**

- 🔒 **Private by design** — share filters are snapshotted before the first change and fully
  restored on uninstall; rows are delivered hidden and only revealed once the exclusions exist.
- 📊 **Know if it's working** — a dashboard tracks what was delivered versus what people actually
  watched (hit rate), per user and per row.
- 🧹 **Kometa-friendly** — never touches collections it didn't create.
- ↩️ **Provable uninstall** — one flow restores your server exactly as Shortlist found it.
- 🧪 **Safe mode** — set `SHORTLIST_DRY_RUN=1` to try it against your real server without writing a
  single change, until you're happy.
- 📦 **Homelab-native** — one container, `/config` volume, GHCR multi-arch, healthcheck,
  Unraid template.

## Screenshots

|                                                                     |                                                             |
| ------------------------------------------------------------------- | ----------------------------------------------------------- |
| ![A user's picks and why](docs/images/user-detail.png)              | ![A run in progress](docs/images/run-detail.png)            |
| **Each person's row, and _why_ each pick** — "Because you watched…" | **Watch every run** — history → candidates → rank → deliver |

<sub>App screenshots use placeholder titles (a test library); the Plex row above is a real server.</sub>

## Where it fits

Shortlist does one narrow thing the rest of the stack doesn't: build a **different** collection for
each person and keep it private, inside Plex. It's designed to sit alongside what you already run:

- **It never touches what it didn't make.** Only collections carrying Shortlist's own `shortlist_*`
  label are ever modified — Kometa's collections, and anything you built by hand, are skipped.
- **It merges share filters, never rebuilds them.** Existing conditions are left byte-for-byte
  identical, and the originals are snapshotted before the first change.
- **It connects rather than duplicates.** Tautulli for richer history, Radarr/Sonarr for gaps, Trakt
  and MDBList for candidates — all optional. Only Plex and a free TMDB key are required.
- **Plex-only.** The privacy model depends on Plex's label-based share filters (PMS 1.43.2+), so
  there's no Jellyfin or Emby equivalent to port to.

Curious how the per-user privacy actually works?
See [How to make a Plex collection visible to only one user](docs/plex-per-user-collections.md).

## Quick start

**You'll need:** somewhere to run a **Docker container** (it does not have to be the same machine as
Plex, just able to reach it) · Plex Media Server ≥ 1.43.2.10687 · Plex Pass on the admin account · a
free TMDB key. Optional: Tautulli, an LLM key. Shortlist ships as a container only — there is no
standalone Windows/macOS/Linux installer. Details in [Getting started](docs/getting-started.md).

**With Docker Compose:**

```bash
mkdir shortlist && cd shortlist
curl -fsSLO https://raw.githubusercontent.com/stevezau/shortlist/master/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
docker compose up -d
```

**Or with `docker run`:**

```bash
docker run -d --name shortlist \
  -p 5959:5959 \
  -e TZ=Etc/UTC \
  -e PUID=1000 -e PGID=1000 \
  -v /path/to/shortlist/config:/config \
  --restart unless-stopped \
  ghcr.io/stevezau/shortlist:latest
```

Then open **http://your-host:5959** and follow the setup wizard — it connects your Plex account,
picks your server, and walks you to your first rows (about 10 minutes).

> 💡 Want to try it without touching your server first? Add `-e SHORTLIST_DRY_RUN=1` — Shortlist
> will show you exactly what it _would_ do and write nothing to Plex.

## Documentation

📖 **[stevezau.github.io/shortlist](https://stevezau.github.io/shortlist/)** — the docs as a website.

|                                            |                                                     |
| ------------------------------------------ | --------------------------------------------------- |
| [Getting started](docs/getting-started.md) | Install, wizard, first run                          |
| [Guides](docs/guides.md)                   | Rows, schedules, requests, AI cost, troubleshooting |
| [Reference](docs/reference.md)             | Settings, API, env vars                             |
| [FAQ](docs/faq.md)                         | Privacy model, Kometa, uninstall                    |

### How Plex itself works

Background on the server, not on Shortlist — worth reading before you build anything on this
yourself, because most advice on the subject predates Plex's 2026 fixes and quietly leaks.

|                                                                                  |                                                                        |
| -------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| [Per-user collections](docs/plex-per-user-collections.md)                        | The label + share-filter mechanism, and the order that leaks           |
| [Recommendations from watch history](docs/plex-recommendations-watch-history.md) | What Plex does with history, and why smart collections aren't personal |
| [A home screen per user](docs/plex-per-user-home-screen.md)                      | Pinned sources, managed users, and what none of them do                |
| [Netflix-style rows](docs/plex-netflix-style-recommendations.md)                 | The four properties that make rows feel personal                       |
| [Tools compared](docs/plex-recommendation-tools.md)                              | Shortlist, Immaculaterr, Curatarr, SeekAndWatch and others             |

## License

MIT © Steven Adams
