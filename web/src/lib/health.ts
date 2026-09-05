import type { AppNotification } from "./types";

export type HealthState = "ok" | "warning" | "error";

export interface HealthCategory {
  category: string;
  label: string;
  /** Where the chip goes when nothing is wrong. A live alert's own `action_url` wins over it. */
  href: string;
}

export interface HealthChip extends HealthCategory {
  state: HealthState;
  /** The worst live alert's title. Absent when the category has nothing outstanding. */
  detail?: string;
}

/**
 * The six subsystems the strip reports, in reading order.
 *
 * Shown even when a category has never fired anything — an install with acquisition switched off
 * keeps a green "Requests" chip. A strip whose shape changes with the weather is harder to scan
 * than one whose chips are always in the same place.
 */
export const CORE_CATEGORIES: HealthCategory[] = [
  { category: "runs", label: "Runs", href: "/runs" },
  { category: "privacy", label: "Privacy", href: "/users" },
  { category: "rows", label: "Rows", href: "/rows" },
  { category: "requests", label: "Requests", href: "/settings#requests" },
  { category: "jobs", label: "Jobs", href: "/jobs" },
  { category: "watch", label: "Watch tracking", href: "/logs" },
];

/** Anything the map below doesn't recognise lands here rather than disappearing. */
const OTHER: HealthCategory = {
  category: "other",
  label: "Other",
  href: "/settings",
};

/**
 * Keyed on the STABLE id prefix each `notifications.py` builder emits, never on its title — copy
 * changes without warning and would silently re-file an alert.
 *
 * This list and `notifications.py` are two hand-maintained registers of the same ids and nothing
 * fails a build if they drift, which is exactly why an unmatched id still surfaces (under "Other")
 * instead of vanishing from the strip.
 *
 * Deliberately absent: `update-` and `owner-sees-all-rows`, both `info` and filtered out anyway;
 * and `secrets-we-cannot-read`, which is real and severe but is not ONE subsystem — a lost
 * `/config/secret.key` breaks whichever features used the credentials it encrypted. It shows under
 * "Other" and its own `action_url` still lands the owner on Settings, which beats a seventh chip
 * sitting green for a condition almost no install ever has.
 */
const CATEGORY_BY_ID_PREFIX: [prefix: string, category: string][] = [
  ["run-failed-", "runs"],
  ["run-partial-", "runs"],
  ["runs-paused", "runs"],
  ["recent-errors-", "runs"],
  ["unhideable-rows-", "privacy"],
  ["filters-not-enforced-", "privacy"],
  // `info` today, so it cannot reach a chip; mapped so that raising its severity later files it
  // under Rows rather than "Other".
  ["rows-unnamed-", "rows"],
  ["shelf-contention-", "rows"],
  ["mdblist-quota-", "requests"],
  ["requests-none-qualified-", "requests"],
  ["failed-jobs-", "jobs"],
  ["playback-listener-down", "watch"],
];

/** Only warning and error ever reach the chips — `deriveHealthChips` drops `info` before this. */
function chipState(notification: AppNotification): "warning" | "error" {
  return notification.severity === "error" ? "error" : "warning";
}

function severityRank(notification: AppNotification): number {
  return chipState(notification) === "error" ? 0 : 1;
}

function categoryFor(id: string): string | undefined {
  return CATEGORY_BY_ID_PREFIX.find(([prefix]) => id.startsWith(prefix))?.[1];
}

/** `action_url` can be an external release page (the bell renders those as `<a>`). A router `Link`
 *  would turn one into a relative path, so only an in-app path may override a chip's own href. */
function internalHref(url: string, fallback: string): string {
  return url.startsWith("/") ? url : fallback;
}

/**
 * Groups the alerts the bell already shows into one chip per subsystem, so the dashboard answers
 * "which area needs me" without the panel being opened and read.
 *
 * Green means "nothing outstanding here", NOT "verified healthy" — most of these alerts are
 * dismissable, and a dismissal silences the chip while the underlying fact stands. The Verdict
 * card's own status line reads raw report fields and cannot be dismissed, so where the two overlap
 * (last run, live watch tracking) that line is the authority and this strip is the summary.
 *
 * `info` is ignored on purpose: an available update, or "you see everyone's rows because you own
 * the server" (true by design on nearly every multi-user install), are not a subsystem in trouble,
 * and folding them in would leave chips permanently amber for conditions nobody should act on.
 */
export function deriveHealthChips(
  notifications: AppNotification[],
): HealthChip[] {
  const worst = new Map<string, AppNotification>();
  const unrecognised: AppNotification[] = [];

  for (const notification of notifications) {
    if (
      notification.severity !== "warning" &&
      notification.severity !== "error"
    )
      continue;
    const category = categoryFor(notification.id);
    if (!category) {
      unrecognised.push(notification);
      continue;
    }
    const current = worst.get(category);
    if (!current || severityRank(notification) < severityRank(current)) {
      worst.set(category, notification);
    }
  }

  const chips = CORE_CATEGORIES.map((core): HealthChip => {
    const hit = worst.get(core.category);
    return hit
      ? {
          ...core,
          state: chipState(hit),
          href: internalHref(hit.action_url, core.href),
          detail: hit.title,
        }
      : { ...core, state: "ok" };
  });

  const other = [...unrecognised].sort(
    (a, b) => severityRank(a) - severityRank(b),
  )[0];
  if (other) {
    chips.push({
      ...OTHER,
      state: chipState(other),
      href: internalHref(other.action_url, OTHER.href),
      detail: other.title,
    });
  }

  return chips;
}
