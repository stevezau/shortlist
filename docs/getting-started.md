---
title: Install Shortlist for Plex with Docker
description: Requirements, Docker install, first login and the setup wizard that connects your Plex server and builds each user's first personalized row.
heading: Getting started
---

## Requirements

- **Somewhere to run a Docker container.** Shortlist ships as a Docker image and is installed by
  running that container — on Windows, macOS, Linux, a NAS (Synology, QNAP, unRAID), or anything
  else that runs Docker. It does **not** have to be the same machine as Plex; it only needs to be
  able to reach your Plex server over the network.
- **Plex Media Server 1.43.2.10687 or newer** — this is the version where Plex started honouring
  the setting that hides each row. On anything older, a private row could show up for other people.
  The wizard checks your version before you begin, so you'll know straight away.
- **Plex Pass** on the server owner's account. The hiding feature is a Pass feature.
- A **TMDB API key** (free: themoviedb.org → Settings → API).

### Optional extras

**Shortlist works fully without any of these.**

- **Tautulli** — only improves the names people are shown by. Watch history comes straight from
  Plex either way, with no setup.
- **An AI provider** — Claude, GPT or Gemini, or a local server you run yourself (Ollama,
  llama.cpp, LM Studio, vLLM, LocalAI). This unlocks one extra source: a live web search for what
  to watch next.
- **An [Exa](https://exa.ai) key _or_ your own [SearXNG](https://docs.searxng.org)** — either makes
  that web search work with _any_ AI provider, including local ones that have no internet access.
  Exa needs a free-tier signup; SearXNG is free and runs on your own hardware. See
  [Which web-search backend should I use?](faq.md#which-web-search-backend-should-i-use).

## Install (Docker)

With Docker Compose:

```bash
mkdir shortlist && cd shortlist
curl -fsSLO https://raw.githubusercontent.com/stevezau/shortlist/master/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
docker compose up -d
```

Or with `docker run`:

```bash
docker run -d --name shortlist \
  -p 5959:5959 \
  -e TZ=Etc/UTC -e PUID=1000 -e PGID=1000 \
  -v /path/to/shortlist/config:/config \
  --restart unless-stopped \
  ghcr.io/stevezau/shortlist:latest
```

Open `http://your-host:5959`. A fresh install goes straight into the wizard. There is
nothing to sign in to yet. Step 1 connects your Plex account (that's the sign-in, and it's
what claims the instance for you); from then on Shortlist only opens for that account.

> Set Shortlist up on your own network first. Until you sign in with Plex and link a server,
> anyone who can open the page could claim it as theirs, so don't put it on the public internet
> until you've finished the wizard. Once you've claimed it, it's yours.

The wizard has **7 steps**, and the progress bar counts them the same way this list does:

1. **Welcome** — a short intro screen. Read it and continue.
2. **Connect Plex** — sign in with a PIN, then pick your server. Shortlist checks your Plex
   version, Plex Pass, and libraries, and tells you in plain English whether each one is OK.
3. **Recommendations & history**. Choose where picks come from (TMDB, Trakt, AI web search).
   Watch history comes straight from Plex with no setup. Tautulli is optional, and only improves
   the names people are shown by.
4. **Choose your AI provider** — Claude / GPT / Gemini / a local server / **None**. Keys stay
   yours: stored encrypted, and hidden again once saved. Picking None is a perfectly good choice.
5. **Pick your users** — everyone you share with, with badges showing how much history each
   person has.
6. **Make it yours** — the row's name, how many titles it holds, and how often it refreshes. Each
   row keeps its own schedule; there's no single global one.

   The name can be plain text, or use a placeholder that fills itself in per person, such as
   `{library_name}`, `{user}` or `{top_seed}`. See [Naming a row](guides/rows.md#naming-a-row)
   for what each one becomes.

7. **First run** — watch it build, person by person. When it finishes, everyone has their row.

## Trying it safely

Shortlist is new and it changes real Plex sharing settings, so you may well want to watch it work
before you trust it. Two ways to do that:

- **Safe mode** — start the container with `-e SHORTLIST_DRY_RUN=1`. Every run then logs exactly what
  it _would_ change and writes **nothing** to Plex. Walk the whole flow, read the run activity, and
  only remove the flag (and recreate the container) once you're happy.
- **One user first** — on the Users page, disable everyone except a test account, run, then sign in
  as that account (not the owner. The owner sees every row) and confirm they see only their own row.

The **first real run is the slowest**: it builds every enabled user's rows and merges every account's
share filter. Later runs are much faster. Most rows are unchanged and skipped.

Every row is kept private automatically: it's a labeled collection excluded on every other
account's share, delivered hidden and only promoted once those exclusions are in place. Your share
filters are snapshotted before the first change, so **Uninstall** (Settings → Danger Zone) puts them
back exactly as they were. This hiding relies on Plex Media Server ≥ 1.43.2.10687. Older builds
ignore the label exclusion, which is why the wizard surfaces your version before you begin.

## One thing you should know

You're in the user list too, so you can give yourself a row like anyone else. On a one-person
server that's the whole point.

What Plex cannot do is hide collections from the **server owner**: your own Home shows every user's
row, not just yours. If you share the server with other people and want a clean Home, watch on a
Plex Home user and keep the admin account for administration.

## You're set up. What now?

Everyone has a row and it will refresh on its own. Worth doing next:

- **Check it landed.** Sign in as somebody who isn't you and confirm they see their row, and only
  theirs. The owner account sees everybody's, so it can't tell you this.
- **Add another kind of row.** "Picked for You" is one of eight templates.
  See [Rows and templates](guides/rows.md).
- **Decide how often rows change.** Each row keeps its own schedule.
  See [Schedules](guides/schedules.md).
- **Let it fill gaps in your library.** Shortlist can ask Radarr or Sonarr for titles your people
  want but you don't have. See [Requests](guides/requests.md).

If a row doesn't turn up, [Troubleshooting](guides/troubleshooting.md) lists what usually causes it.
