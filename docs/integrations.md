---
title: "What Shortlist works with: Plex, Radarr, Sonarr and more"
description: Every service Shortlist can talk to — Plex, TMDB, Sonarr, Radarr, Overseerr, Jellyseerr, Trakt, MDBList, Exa, SearXNG, Tautulli and the AI providers — what each one adds, and which are actually required.
heading: What it works with
updated: 2026-10-03
---

**Two things are required: Plex, and a free TMDB key.** Everything below that is optional, and
Shortlist runs with none of it — no AI account, no download apps, no paid anything.

## Required

| Service      | What it does                                        | What you need                                  |
| ------------ | --------------------------------------------------- | ---------------------------------------------- |
| **Plex**     | Where the rows appear, and where watch history comes from | Plex Pass, and Plex Media Server 1.43.2 or newer |
| **TMDB**     | Finds titles similar to what someone watched        | A free API key — no card, takes a minute        |

Plex Pass is not optional. Private per-person rows depend on a sharing feature Plex only exposes to
Plex Pass servers, so without it Shortlist cannot keep one person's row off everyone else's screen.

## Where extra suggestions come from

| Service      | What it adds                                             | What you need                                    |
| ------------ | -------------------------------------------------------- | ------------------------------------------------ |
| **Trakt**    | Titles TMDB misses, from its own lists                    | An API key, which now needs Trakt's paid VIP plan |
| **MDBList**  | Ratings from IMDb, Rotten Tomatoes, Metacritic and Trakt  | A free API key                                    |

## Who writes the reason next to a pick

Any one of these, or none — with none, Shortlist still writes a plain-English reason itself.

| Service                                              | What you need                        |
| ---------------------------------------------------- | ------------------------------------ |
| **Claude, GPT, Gemini**                               | An API key for that provider          |
| **Ollama, llama.cpp, LM Studio, vLLM, LocalAI**       | A model you run yourself — no key     |

If you want a model to search the web before it answers:

| Service                        | What you need                                              |
| ------------------------------ | ---------------------------------------------------------- |
| **Your provider's own search** | Nothing extra — Claude, GPT and Gemini can search on their own |
| **Exa**                        | A hosted search API and an account                          |
| **SearXNG**                    | Search you host yourself. No account, no key.               |

A model you run yourself cannot search on its own, so it needs Exa or SearXNG. SearXNG is the option
with nothing to sign up for.

## Getting missing titles

When a row wants something the server doesn't have, Shortlist can ask for it. Pick one route:

| Service                      | What it does                                                                   |
| ---------------------------- | ------------------------------------------------------------------------------ |
| **Radarr / Sonarr**          | Shortlist adds the title directly, using your quality profile and root folder    |
| **Overseerr / Jellyseerr**   | Shortlist hands the request over and lets them decide — their profiles, their approvals |

Overseerr and Jellyseerr are the same integration; Jellyseerr is a fork and speaks the same API.
Use one route or the other, not both. [Set it up →](/guides/requests/)

Overseerr is also read the other way round: a **Your requests** row shows each person the titles
they asked for in Overseerr (or that carry their requester tag in Radarr/Sonarr) once they're on Plex,
until they've watched them. [How the row works →](/guides/requests/#your-requests-rows)

## Everything else

| Service       | What it does                                                                    |
| ------------- | -------------------------------------------------------------------------------- |
| **Tautulli**  | Optional. Only used for friendlier display names — Shortlist reads watch history from Plex directly |
| **A webhook** | Optional. Posts JSON to a URL you choose when something you picked happens, so an overnight failure isn't silent |

The webhook is a JSON POST that works with Discord, Slack, Home Assistant, n8n or anything that
accepts one. Add its address on the **Webhook** card in Settings → Connections, then turn it on and
tick what to send in Settings → Connections → Webhook: runs starting, finishing or failing, jobs starting,
finishing or failing, someone able to see a row that isn't theirs, titles waiting for your approval,
and new versions. A failed run and a privacy problem are ticked to begin with. No message names
anybody.

If your receiver needs a key (ntfy, Gotify, n8n's header auth), give a header name and value on the
same card. It is sent with every message, the test included. Clear the header name to stop sending it,
or remove the card to take the address and the header off together.

## What Shortlist does not work with

**Jellyfin and Emby.** The privacy mechanism is built on a Plex sharing feature that has no
equivalent in either, so there is no version of this that works there.
