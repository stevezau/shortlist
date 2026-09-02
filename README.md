<!-- PROJECT SHIELDS — reference-style, so the header stays readable while editing it. -->
<div align="center">

[![Build][build-shield]][build-url]
[![Release][release-shield]][release-url]
[![Coverage][codecov-shield]][codecov-url]
[![Docker Pulls][docker-shield]][docker-url]
[![Image Size][size-shield]][size-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![AI-Assisted][ai-shield]][ai-url]
[![Sponsor][sponsor-shield]][sponsor-url]

</div>

<!-- PROJECT LOGO -->
<div align="center">
  <img src="docs/assets/img/logo.svg" alt="" width="110" height="110">

  <h1 align="center">Shortlist</h1>

  <p align="center">
    Per-user movie &amp; TV recommendations for <strong>Plex</strong> — a private
    <strong>&ldquo;Picked for You&rdquo;</strong> row on every user&rsquo;s home screen, built from
    their own watch history and visible only to them.
    <br />
    Self-hosted, one Docker container, no AI key required.
    <br />
    <br />
    <a href="https://stevezau.github.io/shortlist/"><strong>Explore the docs »</strong></a>
    <br />
    <br />
    <a href="#quick-start">Quick start</a>
    &middot;
    <a href="https://stevezau.github.io/shortlist/plex-per-user-collections/">How per-user rows work</a>
    &middot;
    <a href="https://stevezau.github.io/shortlist/plex-recommendation-tools/">Tools compared</a>
    &middot;
    <a href="https://github.com/stevezau/shortlist/discussions/categories/q-a">Ask a question</a>
    &middot;
    <a href="https://github.com/stevezau/shortlist/issues/new?labels=bug">Report a bug</a>
  </p>
</div>

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

**It slots into the stack you already run.** Reads watch history straight from your Plex server
(Tautulli optional), pulls candidates from TMDB and Trakt, and hands gaps to **Radarr/Sonarr** —
while leaving Kometa's collections completely alone. One container, no database of its own to run.

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
  **Radarr/Sonarr** to grab it — or files a request in **Overseerr/Jellyseerr** and lets it do
  the fetching. Off by default and cautious: the strongest picks auto-send (a few a
  night); the rest wait in a **Requests** inbox for one-click approval.

**Trust & safety**

- 🔒 **Private by design** — share filters are snapshotted before the first change and fully
  restored on uninstall; rows are delivered hidden and only revealed once the exclusions exist.
- 📊 **Know if it's working** — a dashboard tracks what was delivered versus what people actually
  watched, per user and per row — and separates a title they **started** from one they **finished**,
  so a single episode of a series stops scoring like a whole film.
- 🧹 **Kometa-friendly** — never touches collections it didn't create.
- ↩️ **Provable uninstall** — one flow restores your server exactly as Shortlist found it.
- 🧪 **Safe mode** — set `SHORTLIST_DRY_RUN=1` to try it against your real server without writing a
  single change, until you're happy.
- 📦 **Homelab-native** — one container, `/config` volume, GHCR multi-arch, healthcheck,
  Unraid template.

## Screenshots

