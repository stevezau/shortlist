import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queries";
import type { AccountPrivacy, PrivacyStatus } from "@/lib/types";

/**
 * The privacy status read at a glance: the rail's Privacy dot and the dashboard's Privacy cell.
 *
 * Same cache entry as the Privacy page (`usePrivacyStatus`), held for longer. Every read costs a
 * plex.tv roster read and a PMS collections read, and the rail is mounted on every page — with the
 * page's own 60s staleness and refetch-on-focus, alt-tabbing anywhere in the app re-read plex.tv once
 * a minute. A five-minute-old dot is still a warning; the Privacy page itself reads fresh when opened
 * and on "Read again", and its newer answer updates this one through the shared entry.
 */
export function usePrivacyGlance() {
  return useQuery({
    queryKey: queryKeys.privacyStatus,
    queryFn: api.getPrivacyStatus,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
}

/** Account states in which that account can see rows that are not theirs (`api/privacy.py::_account`). */
const NOT_HIDING = new Set(["missing", "unreadable_filter", "refused_by_plex"]);

/**
 * Whether the Privacy nav item should carry its warning dot.
 *
 * Reports what the privacy endpoint already decided and adds no judgement of its own: any headline
 * but "clean" (a failed read included, since an unread filter is not a private one), or any account
 * the endpoint says is not hiding. `refused_by_plex` is checked per account because the headline only
 * escalates it once a run has measured the exposure. `left_alone` and `owner` never count: one is
 * the owner's own setting, the other a Plex fact.
 */
export function privacyNeedsAttention(status: PrivacyStatus | undefined): boolean {
  if (!status) return false;
  if (status.summary !== "clean") return true;
  return status.accounts.some((account) => NOT_HIDING.has(account.state));
}

type PrivacyGlance =
  /** plex.tv failed: nothing about anyone's filter is current. */
  | { kind: "unreadable" }
  /** The PMS rows read failed: there is nothing to check the filters against. */
  | { kind: "rows_unknown" }
  /** No per-person row exists on Plex, so there is nothing for anyone to hide. */
  | { kind: "nothing_to_hide" }
  /** Rows exist, but no account is expected to hide them (only the owner, or every account left alone). */
  | { kind: "no_accounts" }
  | { kind: "counted"; hiding: number; total: number; exposed: AccountPrivacy[] };

/**
 * How many accounts hide every row that is not theirs, read off the live privacy status.
 *
 * Counts only the accounts expected to hide: not the owner (Plex has no share to filter for the
 * account that owns the server) and not one the owner chose to leave alone. An account is short if
 * its state is anything but `hiding`, OR a run measured Plex serving it other people's rows anyway
 * (`enforcement.not_enforced`) — every rule stored is not every rule applied, and the endpoint's
 * own headline ranks that exposure above a clean filter set.
 */
export function privacyGlance(status: PrivacyStatus): PrivacyGlance {
  if (status.error) return { kind: "unreadable" };
  if (status.rows_error) return { kind: "rows_unknown" };
  if (status.rows_on_plex.length === 0) return { kind: "nothing_to_hide" };
  const expected = status.accounts.filter(
    (account) => account.state !== "owner" && account.state !== "left_alone",
  );
  if (expected.length === 0) return { kind: "no_accounts" };
  const exposed = expected.filter((account) => account.state !== "hiding" || rulesNotApplied(account, status));
  return { kind: "counted", hiding: expected.length - exposed.length, total: expected.length, exposed };
}

/**
 * Whether a run's spot-check says Plex is not applying this account's stored rules.
 *
 * The run looks through ONE shared and ONE managed account, so a flagged name stands for its whole
 * kind: every account of the same `user_type` is covered, not only the one named. A flagged name that
 * matches no account (kind unknown) covers every account that isn't the owner.
 */
export function rulesNotApplied(account: AccountPrivacy, status: PrivacyStatus): boolean {
  const enforcement = status.enforcement;
  if (!enforcement?.measured) return false;
  const flagged = Object.entries(enforcement.not_enforced ?? {})
    .filter(([, keys]) => keys.length > 0)
    .map(([name]) => name.toLowerCase());
  if (flagged.length === 0 || account.state === "owner") return false;
  if (flagged.includes(account.user.toLowerCase())) return true;
  const kinds = new Set(
    status.accounts.filter((a) => flagged.includes(a.user.toLowerCase())).map((a) => a.user_type),
  );
  return kinds.size === 0 || kinds.has(account.user_type);
}

/** Accounts Shortlist cannot hide rows from at all: Plex refuses the rule, or fails on the filter. */
export function cannotHide(status: PrivacyStatus): AccountPrivacy[] {
  return status.accounts.filter(
    (account) => account.state === "refused_by_plex" || account.state === "unreadable_filter",
  );
}

/**
 * How many rows that aren't theirs an account can see: ONE figure for every screen that says it.
 *
 * The unit is the Privacy page's "Hides X of Y rows" — a per-person row on Plex, counted once however
 * many libraries it spans, read live from plex.tv — and the figure is its gap, `missing`. The run's
 * own measurement (`users.unhidden_rows`, `enforcement.unhideable`) counts COLLECTIONS, one per row
 * per library, so it is a different and larger number; showing it beside this one had the Dashboard
 * say 3, Users 5 and Privacy "0 of 3" about the same account.
 *
 * Zero where nothing is exposed: a clean filter, the owner (Plex has no share to filter), an account
 * the owner chose to leave alone, a reading with no rows to check against — and a profiled account a
 * run looked through and saw nobody else's rows (`little_kid`), because a rule Plex refuses only
 * matters if Plex then shows the rows.
 */
export function rowsNotHidden(account: AccountPrivacy, status: PrivacyStatus): number {
  if (!NOT_HIDING.has(account.state)) return 0;
  const enforcement = status.enforcement;
  if (account.state === "refused_by_plex" && enforcement?.unhideable_measured) {
    const user = account.user.toLowerCase();
    const seen = Object.entries(enforcement.unhideable ?? {}).some(
      ([name, keys]) => name.toLowerCase() === user && keys.length > 0,
    );
    if (!seen) return 0;
  }
  return account.missing.length;
}

/** "can see 3 rows that aren't theirs" — the words for {@link rowsNotHidden}, on every screen. */
export function rowsNotTheirs(count: number): string {
  return count === 1 ? "can see 1 row that isn’t theirs" : `can see ${count} rows that aren’t theirs`;
}
