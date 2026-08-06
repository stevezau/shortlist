# Changelog

All notable changes to this project are documented here. This project follows
[Conventional Commits](https://www.conventionalcommits.org/) and
[Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-08-05

A minor release with **one behaviour change** — read the first item below before upgrading if any of
your rows are set to 0% already-watched, which is the default.

### Changed

- **A row set to 0% already-watched now also excludes shows someone has only STARTED.** It used to
  exclude only shows they had _finished_, where "finished" meant 80% of the episodes or a
  length-scaled floor of about three. So a series you were two episodes into was, as far as the row
  was concerned, a fresh discovery — and could be recommended straight back to you. That was
  reported, and it was doing exactly what it was told.

  Plex draws no such line: its own watched filter returns a series from its first episode. A probe of
  a real server found it returning shows as little as 1.1% watched (2 of 176), and on that server
  five of ten started shows were still eligible to be suggested to the person watching them. At 0%,
  started now means watched — the two agree.

  **Rows above 0% are unchanged.** There the percentage is still a ceiling on _finished_ titles,
  because "N% of the row" needs a definite line to mean anything. If you relied on the old
  behaviour, set the cap above 0. Rows re-pick their titles on their own refresh night, so the change
  reaches a row when it next rebuilds rather than immediately.

- **"Only series they haven't started" is now offered on a films-and-shows row**, not just a
  shows-only one. The API and the engine always accepted it there; the switch was hidden, so the
  default row most installs have could never turn it on. On a 0% row it is now a no-op — that row
  excludes started series anyway — and it still does real work on a row whose cap is above 0%.

### Added

- **"Have an issue?"** — a new page, replacing the sidebar's separate _Report a bug_ and _Copy
  diagnostics_ buttons. It runs twenty-one read-only checks against your own server and, for most
  problems, names the cause outright: which library refused someone's token, which setting actually
  applied, why a row is short, whether Plex still matches what Shortlist thinks it delivered.

  Nothing on the page changes anything — not your Plex server, not your rows, not your settings. The
  checks stay off until you switch them on and switch themselves off again after 24 hours, because
  they read share filters and per-user tokens. Every check has a **Copy for support** button, and
  what it copies is shown on screen first so a paste holds no surprises. The last section files the
  report: a pre-filled GitHub issue plus a diagnostic to attach, as a paste or a file. That
  diagnostic masks credentials, IP addresses and your server's machine id. It is a good first pass
  rather than a guarantee: logs carry whatever a dependency decided to print, so give the report a
  skim before posting it publicly. It does name the people on your server by their Plex username —
  replace those yourself if you'd rather not publish them.

  If someone reports a problem on a server you don't administer, sending them there is usually faster
  than a list of questions.

### Fixed

- **A downloaded log zip no longer carries your server's IP address or machine id.**
  `Logs → Download` calls itself "the attachment for a bug report" and had only ever removed
  credentials — so it shipped your Plex server's address on every line that recorded a call to it
  (17,234 of them in one real export) and, where a collection write was logged, the machine id that
  identifies your server. Both are now removed there and in the diagnostic report, which had the same
  gap in the log files it bundles.

  A URL keeps its scheme and port (`https://<host>:32400`) because those are the parts that answer a
  question. The live Logs view is deliberately unchanged: it renders on your own screen, where your
  server's address is what makes a line readable.

- The documentation described a `recommendations.watched_show_pct` setting that has never existed.
  The finished threshold is fixed in the engine; the docs now say so.

## [1.1.0] - 2026-08-05

A minor release: one new setting, four fixes reported by v1 users, and no breaking changes. An
existing install upgrades in place and behaves exactly as it did until you change the new setting.

### Added

- **You can now choose what someone with too little watch history gets** ([#66](https://github.com/stevezau/shortlist/issues/66)).
  Until now they always got a row of the server's highest-rated titles. That is still the default,
  but **Settings → Finding titles → When someone hasn't watched enough** can now be set to
  **Don't build their row** instead — no row is created, and any row they already have is removed,
  so "skip" means gone rather than left to go stale. It comes back on its own the night they cross
  the threshold.

  **Any row can override this in its editor**, which is the point of having it per row. A
  `{top_seed}` row ("Because you watched X") is the one worth skipping: it has no favourite to name
  itself after, so for these people it quietly falls back to a plain title. A general "Picked for
  you" row is perfectly happy holding popular titles in the meantime.

- **Where that line sits is now yours to set.** **Enough watch history** (default 10 titles) was
  fixed in the engine and unreachable; owners of small or new servers can lower it.

### Fixed

- **A run in progress no longer disagrees with itself about when it started**
  ([#67](https://github.com/stevezau/shortlist/issues/67)). "Started 8m ago" sat beside a duration
  of "10m 54s" — the two cells read the same timestamp against the same clock, but were evaluated at
  different moments. They now share one.
- **A first run no longer dies because the container started before its network did.** The one
  plex.tv read whose failure aborts a whole run had about three seconds of retry, which is not
  enough to outlast a slow network stack; it now gets about thirty. Every other read is unchanged,
  and a run still fails loudly when plex.tv is genuinely gone.
- **The Rows page now says why the admin's Collections tab lists everyone's rows.** The answer
  existed but only covered the Recommended shelf, and only while that setting was on — an owner who
  found the rows in Plex's Collections tab had nothing in front of them.
- **The "How we picked" button no longer strands mid-header** on a person whose display name is long
  enough to wrap the run card's header.

### Changed

- A muted row whose title depends on its picks (a `{top_seed}` row) is now genuinely removed from
  Plex. It could not be matched by title, so it survived every run — private, but never actually
  gone. Muting has always meant "gone"; now it is.

## [1.0.0] - 2026-08-04

First stable release. No breaking changes from `0.1.0-beta.9` — the version number is a statement
about stability, not a rewrite. An existing install upgrades in place; the migrations run on boot
after taking a pre-migration backup.

### Added

- **A "Because you watched" row can cycle its seed** instead of sitting on the newest watch for
  weeks, so the row keeps moving even when someone's viewing does not.
- **The row editor is a page**, showing what the row will actually do — and whether it is working —
  beside the settings that decide it. Rows can be renamed and deleted from there.
- **A way back from Off**, and a request filter that reaches past the 500-row inbox cap: picking a
  name under "Wanted by" now asks the SERVER, so it searches every title on file rather than the
  page that happened to load.
- **Two new row orders, for when the front of a row feels stuck** ([#63](https://github.com/stevezau/shortlist/issues/63)):
  **Just added** puts whatever is new to the row at the front, and **Taking turns** advances the
  front by one title a day so every pick gets a spell there. Both are presentation only — neither
  changes which titles a row holds, or how often it refreshes, which is still **Freshness**.
  "Newest" is now labelled **Newest released**, to keep it distinct from "Just added".
- **A rebuilt documentation site**, split into eight task-shaped guides.
- **The row editor leads with how the row is doing** — delivered, watched, runs and last built,
  across the top. Runs links to that row's own history, and counts the runs that list actually
  holds, so the number and the page behind it can never disagree.

### Fixed

- **An un-watch is noticed within the night's read** rather than waiting up to a week for the full
  re-read. Only when the read proves it covered its window — a truncated walk deletes nothing, so a
  PMS that omits `totalSize` can never be mistaken for "they un-watched everything".
- **A library removed from the server no longer counts as watched for ever.** Its cached titles and
  cursor are swept on the weekly pass. A library that is merely unshared is left alone — that
  history is still true.
- **`fetch_section` raises instead of returning an empty set** when plex.tv will not mint a token.
  Reported as "nothing watched", it made a full read wipe the section and stamp the sync a success.
- **The dashboard and Rows pages no longer scroll sideways on a phone** (134px and 184px past a
  390px screen; Rows put the Delete button out of reach entirely). Every route, the wizard, the nav
  drawer and every dialog now fit 390px, enforced by `tests/e2e/test_mobile_audit.py`.
- **The drift check's off switch now turns it off.**
- **Pages no longer wait on Plex.** The library list behind every row card was read live from the
  server on each page load, so while a job was deleting collections — one DELETE took 15.8s inside
  Plex's own write lock — every page queued behind it. It is cached now, concurrent misses collapse
  into one read, and a failed refresh serves the last good answer instead of an error.
- **Turning a row off asks first.** The next run takes that row off Plex for everyone who has it,
  which a bare switch gave no hint of. Turning one back on is unchanged — it removes nothing.
- **Rename only offers itself when the name has changed**, and now does the rename rather than
  showing you another button to press. Pressed on an unchanged name it used to rewrite every
  collection, for every person, to the name they already had.
- Warnings look like warnings, small grey text is readable, and the settings are explained in words
  a first-time user already knows.

### Changed

- The read-only Plex audit moved out of the Danger zone into Advanced — it changes nothing, and
  filing it under a destructive heading made the safest control on the page look like the riskiest.
- Renaming moved off the Rows list and into the editor, beside the name it changes.

## [0.1.0-beta.9] - 2026-08-02

### Added

- **Rows choose their own order.** Best match (the ranking, unchanged and still the default),
  Highest rated, Newest, or Shuffled. Plex only sorts a collection by release date, alphabetically,
  or by a custom order, so every one of these is applied by Shortlist and written as that custom
  order — which is what the Home row displays.
- **Highest rated can use IMDb, Trakt, Rotten Tomatoes or Metacritic** instead of TMDB, via MDBList
  (Settings → Finding titles → "Rated by", also editable straight from the row editor). TMDB needs no
  setup and costs no lookups. Without an MDBList key, or once its daily quota is spent, a row falls
  back to TMDB for its whole ordering rather than sorting half of itself on one scale and half on
  another.

### Fixed

- **A "Because you watched X" row now follows the watch it names.** Its title renders from the top
  pick, and every refresh carried that pick forward — so the row stayed named after the first thing
  that ever seeded it while newer watches quietly filled its tail. It now rebuilds around the new
  watch and renames itself when the seed moves. That template also refreshes nightly, since an
  eight-day cadence kept it naming last week's film for a week.
- **The top of a row moves again.** A refresh kept the strongest two-thirds pinned to the head, so on
  a 20-title row thirteen positions could never change however the candidates scored. Survivors and
  newcomers are now ranked together, so a better new suggestion can reach the front.
- **The library picker no longer ticks libraries a row never builds in.** An empty selection means
  "every library of this row's type"; it was drawn as every library, so a movies-only row showed its
  TV ones ticked — and touching them flipped the row to cover both, which on a one-seed row silently
  built an empty collection.

### Changed

- **The row editor is five decisions instead of nineteen.** Name, who gets it, order, schedule and
  size stand alone; artwork, what it draws on, where it appears and requests fold away, each
  captioned with its current values so a closed section still answers "is what I want in here?".
- Several settings now say what they do: "Make this a 'watch it again' row" (was "Lead with things
  they've seen"), "Watches the AI searches from" (was "Recent watches to search", and it is hidden on
  rows that do not use AI web search, where it did nothing), and freshness now says it decides _which_
  titles a row holds rather than the order they appear in.

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
  only thing separating candidates was TMDB's average vote — which on real data put _Traitors_, a
  reality competition show, at the top of a medical drama's row.
- **Genre coherence.** Position alone wasn't enough: TMDB tags The Pitt simply "Drama", as it does
  nearly everything it suggests. But Torchwood and The Sandman are _also_ "Sci-Fi & Fantasy", and
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

- **You get a row too.** Shortlist only ever built rows for accounts you _share with_, so on a
  one-person server it did nothing at all — plex.tv's user list never includes the account that owns
  the server ([#1]). The owner is now synced like anyone else, disabled by default so an existing
  install gains a switch rather than a row appearing unannounced. Their watch history is read from
  the PMS local account, which is named after your plex.tv **username**, not your display title.
- **The honest caveat, stated up front.** Plex cannot hide a collection from the server owner, so
  your own Home shows _every_ user's row. The app says so where it matters instead of leaving you to
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
