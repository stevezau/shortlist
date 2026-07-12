# Getting started

## Requirements

- **Plex Media Server ≥ 1.43.2.10687** — earlier versions leak labeled collections on
  Home/Recommended/Related. Rowarr checks this and refuses to write on older servers.
- **Plex Pass** on the server owner's account (label restrictions are a Pass feature).
- A **TMDB API key** (free: themoviedb.org → Settings → API).
- Optional: **Tautulli** for deeper watch history; **an LLM API key** (Anthropic/OpenAI/Google)
  or a local **Ollama** — Rowarr is fully functional with none of these (heuristic mode).

## Install (Docker)

```bash
mkdir rowarr && cd rowarr
curl -fsSLO https://raw.githubusercontent.com/stevezau/rowarr/master/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
docker compose up -d
```

Open `http://your-host:5959`. A fresh install goes straight into the wizard — there is
nothing to sign in to yet. Step 1 connects your Plex account (that's the sign-in, and it's
what claims the instance for you); from then on Rowarr only opens for that account.

> Don't expose an unconfigured Rowarr to the internet: until you link a server, whoever
> reaches it first can claim it. Once claimed, it's yours.

The wizard then walks:

1. **Connect Plex** — PIN login, pick your server. The capability probe checks your PMS
   version, Plex Pass, and libraries with plain-English results.
2. **History source** — point at Tautulli if you run it; Plex's own history works without it.
3. **Choose your curator** — Claude / GPT / Gemini / Ollama / **None**. Keys are yours,
   stored encrypted, redacted after save.
4. **Pick your users** — everyone you share with, with history-depth and cold-start badges.
5. **Privacy Check** — Rowarr creates a throwaway labeled collection, hides it from a canary
   account, and verifies with that account's own view that it's gone. ~90 seconds, fully
   cleaned up. **Nothing real is written until this passes.**
6. **Make it yours** — row name (static or "Because you watched {top_seed}"), row size, schedule.
7. **First run** — live per-user progress; when it finishes, each user has their private row.

## The one honest caveat

Plex cannot hide collections from the **server owner** — your own Home shows every user's
row. Watch on a separate (Home) account if that bothers you.
