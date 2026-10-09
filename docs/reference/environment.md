---
title: "Reference: environment variables and files"
description: The container's environment variables and which are live or one-time seeds, serving Shortlist from a subpath behind a reverse proxy, and the files it keeps under /config.
heading: Environment and files
updated: 2026-10-05
---

Everything else is set in the app and stored in its database; see the
[Settings reference](settings.md).

## Environment variables (container)

| Variable                                                                   | Default   | Live or seed                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------------------------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PORT`                                                                     | `5959`    | live                                                                                                                                                                                                                                                                                                              |
| `TZ`                                                                       | `Etc/UTC` | live                                                                                                                                                                                                                                                                                                              |
| `PUID` / `PGID`                                                            | `1000`    | live                                                                                                                                                                                                                                                                                                              |
| `SHORTLIST_CONFIG`                                                         | `/config` | live                                                                                                                                                                                                                                                                                                              |
| `PLEX_URL`, `PLEX_TOKEN`, `TAUTULLI_URL`, `TAUTULLI_APIKEY`, `TMDB_APIKEY` | —         | **seed once**: copied into settings on first boot, ignored afterwards                                                                                                                                                                                                                                             |
| `LOG_LEVEL`                                                                | `DEBUG`   | **seed once**: initial value for the `log.level` setting; change it live in Settings → System                                                                                                                                                                                                                   |
| `SHORTLIST_DRY_RUN`                                                        | unset     | live: when set (`1`/`true`), EVERY run is forced to dry-run. The app builds its clients and logs the would-be changes but writes NOTHING to Plex/plex.tv. Safe mode for a demo/test instance pointed at a real server (even a manual "Run now" can't modify it)                                                   |
| `SHORTLIST_ENABLE_DOCS`                                                    | unset     | live: when set (`1`), exposes the API docs at `/api/docs` and `/api/openapi.json` (off by default)                                                                                                                                                                                                                |
| `SHORTLIST_MCP_URL`                                                        | unset     | live: enables assistant access at the exact canonical URL, including `APP_BASE_PATH` and the final `/mcp`; for example `https://media.example.com/shortlist/mcp`. Requires HTTPS outside loopback. Read at startup. See [Connect an assistant](../guides/assistant-access.md).                                      |
| `APP_BASE_PATH`                                                            | `/`       | live: serve the app from a subpath behind a reverse proxy, e.g. `/shortlist`. Read at startup, so the published image works unmodified — no rebuild. Accepts `/shortlist` or `/shortlist/`. The proxy just forwards; it does not need to strip the prefix. See [Serving from a subpath](#serving-from-a-subpath). |

`GIT_SHA` and `GIT_BRANCH` are build arguments baked into the image so the version check can tell which commit it is running. They are build metadata, not settings, and there is nothing to configure.

### Serving from a subpath

Set `APP_BASE_PATH` and point the proxy at the container. No rebuild, and no prefix-stripping
middleware — the app recognises its own prefix, rewrites the asset URLs in the shell it serves, and
publishes the prefix to the SPA so the router and API client use it too.

In the compose file from [Getting started](../getting-started.md#install-docker), add it under
`environment:`:

```yaml
    environment:
      - APP_BASE_PATH=/shortlist
```

Traefik — router and service only:

```yaml
http:
  routers:
    shortlist:
      rule: "Host(`media.example.com`) && PathPrefix(`/shortlist`)"
      service: shortlist
  services:
    shortlist:
      loadBalancer:
        servers:
          - url: "http://shortlist:5959/"
```

nginx — two blocks. `location /shortlist` on its own is a _prefix_ match, so it would also
swallow a sibling app at `/shortlistings`; `^~ /shortlist/` matches only the real subtree, and the
exact-match block redirects the bare URL people actually type. Note there is no trailing slash on
`proxy_pass` — that is what forwards the prefix intact:

```nginx
location = /shortlist {
    return 308 /shortlist/;
}

location ^~ /shortlist/ {
    proxy_pass http://shortlist:5959;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

A proxy that strips the prefix anyway still works — an already-stripped path simply doesn't match
and is passed through untouched. That is also why the container's own healthcheck, which requests
an unprefixed `/api/system/health` on localhost, keeps working.

Leaving it unset serves the shell's bytes exactly as they shipped.

Test through the proxy, at `https://host/shortlist/`. A blank page at the container's own port —
or at any URL without the prefix — is expected, not a fault: the app still answers there (that is
how the healthcheck works), but the SPA it serves is built for the prefix and renders nothing
outside it.

If it is blank _through the proxy_, check the container log first: it states the base path it is
using at startup, and warns if `APP_BASE_PATH` held something it could not use (a query, a
fragment, a space, an escaped or relative path) — in which case it ignores it and serves from the
root, which on its own looks exactly like a proxy problem.

When assistant access is enabled under a base path, MCP clients also request origin-root discovery
URLs. The exact two paths and proxy requirements are listed under
[Reverse proxy routes](../guides/assistant-access.md#reverse-proxy-routes).

### Client-side stdio bridge variables

These belong to the client process that launches `shortlist-mcp-stdio`; they are not alternative
server settings:

| Variable                       | Meaning                                                                                  |
| ------------------------------ | ---------------------------------------------------------------------------------------- |
| `SHORTLIST_MCP_URL`            | The same canonical `/mcp` URL configured on the Shortlist server                         |
| `SHORTLIST_MCP_CREDENTIAL`     | A named `shla_` local credential copied once from **AI assistants**; treat as a secret |

The bridge never accepts a credential in the URL or as a command argument.

## Files under /config

`shortlist.db` (SQLite: settings, users, runs, restriction snapshots, and the
durable plex-account-id → slug map a row's label is built from) · `secret.key` (Fernet, 600) ·
`session.secret` · `logs/`.
