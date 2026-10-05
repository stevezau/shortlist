import { ShieldAlert } from "lucide-react";
import { Link } from "react-router";

import { cannotHide } from "@/lib/privacy-attention";
import { hasPrivacyWarning, nameList } from "@/lib/run-privacy";
import type { PrivacyStatus, Run } from "@/lib/types";

/**
 * An account Shortlist cannot hide rows from at all, named, with the reason in the owner's terms.
 *
 * Only for the two states no run can fix: Plex refuses hide rules for an account with a Restriction
 * Profile, and fails on a filter with a raw "&" in a label. A merely missing rule is not here — the
 * next run merges it back, and the Privacy cell above already counts it.
 */
export function PrivacyCallout({ status, lastRun }: { status: PrivacyStatus | undefined; lastRun: Run | undefined }) {
  if (!status || status.error) return null;
  const stuck = cannotHide(status);
  if (stuck.length === 0) return null;
  const nameOf = (account: (typeof stuck)[number]) => account.display_name || account.user;
  const refused = stuck.filter((account) => account.state === "refused_by_plex");
  const unreadable = stuck.filter((account) => account.state === "unreadable_filter");
  // "Reported as OK with a warning" only when the last run actually did — it measured these accounts
  // seeing other people's rows. Said of a run that never looked, it would be invented.
  const flaggedByRun =
    lastRun !== undefined &&
    hasPrivacyWarning(lastRun) &&
    refused.some((account) =>
      (lastRun.privacy?.can_see_others ?? []).some((name) => name.toLowerCase() === account.user.toLowerCase()),
    );
  const onlyRefused = refused.length === 1 ? refused[0] : undefined;

  return (
    <div
      role="status"
      data-testid="privacy-callout"
      className="grid grid-cols-[auto_minmax(0,1fr)] gap-2.5 rounded-lg border border-warning/40 bg-warning/10 px-3.5 py-3 text-sm"
    >
      <ShieldAlert className="mt-0.5 h-4 w-4 text-warning" aria-hidden="true" />
      <div className="space-y-1.5 [overflow-wrap:anywhere]">
        {refused.length > 0 && (
          <p>
            <strong className="font-semibold">
              Plex won’t hide other people’s rows from {nameList(refused.map(nameOf))}.
            </strong>{" "}
            {onlyRefused
              ? `That account has a Restriction Profile${onlyRefused.restriction_profile ? ` (${onlyRefused.restriction_profile})` : ""}`
              : "Those accounts have a Restriction Profile"}
            , and Plex rejects hide rules for those.
            {flaggedByRun &&
              " The last run still built everyone’s rows; it’s reported as OK with a warning until this is resolved."}
          </p>
        )}
        {unreadable.length > 0 && (
          <p>
            <strong className="font-semibold">
              Plex can’t read the restrictions on {nameList(unreadable.map(nameOf))}.
            </strong>{" "}
            A label there has an “&amp;” in its name, and Plex fails on it. Rename the label in Plex and the
            next run hides the rows.
          </p>
        )}
        <Link
          to="/privacy"
          className="inline-block rounded-sm text-accent-foreground underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          What to do →
        </Link>
      </div>
    </div>
  );
}
