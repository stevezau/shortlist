# Rowarr ✨

> A private, AI-curated **"Picked for You"** row for every user on your Plex server.

[![CI](https://github.com/stevezau/rowarr/actions/workflows/ci.yml/badge.svg)](https://github.com/stevezau/rowarr/actions/workflows/ci.yml)
[![Docker](https://github.com/stevezau/rowarr/actions/workflows/docker.yml/badge.svg)](https://github.com/stevezau/rowarr/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![AI-Assisted](https://img.shields.io/badge/AI-assisted%20development-8A2BE2)

Rowarr watches what each of your users watches, asks an LLM to curate what they should watch
next **from what you already own**, and puts it on their Home screen — privately,
automatically, every night. Netflix's killer feature, on Plex, without Plex's involvement.

## Why this couldn't exist before 2026

Per-user private collections were impossible until Plex fixed label restrictions on
Home/Recommended (v1.43.1) and Related hubs (v1.43.2). Rowarr is built on that fix — and
**proves it works on your server** with a built-in Privacy Check before writing anything real.

## Features

- 🔒 **Private by design** — each user's row is a labeled collection excluded on the share of
  every other account on your server (one row per library, since Plex filters per library).
  Verified by probe before first write, re-verified weekly, snapshotted and reversible.
- 🧠 **AI that can't hallucinate** — the LLM (Claude / GPT / Gemini / local Ollama) only
  re-ranks titles verified to exist in your library. **Works with zero AI** too (heuristic
  mode) — no keys required.
- 💬 **Explainable** — every pick carries "Because you watched X"; every Plex write lands in
  an audit feed you can read.
- 📥 **Fills its own gaps (optional)** — when a great pick isn't in your library yet, Rowarr can
  ask **Radarr/Sonarr** to grab it. Off by default and deliberately cautious: only a few per
  night, and only titles that are both highly rated and widely reviewed.
- 🧹 **Kometa-friendly** — Rowarr never touches collections it didn't create.
- ↩️ **Provable uninstall** — one flow restores your server exactly as Rowarr found it.
- 📦 **Homelab-native** — one container, `/config` volume, dark UI, GHCR multi-arch,
  healthcheck, Unraid template.

## Quick start

```bash
mkdir rowarr && cd rowarr
curl -fsSLO https://raw.githubusercontent.com/stevezau/rowarr/master/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
docker compose up -d
# open http://your-host:5959 → the wizard (connect Plex is step 1) → ~10 min to first rows
```

Requirements: PMS ≥ 1.43.2.10687 · Plex Pass on the admin account · a free TMDB key.
Optional: Tautulli, an LLM key. Details in [Getting started](docs/getting-started.md).

## Documentation

|                                            |                                          |
| ------------------------------------------ | ---------------------------------------- |
| [Getting started](docs/getting-started.md) | Install, wizard, first run               |
| [Guides](docs/guides.md)                   | UI tour, schedules, CLI, troubleshooting |
| [Reference](docs/reference.md)             | Settings, API, env vars                  |
| [FAQ](docs/faq.md)                         | Privacy model, Kometa, uninstall         |

## CLI (headless / cron mode)

The same engine without the web app — see [Guides → CLI](docs/guides.md#the-cli).

## License

MIT © Steven Adams
