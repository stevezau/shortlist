import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { cannotHide, rowsNotHidden, rowsNotTheirs } from "@/lib/privacy-attention";
import { nameList } from "@/lib/run-privacy";
import type { PrivacyStatus } from "@/lib/types";

/**
 * The one place the dashboard says an account cannot be hidden from, in one line with a way out.
 *
 * Only for the two states no run can fix: Plex refuses hide rules for an account with a Restriction
 * Profile, and fails on a filter with a raw "&" in a label. A merely missing rule is not here — the
 * next run merges it back, and the Privacy cell above already counts it. The figure is in ROWS, the
 * same one the Users and Privacy pages use.
 */
export function PrivacyCallout({ status }: { status: PrivacyStatus | undefined }) {
  if (!status || status.error) return null;
  const stuck = cannotHide(status);
  if (stuck.length === 0) return null;
  const nameOf = (account: (typeof stuck)[number]) => account.display_name || account.user;
  const refused = stuck.filter((account) => account.state === "refused_by_plex");
  const unreadable = stuck.filter((account) => account.state === "unreadable_filter");
  const onlyRefused = refused.length === 1 ? refused[0] : undefined;
  // Rows an account can see anyway, only when someone looked through it and saw them.
  const exposed = onlyRefused ? rowsNotHidden(onlyRefused, status) : 0;

  return (
    <div
      role="status"
      data-testid="privacy-callout"
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-warning/30 bg-warning/10 px-4 py-2.5"
    >
      <div className="min-w-0 flex-1 basis-72 space-y-1 text-sm [overflow-wrap:anywhere]">
        {refused.length > 0 && (
          <p>
            {onlyRefused ? (
              <>
                <strong className="font-semibold">{nameOf(onlyRefused)}</strong>’s Plex Restriction Profile
                {onlyRefused.restriction_profile ? ` (${onlyRefused.restriction_profile})` : ""} blocks hide rules
                {exposed > 0 ? `, so ${nameOf(onlyRefused)} ${rowsNotTheirs(exposed)}.` : "."}
              </>
            ) : (
              <>
                Plex Restriction Profiles block hide rules for{" "}
                <strong className="font-semibold">{nameList(refused.map(nameOf))}</strong>.
              </>
            )}
          </p>
        )}
        {unreadable.length > 0 && (
          <p>
            Plex can’t read the restrictions on{" "}
            <strong className="font-semibold">{nameList(unreadable.map(nameOf))}</strong>: a label there has an
            “&amp;” in its name. Rename it and the next run hides the rows.
          </p>
        )}
      </div>
      <Button asChild variant="outline" size="sm">
        <Link to="/privacy">How to fix in Plex →</Link>
      </Button>
    </div>
  );
}
