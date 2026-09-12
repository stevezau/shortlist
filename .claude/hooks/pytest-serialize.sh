#!/bin/bash
# PreToolUse/Bash: refuse a pytest run while another one is already in flight.
#
# Several agent sessions share this host. Each `pytest` fans out to PYTEST_XDIST_AUTO_NUM_WORKERS
# processes, so four concurrent sessions is 4x that many — the shape that took the plex host down on
# 2026-09-12 (189 workers, ~30 GB into swap). Scoped runs stay cheap and are still encouraged; what
# is not safe is two of them overlapping.
#
# Denies rather than waits: a hook that blocks holds the whole session, and the agent retrying a
# moment later is the cheaper failure mode.
set -uo pipefail

readonly REPO="/home/data/workspace/shortlist"

payload=$(cat)
cmd=$(printf '%s' "${payload}" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[[ "${cmd}" == *pytest* ]] || exit 0

# Our own process tree must not count as "another run".
mine=$({ pstree -p $$ 2>/dev/null || true; } | grep -oE '\([0-9]+\)' | tr -d '()')
mine="$$ ${PPID} ${mine}"

running=""
while read -r pid args; do
  [[ -z "${pid}" ]] && continue
  [[ " ${mine} " == *" ${pid} "* ]] && continue
  # xdist workers are children of the controller; report the controller only.
  [[ "${args}" == *execnet* ]] && continue
  [[ "$(readlink -f "/proc/${pid}/cwd" 2>/dev/null)" == "${REPO}"* ]] || continue
  running="${pid}"
  break
done < <(pgrep -af '[p]ytest' 2>/dev/null)

[[ -z "${running}" ]] && exit 0

started=$(ps -o lstart= -p "${running}" 2>/dev/null | xargs)
jq -cn --arg pid "${running}" --arg started "${started}" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: ("Another shortlist pytest is already running (pid \($pid), started \($started)). Concurrent runs on this host oversubscribe the box — that is what took plex down. Wait for it to finish and retry, or narrow your run and retry once it clears.")
  }
}'
