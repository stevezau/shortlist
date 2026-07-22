# Changelog

All notable changes to this project are documented here. This project follows
[Conventional Commits](https://www.conventionalcommits.org/) and
[Semantic Versioning](https://semver.org/).

## [0.1.0-beta.5] - 2026-07-22

### Fixed

- **The run page really does show where each pick came from now.** There were three places that
  build a pick, and the run page renders the one that was still missing provenance — a stored
  per-(row, library) breakdown, not the picks table. beta.4 fixed the renderer; the data feeding it
  was still blank.
- **Existing runs explain themselves too.** Provenance is joined onto the breakdown from the picks
  rows when a run is read, so runs recorded before this don't stay blank until they're rebuilt. A
  pick with no matching row stays blank rather than being given an invented source.

## [0.1.0-beta.4] - 2026-07-22

### Fixed

- **The run page now shows where each pick came from.** beta.3 added the "suggested by TMDB ·
  loosely related" line, but the run detail page renders its picks with its own component — so the
  line appeared on the user page and nowhere else, including the one screen people open to ask
  exactly that question.

Picks kept from an earlier run still show nothing, which is correct: those were written before
provenance was recorded, so it genuinely isn't known. They gain it the first time they are rebuilt.

## [0.1.0-beta.3] - 2026-07-22

Picks that actually resemble what you watched.

### Ranking

A beta user's row seeded by **The Pitt** — a medical drama — came back as The Sandman, Servant,
Torchwood and King & Conqueror. TMDB was not at fault: its recommendations for that show are ER,
Chicago Med, Grey's Anatomy, Code Black, The Good Doctor. Shortlist was reading the right list and
picking from the wrong end of it.

- **TMDB's ordering is no longer thrown away.** Suggestions were pooled into one bag, so "#1 closest
  match" and "#19, loosely related" arrived indistinguishable — and `/similar` (keyword matching)
  was weighted the same as `/recommendations` (what people actually watch together).
- **Ranking now asks whether a title is similar, not just well-rated.** With position discarded, the
  only thing separating candidates was TMDB's average vote — which on real data put *Traitors*, a
  reality competition show, at the top of a medical drama's row.
- **Genre coherence.** Position alone wasn't enough: TMDB tags The Pitt simply "Drama", as it does
  nearly everything it suggests. But Torchwood and The Sandman are *also* "Sci-Fi & Fantasy", and
  that foreign genre is the whole difference.

Sources with no ranking of their own — discover, Trakt, the AI sources — are unaffected. They are
deliberate picks, not the tail of a list.

### Rows can be short now

Filling a half-empty row from the tail is how a weak association became a delivered title. Padding
now draws only from candidates that are genuinely related, so **a row may come up short** — four
titles that fit beat ten where six are filler. The run log says so, naming the closest rejected
title, so a short row reads as the filter working rather than a failure.

### Where every pick came from

Each pick records the source that surfaced it and how strongly that source vouched, shown under the
title:

```
#3  The Sandman — Because you watched The Pitt
    suggested by TMDB · loosely related
```

Nothing claims a strength it didn't measure: sources that don't rank their suggestions say only who
suggested it. The run log carries the same per row at DEBUG — every pick with its seed, source and
affinity — so a "why did it pick that?" report is answerable from a downloaded log.

### Also

- Release tags now publish `:dev` as well as `:latest` and the version tag — a tag is cut from
  `dev`, so `:dev` was being left a build behind.

## [0.1.0-beta.2] - 2026-07-22

Second beta. Mostly the things the first beta's users ran into.

### The owner is a user now

- **You get a row too.** Shortlist only ever built rows for accounts you *share with*, so on a
  one-person server it did nothing at all — plex.tv's user list never includes the account that owns
  the server ([#1]). The owner is now synced like anyone else, disabled by default so an existing
  install gains a switch rather than a row appearing unannounced. Their watch history is read from
  the PMS local account, which is named after your plex.tv **username**, not your display title.
- **The honest caveat, stated up front.** Plex cannot hide a collection from the server owner, so
  your own Home shows *every* user's row. The app says so where it matters instead of leaving you to
  discover it.

### Say why, not just what

- **Every skip explains itself** ([#3]). "Skipped" used to be the whole message. A run now records
  the reason per person — no watch history yet, no candidates survived filtering, the row's
  libraries don't match their share — and shows it in the run detail.
- **A failed run names the account that blocked it** and what went wrong, rather than
  "promotion skipped — a privacy sync failed this run".
- **A skipped person is no longer counted as a success.** Three skipped users reported as
  "3 succeeded".

### Logs, in the app

- **A Logs view** — filter by level, search, follow live, copy, or download every log file as a zip.
  Built because diagnosing the first beta meant asking people to fish `logs.log` out of a container.
- **Redacted before you ever see it.** Plex tokens, bearer credentials and provider API keys
  (Anthropic, OpenAI including `sk-proj-`/`sk-or-v1-`, Google, xAI, Groq) are stripped from every
  line served, copied, or exported — the whole point of the view is that the output is shareable.

### Rows and users

- **Nicknames** ([#4]) — call someone what they're actually called in a row title, without touching
  their Plex username. The label never moves, so their row stays private. A Tautulli rename now
  renames the collections already on Plex instead of leaving a stale duplicate.
- **Watch history is scoped to the row's own libraries.** A row built from your 4K library was
  seeded by what you watched anywhere, so its picks could be shaped by history from a library that
  row never touches.

### One local-AI provider

- **"Local / OpenAI-compatible" replaces the separate Ollama and OpenAI-compatible options**
  ([#7]). llama.cpp, LM Studio, vLLM, LocalAI, Ollama and OpenRouter all speak the same
  `/v1/chat/completions`, so one provider with a base URL covers all of them. Existing Ollama setups
  migrate automatically. A bare host gains `/v1` for you; **Test** lists your models instead of
  making one generate, so it answers instantly.
- **It now survives the servers it exists for.** The request degrades from OpenAI's strict
  JSON-schema mode to plain JSON mode to neither, since older local builds reject the strict form
  outright; and a blank **Model** resolves to a chat model the server actually reports, rather than
  OpenAI's default (which vLLM and LM Studio reject) or the alphabetically-first name (which on a
  stock Ollama box is an embedding model that cannot chat).

### Also

- The users roster can be re-synced after setup, not only during it.
- Unraid Community Applications template and CA profile.
- CI tests only Python 3.12 — the version the image actually ships.

[#1]: https://github.com/stevezau/shortlist/issues/1
[#3]: https://github.com/stevezau/shortlist/issues/3
[#4]: https://github.com/stevezau/shortlist/issues/4
[#7]: https://github.com/stevezau/shortlist/issues/7

## [0.1.0-beta] - 2026-07-21

First public beta. Everything below ships in this release.

### Personalized rows

- **Engine** — the full nightly pipeline per user: watch history (Tautulli, with a per-user
  fallback to Plex's own history; episodes de-duplicated to distinct shows) → candidate sources →
  heuristic ranking → optional LLM curation → per-user collection delivery → merge-only
  share-filter privacy sync with snapshots.
- **Candidate sources** — TMDB similar, TMDB discover-by-taste, Trakt related titles, "AI suggests
  from your library", and **AI web search** for current/well-reviewed titles (via the curator's own
  web search or an Exa key — the latter also gives a local Ollama model web search).
- **Optional AI curator** — Anthropic / OpenAI / Google / Ollama, with a fetched model picker; or
  **None** (heuristic mode), the default. The curator only ever picks from titles verified to exist
  in your library, and writes the one-line "Because you watched X" reason.
- **Multiple rows + shared rows** — several rows per person and server-wide shared rows, each with
  its own sources, size, libraries, curation style/prompt, audience, schedule, placement, and
  poster.
- **Freshness as a cadence** — rows stay stable and refresh every N days (nightly → fortnightly),
  so a person's row isn't reshuffled every night; unchanged rows skip the Plex write entirely.
- **Row placement** — choose the Plex shelf (Home / library Recommended / both) and position, per
  row; coexists with other shelf-ordering tools.
- **Custom / AI row posters** — upload artwork or generate it from text (with `{user}` /
  `{library_name}` placeholders), reusing your AI key; cached across runs.

### Privacy & safety

- **Leak-safe row privacy** — each row is labelled `shortlist_<userslug>`; a
  `label!=shortlist_<userslug>` exclusion is merged (read-modify-write, never rebuilt) into every
  other account's share filter. Rows are swept/delivered **unpromoted**, exclusions merged, and only
  then promoted — a row is never visible before the exclusion that hides it exists.
- **Provable uninstall** — restores every user's share filters from the snapshot taken before the
  first restriction write, and deletes only `shortlist_*`-labelled collections; dry-run preview.
- **Safe mode** — `SHORTLIST_DRY_RUN=1` forces every run to dry-run (writes nothing to Plex) — try
  it against a real server first.
- **Secrets** — Plex tokens and LLM/API keys encrypted at rest (Fernet), redacted in the UI, never
  logged.

### App

- **Web app** — FastAPI backend (SQLite, APScheduler, SSE) + React SPA: an impact dashboard
  (delivered vs actually-watched hit rate), users, rows, live run activity, requests inbox, and a
  first-run onboarding wizard. Programmatic API token for automation.
- **Login with Plex** — PIN flow, owner-only sessions, CSRF-protected mutations.
- **Requests** — an approval inbox for wanted-but-missing titles, optionally auto-sent to
  Sonarr/Radarr, with a choice of rating source (TMDB, or IMDb/RT/Metacritic/Trakt via MDBList).
  Each entry shows which person and row wanted it and why; a **Sent** log records what went out.
  Rejected titles are never re-queued.
- **Packaging** — multi-arch Docker image (GHCR), compose example, Unraid template, healthcheck,
  PUID/PGID, configurable PMS timeout (`plex.timeout_s`).

### Notes

- The label-based share exclusions require PMS **≥ 1.43.2.10687** (older builds ignore the
  exclusion). The setup wizard shows the server version but never blocks a run over it.
- Collections without a `shortlist_*` label are never modified or deleted (Kometa coexistence).
- Plex cannot hide collections from the **server owner** — the owner's own Home shows every row.
