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
  return unseen(
    [...privacy.can_see_others, ...(privacy.unreadable_filters ?? []), ...(privacy.filters_not_enforced ?? [])],
    new Set(),
  );
}

/**
 * Everything that makes a run a warning: the flagged accounts plus those whose hide rules could not be
 * saved or that nobody could look through. A left-alone account is the owner's choice, not a warning.
 */
export function privacyWarnings(privacy: RunPrivacy | null | undefined): string[] {
  if (!privacy) return [];
  return unseen([...privacyFindings(privacy), ...(privacy.write_failed ?? []), ...(privacy.unchecked ?? [])], new Set());
}

/**
 * "OK with warnings": an OK run whose measurement flagged somebody.
 *
 * Not a run status. Five server queries filter on `status in ("ok", "error")`, so a new status would
 * silently drop these runs from all of them (phase-2 plan); the warning is derived here instead. A
 * failed run keeps saying "Failed" — the warning never upgrades or replaces it.
 */
export function hasPrivacyWarning(run: { status: string; privacy?: RunPrivacy | null }): boolean {
  return run.status === "ok" && privacyWarnings(run.privacy).length > 0;
}

type RunPrivacyVerdict =
  /** The run never measured (older runs, dry runs, runs that died before the merge). */
  | { kind: "not_measured" }
  /** Plex's own filter read did not run, or the run did not record which accounts it could not vouch
   *  for, so "every row is hidden" cannot be said of anyone. */
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
    }
  /** Counted, but some accounts this run could not vouch for. They are never counted as hiding, and
   *  are named in place of the all-clear. Each list leaves out names an earlier one already gave. */
  | {
      kind: "unvouched";
      hiding: number;
      total: number;
      flagged: string[];
      /** A Restriction Profile account nobody could look through (no token, no usable read). */
      unchecked: string[];
      /** Its share-filter write failed. */
      writeFailed: string[];
      /** The owner chose to leave its sharing alone, so it sees every row. Not a fault. */
      leftAlone: string[];
    };

/** `names` without any already in `seen` (case-insensitively), each added to `seen` as it is kept. */
function unseen(names: string[], seen: Set<string>): string[] {
  return names.filter((name) => {
    const key = name.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * How many accounts hide every row that is not theirs, as far as this run can vouch for.
 *
 * An account is counted as hiding only when the run vouched for it: never one it flagged, one it
 * could not look through, one whose filter write failed, or one the owner left alone. An older run
 * that did not record those last three cannot vouch for anyone, so it gets no count at all.
 *
 * The total is the run's people PLUS anyone named who is not among them: the privacy merge writes
 * to every account on the server, so an unreadable filter can belong to someone this run built
 * nothing for, and leaving them out would print "4 of 4" above a callout naming a fifth.
 */
export function runPrivacyVerdict(
  privacy: RunPrivacy | null | undefined,
  people: string[],
): RunPrivacyVerdict {
  if (!privacy) return { kind: "not_measured" };
  const flagged = privacyFindings(privacy);
  const { unchecked, write_failed, left_alone } = privacy;
  if (privacy.unreadable_filters === null || unchecked === null || write_failed === null || left_alone === null) {
    return { kind: "partly_measured", flagged };
  }
  const named = new Set(flagged.map((name) => name.toLowerCase()));
  const writeFailed = unseen(write_failed, named);
  const notChecked = unseen(unchecked, named);
  const leftAlone = unseen(left_alone, named);
  const accounts = new Set([...people.map((name) => name.toLowerCase()), ...named]);
  if (accounts.size === 0) return { kind: "no_accounts" };
  const hiding = accounts.size - named.size;
  if (named.size > flagged.length) {
    return {
      kind: "unvouched",
      hiding,
      total: accounts.size,
      flagged,
      unchecked: notChecked,
      writeFailed,
      leftAlone,
    };
  }
  return {
    kind: "counted",
    hiding,
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
