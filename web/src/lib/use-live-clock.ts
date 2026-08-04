import { useEffect, useState } from "react";

/**
 * `Date.now()` that refreshes every second while `active`, so several timestamps rendered side by
 * side stay on the same clock. Frozen at the mount value when `active` is false — a finished run's
 * numbers never change, so it costs nothing to keep it mounted.
 */
export function useLiveClock(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [active, intervalMs]);
  return now;
}
