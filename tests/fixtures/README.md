# Recorded API fixtures

Response shapes recorded from real servers (plex-safety rule 11). Identifying values
(names, ids, tokens, hostnames) are sanitized; structure and field names are verbatim.

| File                 | Source                                                                                                                                                                          | Recorded   |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| `pms_hubs_home.json` | PMS 1.43.3 `GET /hubs` (JSON) — reconstructed from the live Phase 0 probe observations (collection hub `key`/`context` shapes); re-record with a direct capture when convenient | 2026-07-12 |
| `plextv_users.xml`   | plex.tv `GET /api/users` — field-verified live in Phase 0 (share filters as `<User>` attributes)                                                                                | 2026-07-12 |
