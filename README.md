# Rowarr ✨

> A private, AI-curated **"Picked for You"** row for every user on your Plex server.

Rowarr watches what each of your users watches, finds similar titles you already own, has an LLM
curate and explain the picks, and puts them on each user's Plex Home screen as their own private
row — visible only to them. Netflix's killer feature, self-hosted.

**Status: pre-alpha — in design/build.** Nothing to install yet.

## How it will work

1. `docker compose up -d` — one container, one `/config` volume
2. **Login with Plex** → pick your server → pick your users
3. Choose a curator: Claude / GPT / Gemini / Ollama (local) / None (no AI needed)
4. The built-in **Privacy Check** proves rows stay private on _your_ server before anything real is written
5. Every night, each user's row refreshes from their own watch history — with
   "because you watched …" reasons

Key properties:

- **Can't hallucinate** — the AI only ranks titles verified to exist in your library
- **Private by design** — per-user label restrictions (Plex Pass, PMS ≥ 1.43.2), verified weekly
- **Reversible** — every change is snapshotted; uninstall provably restores your server
- **Works without AI** — heuristic mode needs zero API keys

## Design documents

- [Product & UX design](.claude/docs/rowarr-design.md)
- [Architecture & execution plan](.claude/docs/rowarr-architecture.md)

## License

MIT © Steven Adams
