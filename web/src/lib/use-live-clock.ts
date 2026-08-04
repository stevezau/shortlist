import { useEffect, useState } from "react";

/**
 * `Date.now()` that refreshes every second while `active`, so several timestamps rendered side by
 * side stay on the same clock (a run's "Started" and its duration must not disagree — issue #67).
 *
 * When `active` is false it stops the TIMER but still reads the current time on each render. Not the
 * same as freezing: a finished run's DURATION never changes, but how long ago it started does, and
 * pinning the value to mount left every finished run's "8m ago" stuck at its load-time figure on a
 * tab left open — which is the same class of staleness #67 was about, just slower.
 */
export function useLiveClock(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [active, intervalMs]);
  return active ? now : Date.now();
}
