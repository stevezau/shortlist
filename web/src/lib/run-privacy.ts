import type { Run } from "@/lib/types";

/** What one run measured about who can see whose rows (`api/runs.py::_run_privacy`). */
export type RunPrivacy = NonNullable<Run["privacy"]>;

/**
 * Every account the run flagged, once each, in the order the checks report them.
 *
 * A check that did not run (`null`) contributes nothing here, which is NOT the same as reporting it
 * clean: whether a missing check still licenses an all-clear is {@link runPrivacyVerdict}'s call.
 */
export function privacyFindings(privacy: RunPrivacy | null | undefined): string[] {
  if (!privacy) return [];
  const seen = new Set<string>();
  const names: string[] = [];
  for (const name of [
    ...privacy.can_see_others,
    ...(privacy.unreadable_filters ?? []),
    ...(privacy.filters_not_enforced ?? []),
  ]) {
    const key = name.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    names.push(name);
  }
  return names;
}

/**
 * "OK with warnings": an OK run whose measurement flagged somebody.
 *
 * Not a run status. Five server queries filter on `status in ("ok", "error")`, so a new status would
 * silently drop these runs from all of them (phase-2 plan); the warning is derived here instead. A
 * failed run keeps saying "Failed" — the warning never upgrades or replaces it.
 */
export function hasPrivacyWarning(run: { status: string; privacy?: RunPrivacy | null }): boolean {
  return run.status === "ok" && privacyFindings(run.privacy).length > 0;
}

export type RunPrivacyVerdict =
  /** The run never measured (older runs, dry runs, runs that died before the merge). */
  | { kind: "not_measured" }
  /** Plex's own filter read did not run, so "every row is hidden" cannot be said of anyone. */
  | { kind: "partly_measured"; flagged: string[] }
  /** Measured, with nobody to measure — never rendered as "0 of 0". */
  | { kind: "no_accounts" }
  | {
      kind: "counted";
      hiding: number;
      total: number;
      flagged: string[];
      /** False when the enforcement spot-check did not run: the rules were stored, nobody looked
       *  through an account's eyes to see Plex apply them. */
      enforcementChecked: boolean;
    };

/**
 * How many accounts hide every row that is not theirs, as far as this run can vouch for.
 *
 * The total is the run's people PLUS anyone flagged who is not among them: the privacy merge writes
 * to every account on the server, so an unreadable filter can belong to someone this run built
 * nothing for, and leaving them out would print "4 of 4" above a callout naming a fifth.
 */
export function runPrivacyVerdict(
  privacy: RunPrivacy | null | undefined,
  people: string[],
): RunPrivacyVerdict {
  if (!privacy) return { kind: "not_measured" };
  const flagged = privacyFindings(privacy);
  if (privacy.unreadable_filters === null) return { kind: "partly_measured", flagged };
  const accounts = new Set([...people, ...flagged].map((name) => name.toLowerCase()));
  if (accounts.size === 0) return { kind: "no_accounts" };
  return {
    kind: "counted",
    hiding: accounts.size - flagged.length,
    total: accounts.size,
    flagged,
    enforcementChecked: privacy.filters_not_enforced !== null,
  };
}

/** "kid", "kid and tom", "a, b and c", "a, b, c and 2 more". */
export function nameList(names: string[], max = 3): string {
  if (names.length <= 1) return names[0] ?? "";
  if (names.length > max) {
    return `${names.slice(0, max).join(", ")} and ${names.length - max} more`;
  }
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}
