#!/bin/bash
# Deploy the latest built image, with a health-gated rollback. Run AFTER the GitHub Docker workflow
# has published the tag.
#
# WHO DEPLOYS (checked 2026-09-08): watchtower does, on its nightly 04:30 schedule
# (`WATCHTOWER_SCHEDULE=0 30 4 * * *`; it is not label-scoped and `shortlist` is not in
# WATCHTOWER_DISABLE_CONTAINERS). This script is the "don't wait until 04:30" button, and it builds a
# container IDENTICAL to the one watchtower would — same image, ports, mounts and env — so running it
# does not change who owns the container. An earlier version of this header claimed watchtower was
# disabled here and that this script was "the single deployer"; both were false, and the
# `watchtower.enable=false` label that made the second claim true-by-force has been removed.
#
# Usage:  bash scripts/deploy.sh
#         (the repo now lives on the plex host; this used to be `ssh plex 'bash -s' < …`)
set -uo pipefail

readonly IMAGE="ghcr.io/stevezau/shortlist:dev"   # published by CI on every master push
readonly NAME="shortlist"
readonly PORT="5959"
readonly CONFIG_VOL="/config/shortlist:/config"
readonly PUID="${PUID:-1000}"
readonly PGID="${PGID:-1000}"
readonly TZ_="${TZ:-Australia/Sydney}"

echo "== pulling ${IMAGE} =="
docker pull "${IMAGE}"

echo "== recreating ${NAME} =="
# First deploy of this name has nothing to rename; guard so it doesn't abort.
if docker inspect "${NAME}" >/dev/null 2>&1; then
  docker rename "${NAME}" "${NAME}_old"
  docker stop "${NAME}_old" >/dev/null
fi
# These flags must stay in step with the live container (and with dotfiles.flex's `run_check`), or a
# manual deploy quietly produces a different container than the nightly one.
docker run -d --name "${NAME}" \
  -p "${PORT}:5959" \
  -e PUID="${PUID}" \
  -e PGID="${PGID}" \
  -e TZ="${TZ_}" \
  -v "${CONFIG_VOL}" \
  -v /etc/localtime:/etc/localtime:ro \
  --restart unless-stopped \
  "${IMAGE}" >/dev/null

echo "== waiting for health =="
ok=0
for _ in $(seq 1 25); do
  sleep 3
  if curl -fsS "http://localhost:${PORT}/api/system/health" >/dev/null 2>&1; then ok=1; break; fi
done

if [ "${ok}" = "1" ]; then
  docker rm "${NAME}_old" >/dev/null 2>&1 || true  # absent on a first deploy
  echo "== HEALTHY — deploy ok =="
  echo -n "health: "; curl -fsS "http://localhost:${PORT}/api/system/health"; echo
else
  echo "== UNHEALTHY — rolling back =="
  docker logs --tail 50 "${NAME}" 2>&1 || true
  docker stop "${NAME}" >/dev/null 2>&1 || true
  docker rm "${NAME}" >/dev/null 2>&1 || true
  if docker inspect "${NAME}_old" >/dev/null 2>&1; then
    docker rename "${NAME}_old" "${NAME}"
    docker start "${NAME}" >/dev/null
    echo "== rolled back to previous image =="
  else
    echo "== no previous container to roll back to (first deploy) =="
  fi
  exit 1
fi
