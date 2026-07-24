# Getting started

## Requirements

- **Plex Media Server ≥ 1.43.2.10687** — earlier versions ignore the label exclusion that hides
  each row, so a private row could leak onto other accounts' Home/Recommended/Related. The wizard
  shows your server's version up front so you can confirm it before you start.
- **Plex Pass** on the server owner's account (label restrictions are a Pass feature).
- A **TMDB API key** (free: themoviedb.org → Settings → API).
- Optional: **Tautulli** for friendlier display names (watch history is read straight from Plex,
  no setup); **an LLM API key** (Anthropic/OpenAI/Google) or a **local server** (Ollama, llama.cpp,
  LM Studio, vLLM, LocalAI) — Shortlist is fully functional with none of these (heuristic mode).

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

Open `http://your-host:5959`. A fresh install goes straight into the wizard — there is
nothing to sign in to yet. Step 1 connects your Plex account (that's the sign-in, and it's
what claims the instance for you); from then on Shortlist only opens for that account.

> Set Shortlist up on your own network first. Until you sign in with Plex and link a server,
> anyone who can open the page could claim it as theirs — so don't put it on the public internet
> until you've finished the wizard. Once you've claimed it, it's yours.

The wizard opens on a short welcome screen, then walks (the progress bar reads "step X of 7"):

1. **Connect Plex** — PIN login, pick your server. The capability probe checks your PMS
   version, Plex Pass, and libraries with plain-English results.
2. **Recommendations & history** — where picks come from (TMDB, Trakt, AI, web search). Watch
   history is read straight from Plex, per user, with no setup; Tautulli is optional here, only
   for the friendlier display names it knows people by.
3. **Choose your curator** — Claude / GPT / Gemini / Local (OpenAI-compatible) / **None**.
   Keys are yours, stored encrypted, redacted after save.
4. **Pick your users** — everyone you share with, with history-depth and new-viewer badges.
5. **Make it yours** — row name, row size, and when rows refresh (each row runs on its own
   schedule; there is no single global schedule). The name can be plain text or use a placeholder:
   `{library_name}` (the library — the default `✨ {library_name} Picked for You` becomes "✨ Movies
   Picked for You"), `{user}` (the person's name — e.g. "Sarah's picks"), or `{top_seed}` (their
   current favourite — e.g. "Because you watched {top_seed}").
6. **First run** — live per-user progress; when it finishes, each user has their private row.

## Trying it safely

Shortlist is new and modifies real Plex share permissions, so it's fair to want to watch it before
trusting it. Two ways to de-risk your first run:

- **Safe mode** — start the container with `-e SHORTLIST_DRY_RUN=1`. Every run then logs exactly what
  it _would_ change and writes **nothing** to Plex. Walk the whole flow, read the run activity, and
  only remove the flag (and recreate the container) once you're happy.
- **One user first** — on the Users page, disable everyone except a test account, run, then sign in
  as that account (not the owner — the owner sees every row) and confirm they see only their own row.

The **first real run is the slowest**: it builds every enabled user's rows and merges every account's
share filter. Later runs are much faster — most rows are unchanged and skipped.

Every row is kept private automatically: it's a labeled collection excluded on every other
account's share, delivered hidden and only promoted once those exclusions are in place. Your share
filters are snapshotted before the first change, so **Uninstall** (Settings → Danger Zone) puts them
back exactly as they were. This hiding relies on a PMS ≥ 1.43.2.10687 — older builds ignore the
label exclusion, which is why the wizard surfaces your version before you begin.

## The one honest caveat

You're in the user list too, so you can give yourself a row like anyone else — on a one-person
server that's the whole point.

What Plex cannot do is hide collections from the **server owner**: your own Home shows every user's
row, not just yours. If you share the server with other people and want a clean Home, watch on a
Plex Home user and keep the admin account for administration.
