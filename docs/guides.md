# Guides

## The web interface

- **Dashboard** — privacy badge (last verified), next scheduled run, per-user cards with
  their current row, hit rate, and a Run now button. Live-updates during runs.
- **Users** — enable/disable, per-user overrides (row name, size, excluded genres, max
  rating), pause, and each user's restriction status.
- **Runs** — every run with per-user diffs ("added X, removed Y (watched ✓)"), errors as
  first-class rows with copy-for-GitHub buttons, LLM token usage.
- **Settings** — every connection re-testable in place; schedule editor (cron for power
  users); label prefix, plex.tv throttle and log level under Advanced; the Danger Zone.

## Schedules

Default is nightly at 03:30 server-local. Settings → Schedules takes a full cron expression
(`30 3 * * *`) with a human-readable preview. Per-user cadence overrides live on each user's
detail page.

## Hit rate

The % of recommended items a user actually watched within 30 days, computed from the same
history source that feeds recommendations. It's Shortlist's own proof of value — visible
globally and per user. Expect ~20-40% on engaged users after a few weeks.

## Requests (Radarr / Sonarr)

Off by default. When on, Shortlist notices the titles the curator surfaced that your library doesn't
have yet, and asks Radarr (movies) or Sonarr (shows) to grab a few of the best ones on each run.

Set it up under **Settings → Requests**:

1. Turn on **Fill in the gaps automatically**.
2. For each app, paste its **address** (e.g. `http://localhost:7878` for Radarr,
   `http://localhost:8989` for Sonarr) and **API key** (found in the app under _Settings →
   General_), then click **Test connection**. Save.
3. Once connected, pick a **Quality** profile and a **Save to** folder from the dropdowns — Shortlist
   reads these straight from the app, so there are no ids to look up.
4. Tune the **Guardrails**: a minimum rating and minimum number of reviews a title must clear, the
   fewest people who must want it, and the most titles to auto-request per night (a hard cap across
   both apps).
5. Set the **Auto-send vs. ask me** bar: titles wanted by enough people _and_ rated highly enough
   are requested automatically each night; everything else that clears the guardrails waits in your
   **Requests** inbox. Turn auto-send off for a fully manual queue.
6. Optionally set a **tag** (default `shortlist`). Every title Shortlist requests gets this tag in
   Radarr/Sonarr — created there if it doesn't exist — so you can filter, find, or hang tag-based
   rules (quality/release/cleanup) on exactly what Shortlist added. Leave blank for no tag.

### The Requests inbox

The **Requests** tab (in the sidebar) is your approval queue. Each run adds the wanted-but-missing
titles it didn't auto-send — with the title, year, rating, and how many people wanted it. Tick the
ones you want and click **Send to Sonarr/Radarr**, or **Reject** the rest. Rejected titles are never
re-queued, and a title already in the library stops appearing on its own. Sent and dismissed titles
move to **Already handled** so you can see what you've actioned.

It stays cautious on purpose. Missing titles are deduplicated across all your users — three people
wanting the same one is a single entry, and multi-person demand ranks it higher and can push it over
the auto-send bar. A title already in Radarr/Sonarr is skipped, never re-added, and a dry-run only
logs what it _would_ ask for. Every request (and every skip) is recorded in the audit feed, and the
run's detail page shows how many titles it requested.

Requires Radarr v3+ / Sonarr v4+ reachable from the Shortlist container.

## The CLI

The same engine, no web UI — useful for cron-driven setups and CI smoke tests:

```bash
shortlist --config-dir /config run --dry-run   # log every would-be change, write nothing
shortlist --config-dir /config run             # the nightly pipeline
shortlist --config-dir /config verify          # T1 read-back + T2 canary view
shortlist --config-dir /config verify --probe  # full probe (throwaway collection, ~90s)
shortlist --config-dir /config uninstall       # restore snapshots, delete rowarr collections
```

`run` refuses real writes unless a passing `verify` is on record from the last 7 days —
that's deliberate.

## Troubleshooting

- **A user says they can see someone else's row** — run `verify` immediately; if T1 fails it
  names the user and the missing exclusion. Re-running `run` re-merges filters. Check
  whether the share was edited by hand in plex.tv (Shortlist re-merges but never deletes
  foreign filter conditions).
- **Rows not appearing for anyone** — promoted rows land in Plex's hub order; users may
  need to scroll, or pin the row via "Manage Home Screen" on their client.
- **Tautulli shows fewer watches than expected** — Tautulli only knows sessions it observed
  live. Shortlist automatically falls back to Plex's own history per user when Tautulli's
  answer is thin.
- **Everything broke, get me out** — `shortlist uninstall` (or Settings → Danger Zone →
  Uninstall) restores every user's share filters from the pre-Shortlist snapshots and deletes
  every rowarr-labeled collection. Kometa and other tools' collections are never touched.
