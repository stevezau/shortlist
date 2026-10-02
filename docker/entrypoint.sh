#!/bin/sh
# Run as the PUID/PGID user (linuxserver-style) so /config files aren't root-owned.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Trust X-Forwarded-Proto/-For so the session cookie gets `Secure` and the PIN rate limiter keys on
# the real client behind a TLS-terminating proxy (Traefik/nginx/Caddy). Default `*` because the
# proxy's source IP inside a Docker network isn't knowable ahead of time. Trade-off: with `*`, a
# client that can reach the container directly can spoof X-Forwarded-For — the cookie flag is still
# fail-safe (only ever ADDS Secure), and the PIN limiter keeps an unspoofable GLOBAL ceiling, but
# set FORWARDED_ALLOW_IPS to your proxy's subnet if you publish the container port outside the proxy
# network and want the per-IP limit and forwarded headers fully trustworthy.
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"
PORT="${PORT:-5959}"

# --timeout-graceful-shutdown is a backstop. uvicorn waits for every open connection before running
# the app's shutdown, and a connection that never ends used to let `docker stop` run out its 10s and
# SIGKILL the container with the scheduler and playback listener never stopped. The app now ends its
# own event streams on the stop signal; this bounds anything else. 3s, plus up to 5s for the
# listener's own shutdown, stays inside Docker's default 10s.
if [ "$(id -u)" = "0" ]; then
    getent group shortlist >/dev/null 2>&1 || addgroup --gid "$PGID" shortlist 2>/dev/null || true
    id shortlist >/dev/null 2>&1 || adduser --uid "$PUID" --gid "$PGID" --disabled-password --gecos "" shortlist 2>/dev/null || true
    mkdir -p /config
    chown -R "$PUID:$PGID" /config
    exec gosu "$PUID:$PGID" uvicorn shortlist.server.main:app --host 0.0.0.0 --port "$PORT" \
        --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS" --timeout-graceful-shutdown 3
fi

exec uvicorn shortlist.server.main:app --host 0.0.0.0 --port "$PORT" \
    --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS" --timeout-graceful-shutdown 3
