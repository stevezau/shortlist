import { useEffect, useState } from "react";

/**
 * `Date.now()` that refreshes every second while `active`, so several timestamps rendered side by
 * side stay on the same clock (a run's "Started" and its duration must not disagree — issue #67).
 *
 * Inactive means SLOWER, not frozen. A finished run's duration never changes, but how long ago it
 * started does — pinning the value to mount left "8m ago" stuck at whatever the page loaded with on
 * a tab left open, the same staleness #67 was about, just slower. A minute is plenty for a reading
 * that only ever counts in minutes, and avoids a per-second timer on rows that no longer move.
 *
 * The clock is read in an effect and kept in state, never during render: `Date.now()` is impure, and
 * calling it while rendering makes the output depend on when React happens to re-render.
 */
const IDLE_INTERVAL_MS = 60_000;

export function useLiveClock(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  const period = active ? intervalMs : IDLE_INTERVAL_MS;
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), period);
    return () => clearInterval(id);
  }, [period]);
  return now;
}
