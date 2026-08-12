# Shortlist

**Per-user movie & TV recommendations for Plex.** A private, personalized **"Picked for You"** row on
every user's Plex home screen — built from their own watch history, visible only to them.
Self-hosted, one Docker container, no AI key required.

[Source on GitHub](https://github.com/stevezau/shortlist) ·
[Documentation](https://stevezau.github.io/shortlist/) ·
[Report a bug](https://github.com/stevezau/shortlist/issues/new/choose)

> The identical image is also published to GHCR as `ghcr.io/stevezau/shortlist` — same build, same
> manifest, same tags. Pull from there if you'd rather not hit Docker Hub's anonymous pull limits.

## The problem

Everyone on your Plex server sees the same recommendation rows. Plex's Recommended and Home shelves
come from the library, not from the person looking at it — so the friend who has watched every
sci-fi film you own and the one who only watches comedies get an identical home screen.

Shortlist gives **each user their own row**, built from **their own** watch history, containing only
titles that are **already in your library** and that they haven't seen.

And it's private: each person sees only their own row, nobody else's.

## How the privacy works

Plex has no per-user collections. It does have **label restrictions** on share filters, which Plex
evaluates per account. Each row is labelled `shortlist_<user>`, and `label!=shortlist_<user>` is
merged into every _other_ account's share filter — leaving one person who can see it.

The ordering is what makes it safe. Every run delivers rows **unpromoted**, merges the exclusions
into every share filter, and **only then** promotes rows onto Home. A row is never visible before
the rule that hides it exists.

This needs **Plex Media Server 1.43.2.10687 or newer** and a **Plex Pass** on the admin account.
Earlier versions ignored label restrictions on the Home, Recommended and Related shelves, which is
why older forum threads say this can't be done.

## Quick start

```bash
docker run -d --name shortlist \
  -p 5959:5959 \
  -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
  -v /path/to/config:/config \
  stevezzau/shortlist:latest
```

Then open **http://your-host:5959** and follow the setup wizard — it connects your Plex account,
picks your server, and walks you to your first rows in about ten minutes.

**Try it without touching your server:** add `-e SHORTLIST_DRY_RUN=1` and Shortlist will show you
exactly what it _would_ do and write nothing to Plex.

### docker-compose

```yaml
services:
  shortlist:
    image: stevezzau/shortlist:latest
    container_name: shortlist
    restart: unless-stopped
    ports:
      - "5959:5959"
    environment:
      PUID: 1000
      PGID: 1000
      TZ: Etc/UTC
    volumes:
      - /path/to/config:/config
```

## Tags

| Tag      | What it is                                            |
| -------- | ----------------------------------------------------- |
| `latest` | The current stable release                            |
| `X.Y.Z`  | A specific release, pinned                            |
| `dev`    | Every green push to `dev` — newest code, less settled |

Multi-arch: `linux/amd64` and `linux/arm64`.

## What you get

- **A private row for every user**, built from their watch history. One container serves your whole
  server — including you, so it's just as useful on a one-person server.
- **No AI key required.** The built-in picker runs entirely in code. An optional LLM (Claude, GPT,
  Gemini, or a local model via Ollama, llama.cpp, LM Studio, vLLM or LocalAI) adds a ranking and
  explanation pass.
- **No hallucinated picks.** Every title is verified to exist in your library before it's delivered.
- **Every pick explains itself** — "Because you watched _Arrival_".
- **Multiple rows per person, plus shared rows**, each with its own sources, size, libraries,
  refresh cadence and audience.
- **Radarr/Sonarr requests (optional)** when a strong pick isn't in your library yet.
- **Kometa-friendly** — never touches a collection it didn't create.
- **Provable uninstall** — share filters are snapshotted before the first change and restored
  exactly.

## Configuration

Settings live in the app's web interface, not in environment variables. The only environment
variables that stay live are infrastructure ones: `PUID`, `PGID`, `TZ`, `PORT`, `APP_BASE_PATH`, and
`SHORTLIST_DRY_RUN`.

Full list: [Reference](https://stevezau.github.io/shortlist/reference/).

## Requirements

- Plex Media Server **1.43.2.10687+** with a **Plex Pass** on the admin account
- A free [TMDB](https://www.themoviedb.org/) API key
- A volume mounted at `/config`

## Links

- [Documentation](https://stevezau.github.io/shortlist/)
- [Getting started](https://stevezau.github.io/shortlist/getting-started/)
- [How per-user Plex collections work](https://stevezau.github.io/shortlist/plex-per-user-collections/)
- [Plex recommendation tools compared](https://stevezau.github.io/shortlist/plex-recommendation-tools/)
- [FAQ](https://stevezau.github.io/shortlist/faq/)

MIT licensed. Not affiliated with Plex Inc. Plex is a trademark of Plex, Inc.
