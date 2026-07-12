# Rowarr

**A private, AI-curated "✨ Picked for You" row for every user on your Plex server.**

Rowarr watches what each of your users watches, finds similar titles you already own, has an
LLM (or a plain heuristic — no AI required) curate and explain the picks, and puts them on
each user's Plex Home screen as their own private row — visible only to them.

- **Private by design** — per-user label restrictions (Plex Pass, PMS ≥ 1.43.2), proven by a
  built-in Privacy Check before anything real is written, re-verified weekly.
- **Can't hallucinate** — the AI only ranks titles verified to exist in your library.
- **Reversible** — every share-filter change is snapshotted; uninstall provably restores
  your server.
- **Plays nice with Kometa** — Rowarr never touches collections it didn't create.

## Quick start

```yaml
services:
  rowarr:
    image: ghcr.io/stevezau/rowarr:latest
    ports: ["5959:5959"]
    volumes: ["./config:/config"]
    environment:
      - TZ=Etc/UTC
    restart: unless-stopped
```

Open `http://your-host:5959` → **Login with Plex** → the wizard does the rest (~10 minutes
including your first rows).

Docs, source and issues: https://github.com/stevezau/rowarr

Tags: `latest` (releases) · `X.Y.Z` · `dev` (master) · `pr-<n>` (PR previews). amd64 + arm64.
