import { formatHitRate, timeAgo } from "@/lib/format";
import type { Run, User } from "@/lib/types";

/**
 * The dashboard's top-strip summary. Computed from users alone — runs are best-effort, so a missing
 * or empty runs list simply reports "never"/"—" for the last-run fields rather than blocking the
 * whole strip.
 */
export function dashboardStats(users: User[], runs: Run[]) {
  const enabled = users.filter((user) => user.enabled).length;
  const lastRun = runs[0];
  const rates = users
    .map((user) => user.hit_rate)
    .filter((rate): rate is number => rate !== null);
  const hitRate =
    rates.length > 0
      ? formatHitRate(rates.reduce((a, b) => a + b, 0) / rates.length)
      : "—";
  return {
    enabled,
    total: users.length,
    lastRunAgo: lastRun ? timeAgo(lastRun.started_at) : "never",
    errors: lastRun ? lastRun.stats.users_error : null,
    hitRate,
  };
}
