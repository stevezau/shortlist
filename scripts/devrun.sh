#!/bin/bash
# Run the app from the working tree for hands-on iteration, without touching a real deployment.
#
# Defaults are deliberately paranoid: a throwaway config directory, safe mode on, and a port that is
# NOT the one a deployed container uses. Override with the env vars below when you mean to.
#
# Usage:  bash scripts/devrun.sh
set -euo pipefail

readonly PORT="${PORT:-5960}"
readonly CONFIG="${SHORTLIST_CONFIG:-/tmp/shortlist-dev}"
readonly DRY_RUN="${SHORTLIST_DRY_RUN:-1}"
readonly PYTHON="${PYTHON:-.venv/bin/python}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -x "${PYTHON}" ]]; then
  echo "error: no interpreter at ${PYTHON} — create one, or set PYTHON=..." >&2
  exit 1
fi

# The app starts APScheduler and runs Alembic to head on boot. Against a config directory that a
# deployed container is also using, that means two schedulers writing to one database (duplicated
# real writes to people's Plex accounts) and a silent migration of live data by whatever branch
# happens to be checked out. Refuse rather than warn.
if [[ -S "/var/run/docker.sock" ]] && command -v docker >/dev/null 2>&1; then
  while read -r name; do
    [[ -z "${name}" ]] && continue
    in_use=$(docker inspect "${name}" --format '{{range .HostConfig.Binds}}{{println .}}{{end}}' 2>/dev/null |
      awk -F: -v c="$(readlink -f "${CONFIG}" 2>/dev/null || echo "${CONFIG}")" '$1==c {print $1}' | head -1)
    if [[ -n "${in_use}" ]]; then
      echo "error: container '${name}' is running and already mounts ${CONFIG} as its config." >&2
      echo "       Two processes on one database is not safe. Either point SHORTLIST_CONFIG" >&2
      echo "       somewhere else, or stop that container first: docker stop ${name}" >&2
      exit 1
    fi
  done < <(docker ps --format '{{.Names}}' 2>/dev/null)
fi

mkdir -p "${CONFIG}"

echo "== config:  ${CONFIG}"
echo "== port:    ${PORT}"
echo "== dry-run: ${DRY_RUN}  (1 = nothing reaches Plex/plex.tv)"

SHORTLIST_CONFIG="${CONFIG}" SHORTLIST_DRY_RUN="${DRY_RUN}" \
  exec "${PYTHON}" -m uvicorn --factory shortlist.server.main:create_app --reload --port "${PORT}"