| Each person's row, and _why_ each pick                 | Watch every run, step by step                    |
| ------------------------------------------------------ | ------------------------------------------------ |
| ![A user's picks and why](docs/images/user-detail.png) | ![A run in progress](docs/images/run-detail.png) |

<sub>App screenshots use placeholder titles (a test library); the Plex row above is a real server.</sub>

## Where it fits

Shortlist does one narrow thing the rest of the stack doesn't: build a **different** collection for
each person and keep it private, inside Plex. It's designed to sit alongside what you already run:

- **It never touches what it didn't make.** Only collections carrying Shortlist's own `shortlist_*`
  label are ever modified — Kometa's collections, and anything you built by hand, are skipped.
- **It can be told to stand aside.** Every row also carries a plain `shortlist` label, so a tool that
  reorders the same shelf can exclude all of them with one entry — Agregarr's _Exclude from Ordering
  (Plex Label)_, for instance. Or turn Shortlist's own shelf ordering off entirely.
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
  stevezzau/shortlist:latest
```

Also on GHCR as `ghcr.io/stevezau/shortlist` — the identical image, same tags, no pull limits if
you'd rather avoid Docker Hub's.

Then open **http://your-host:5959** and follow the setup wizard — it connects your Plex account,
picks your server, and walks you to your first rows (about 10 minutes).

> 💡 Want to try it without touching your server first? Add `-e SHORTLIST_DRY_RUN=1` — Shortlist
> will show you exactly what it _would_ do and write nothing to Plex.

## Documentation

📖 **[stevezau.github.io/shortlist](https://stevezau.github.io/shortlist/)** — the docs as a website.

| Page                                       | What's in it                                        |
| ------------------------------------------ | --------------------------------------------------- |
| [Getting started](docs/getting-started.md) | Install, wizard, first run                          |
| [Guides](docs/guides.md)                   | Rows, schedules, requests, AI cost, troubleshooting |
| [Reference](docs/reference.md)             | Settings, API, env vars                             |
| [FAQ](docs/faq.md)                         | Privacy model, Kometa, uninstall                    |

### How Plex itself works

Background on the server, not on Shortlist — worth reading before you build anything on this
yourself, because most advice on the subject predates Plex's 2026 fixes and quietly leaks.

| Page                                                                             | What's in it                                                           |
| -------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| [Per-user collections](docs/plex-per-user-collections.md)                        | The label + share-filter mechanism, and the order that leaks           |
| [Improving Plex's recommendations](docs/improve-plex-recommendations.md)         | What Manage Recommendations really changes, and where it stops         |
| [Recommendations from watch history](docs/plex-recommendations-watch-history.md) | What Plex does with history, and why smart collections aren't personal |
| [A home screen per user](docs/plex-per-user-home-screen.md)                      | Pinned sources, managed users, and what none of them do                |
| [Netflix-style rows](docs/plex-netflix-style-recommendations.md)                 | The four properties that make rows feel personal                       |
| [AI recommendations](docs/plex-ai-recommendations.md)                            | Where a model helps, and where it invents films you don't own          |
| [Tools compared](docs/plex-recommendation-tools.md)                              | Shortlist, Immaculaterr, Curatarr, SeekAndWatch and others             |

## Support the project

Shortlist is free and MIT-licensed, and built in evenings. If it saved you some, you can
[sponsor it on GitHub](https://github.com/sponsors/stevezau) — entirely optional, and it buys time
rather than features on request.

Bug reports are worth just as much. The **Have an issue?** page runs read-only checks that often name
the cause outright, then opens a pre-filled issue with a secrets-free diagnostic to attach.

Not sure it's a bug? Ask in **[Discussions → Q&A](https://github.com/stevezau/shortlist/discussions/categories/q-a)**.
Answers there get marked as answers, so the next person searching the same problem finds one.

## License

MIT © Steven Adams

<!-- SHIELD DEFINITIONS -->
<!-- `for-the-badge` throughout: mixing shields' flat default with GitHub's own actions badge left
     the row at two different heights, which is what made it read as clutter rather than a header.
     The build badge tracks `master` (the released code), not the default branch — a green tick next
     to an unreleased dev commit tells a visitor nothing about what they are about to install. -->

[build-shield]: https://img.shields.io/github/actions/workflow/status/stevezau/shortlist/ci.yml?branch=master&style=for-the-badge&label=build
[build-url]: https://github.com/stevezau/shortlist/actions/workflows/ci.yml
[release-shield]: https://img.shields.io/github/v/release/stevezau/shortlist?style=for-the-badge&label=release
[release-url]: https://github.com/stevezau/shortlist/releases
[codecov-shield]: https://img.shields.io/codecov/c/github/stevezau/shortlist?style=for-the-badge
[codecov-url]: https://codecov.io/gh/stevezau/shortlist
[docker-shield]: https://img.shields.io/docker/pulls/stevezzau/shortlist?style=for-the-badge
[docker-url]: https://hub.docker.com/r/stevezzau/shortlist
[size-shield]: https://img.shields.io/docker/image-size/stevezzau/shortlist/latest?style=for-the-badge&label=image
[size-url]: https://hub.docker.com/r/stevezzau/shortlist/tags
[stars-shield]: https://img.shields.io/github/stars/stevezau/shortlist.svg?style=for-the-badge
[stars-url]: https://github.com/stevezau/shortlist/stargazers
[issues-shield]: https://img.shields.io/github/issues/stevezau/shortlist.svg?style=for-the-badge
[issues-url]: https://github.com/stevezau/shortlist/issues
[license-shield]: https://img.shields.io/github/license/stevezau/shortlist.svg?style=for-the-badge
[license-url]: https://github.com/stevezau/shortlist/blob/master/LICENSE
[ai-shield]: https://img.shields.io/badge/AI--Assisted-Claude%20Code-8A2BE2?style=for-the-badge&logo=anthropic&logoColor=white
[ai-url]: https://claude.com/claude-code
[sponsor-shield]: https://img.shields.io/badge/Sponsor-db61a2?style=for-the-badge&logo=githubsponsors&logoColor=white
[sponsor-url]: https://github.com/sponsors/stevezau
