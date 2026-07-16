#!/bin/sh
# Run as the PUID/PGID user (linuxserver-style) so /config files aren't root-owned.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Trust X-Forwarded-Proto so the session cookie gets `Secure` behind a TLS-terminating proxy
# (Traefik/nginx/Caddy). Default `*` because the proxy's source IP inside a Docker network isn't
# knowable ahead of time; it only ever adds `Secure` to a cookie (fail-safe — no IP-based security
# decision reads these headers). Set FORWARDED_ALLOW_IPS to your proxy's subnet if you publish the
# container port outside the proxy network and want to lock the trust boundary down.
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"
PORT="${PORT:-5959}"

if [ "$(id -u)" = "0" ]; then
    getent group shortlist >/dev/null 2>&1 || addgroup --gid "$PGID" shortlist 2>/dev/null || true
    id shortlist >/dev/null 2>&1 || adduser --uid "$PUID" --gid "$PGID" --disabled-password --gecos "" shortlist 2>/dev/null || true
    mkdir -p /config
    chown -R "$PUID:$PGID" /config
    exec gosu "$PUID:$PGID" uvicorn shortlist.server.main:app --host 0.0.0.0 --port "$PORT" \
        --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS"
fi

exec uvicorn shortlist.server.main:app --host 0.0.0.0 --port "$PORT" \
    --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS"
