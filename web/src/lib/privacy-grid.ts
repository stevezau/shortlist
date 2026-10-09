import { rowsNotHidden, rulesNotApplied } from "@/lib/privacy-attention";
import type { AccountPrivacy, PrivacyStatus } from "@/lib/types";

/** What one account can do with one row, as far as the live reading can say. */
export type GridCell =
  /** The account's own row. */
  | "own"
  /** plex.tv stores the exclude that hides this row from the account. */
  | "hidden"
  /** The account can see a row that isn't theirs. */
  | "sees"
  /** The exclude is stored, but a run looked through this account and Plex showed other people's rows. */
  | "stored_not_applied"
  /** Plex refuses the rule for this account and a run saw none of the rows: nothing hides them, nothing showed. */
  | "refused"
  /** The owner: Plex has no share to filter. */
  | "owner"
  /** The owner chose to leave this account's sharing alone. */
  | "left_alone"
  /** No reading to base an answer on. */
  | "unknown";

const PREFIX = "shortlist_";

/** "mike" for `shortlist_mike`: the slug a per-person row label carries. */
export function labelSlug(label: string): string {
  return label.toLowerCase().startsWith(PREFIX) ? label.slice(PREFIX.length) : label;
}

/** "mike's rows" — the column name for a per-person row label, using the account's display name when known. */
export function rowColumnName(label: string, accounts: AccountPrivacy[]): string {
  const slug = labelSlug(label).toLowerCase();
  const owner = accounts.find((account) => account.slug.toLowerCase() === slug);
  return `${owner?.display_name || labelSlug(label)}’s rows`;
}

/**
 * The answer for one account and one row label.
 *
 * Never more certain than the reading: "hidden" is only what plex.tv stores, and an account a run
 * measured seeing other people's rows anyway reads "stored_not_applied" instead.
 */
export function gridCell(account: AccountPrivacy, label: string, status: PrivacyStatus): GridCell {
  if (account.state === "owner") return "owner";
  if (account.state === "unknown") return "unknown";
  if (account.state === "left_alone") return "left_alone";
  const wanted = label.toLowerCase();
  if (wanted === `${PREFIX}${account.slug}`.toLowerCase()) return "own";
  const lower = (labels: string[]) => labels.map((value) => value.toLowerCase());
  if (lower(account.missing).includes(wanted)) {
    return account.state === "refused_by_plex" && rowsNotHidden(account, status) === 0 ? "refused" : "sees";
  }
  if (lower(account.hides).includes(wanted)) {
    return rulesNotApplied(account, status) ? "stored_not_applied" : "hidden";
  }
  return "unknown";
}
