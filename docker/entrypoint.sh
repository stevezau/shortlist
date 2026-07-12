#!/bin/sh
# Run as the PUID/PGID user (linuxserver-style) so /config files aren't root-owned.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    getent group rowarr >/dev/null 2>&1 || addgroup --gid "$PGID" rowarr 2>/dev/null || true
    id rowarr >/dev/null 2>&1 || adduser --uid "$PUID" --gid "$PGID" --disabled-password --gecos "" rowarr 2>/dev/null || true
    mkdir -p /config
    chown -R "$PUID:$PGID" /config
    exec gosu "$PUID:$PGID" uvicorn rowarr.server.main:app --host 0.0.0.0 --port "${PORT:-5959}"
fi

exec uvicorn rowarr.server.main:app --host 0.0.0.0 --port "${PORT:-5959}"
