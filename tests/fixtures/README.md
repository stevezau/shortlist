# Recorded API fixtures

Response shapes recorded from real servers (plex-safety rule 11). Identifying values
(names, ids, tokens, hostnames) are sanitized; structure and field names are verbatim.

> **XML fixtures must NOT use a bare `.xml` extension — use `.xml.txt`.** Unraid Community
> Applications scans every `*.xml` file in the repo looking for app templates, and flags any that
> isn't one as `not_unraid_application`, which shows as a failed check on our CA submission. The
> content is still plain XML; only the filename differs.

| File                   | Source                                                                                                                                                                          | Recorded   |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| `pms_hubs_home.json`   | PMS 1.43.3 `GET /hubs` (JSON) — reconstructed from the live Phase 0 probe observations (collection hub `key`/`context` shapes); re-record with a direct capture when convenient | 2026-07-12 |
| `plextv_users.xml.txt` | plex.tv `GET /api/users` — field-verified live in Phase 0 (share filters as `<User>` attributes)                                                                                | 2026-07-12 |
| `plextv_resources.json` | plex.tv `GET /api/v2/resources?includeHttps=1` — the shape behind `auth.owned_machine_ids()`, which decides who may sign in to an unclaimed instance and which server they may link. Captured owned entry is real; the `owned:false` and player entries are synthesised (this account has no inbound shares) and labelled as such in `_why` | 2026-07-29 |
| `pms_watched_incremental.xml.txt` | PMS 1.43.3 `GET /library/sections/{key}/all` — the watched read, plus a measured table of which query params this server honours. The headline: `unwatched=0` and `sort=lastViewedAt:desc` work, while **every cutoff-filter form (`lastViewedAt>=`, `>>=`, and a `year>>=` control) is silently ignored** and returns the full set with a 200. That is why the incremental watch-history read narrows by ordering + an early stop rather than by a filter | 2026-07-30 |
