import { ShieldAlert } from "lucide-react";
import { Link } from "react-router";

import { nameList } from "@/lib/run-privacy";
import type { RunDetail } from "@/lib/types";

/**
 * Names the accounts this run found could see rows that are not theirs, and says why.
 *
 * Amber, not red: the run still built everyone's rows, and for the commonest case (a managed account
 * with a Restriction Profile) the fix is a Plex setting only the owner can change. Reporting only —
 * every sentence restates a list the run persisted; a list the run did not measure says nothing.
 * `displayName` names accounts the run built nothing for. `onFix` replaces the link with a button for callers that must do something before leaving the page.
 */
export function RunPrivacyCallout({
  run,
  onFix,
  displayName,
}: {
  run: RunDetail;
  onFix?: () => void;
  displayName?: (username: string) => string;
}) {
  const privacy = run.privacy;
  if (!privacy) return null;
  const nameOf = (username: string) =>
    displayName?.(username) ||
    run.users.find((user) => user.username.toLowerCase() === username.toLowerCase())?.display_name ||
    username;
  const canSee = privacy.can_see_others;
  const unreadable = privacy.unreadable_filters ?? [];
  const notEnforced = privacy.filters_not_enforced ?? [];
  if (canSee.length === 0 && unreadable.length === 0 && notEnforced.length === 0) return null;

  const onlyCanSee = canSee.length === 1 ? canSee[0] : undefined;
  // "kid's row was built" only when it was: a person whose own build failed is named without it.
  const builtFor = (username: string) =>
    run.users.some((user) => user.username.toLowerCase() === username.toLowerCase() && user.status === "ok");

  return (
    <div
      role="status"
      data-testid="run-privacy-callout"
      className="grid grid-cols-[auto_minmax(0,1fr)] gap-2.5 rounded-lg border border-warning/40 bg-warning/10 px-3.5 py-3 text-sm"
    >
      <ShieldAlert className="mt-0.5 h-4 w-4 text-warning" aria-hidden="true" />
      <div className="space-y-1.5 [overflow-wrap:anywhere]">
        {canSee.length > 0 && (
          <p>
            <strong className="font-semibold">
              {onlyCanSee
                ? builtFor(onlyCanSee)
                  ? `${nameOf(onlyCanSee)}’s row was built, but ${nameOf(onlyCanSee)} can see everyone else’s.`
                  : `${nameOf(onlyCanSee)} can see everyone else’s rows.`
                : `${nameList(canSee.map(nameOf))} can see everyone else’s rows.`}
            </strong>{" "}
            Plex rejects hide rules for accounts with a Restriction Profile, so other people’s rows show on{" "}
            {onlyCanSee ? "that account’s" : "their"} Home.
            {run.status === "ok" && " Everything else in this run went through."}
          </p>
        )}
        {unreadable.length > 0 && (
          <p>
            <strong className="font-semibold">
              Plex can’t read the restrictions on {nameList(unreadable.map(nameOf))}, so Shortlist can’t hide
              rows from {unreadable.length === 1 ? "that account" : "them"}.
            </strong>{" "}
            A label there has an “&amp;” in its name. Rename it in Plex and the next run hides the rows.
          </p>
        )}
        {notEnforced.length > 0 && (
          <p>
            <strong className="font-semibold">
              Plex isn’t applying the hide rules on {nameList(notEnforced.map(nameOf))}.
            </strong>{" "}
            Shortlist wrote them and Plex stored them, but this run still saw other people’s rows on{" "}
            {notEnforced.length === 1 ? "that account’s" : "their"} Home.
          </p>
        )}
        {(() => {
          const className =
            "inline-block rounded-sm text-accent-foreground underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
          return onFix ? (
            <button type="button" className={className} onClick={onFix}>
              Fix in Privacy →
            </button>
          ) : (
            <Link to="/privacy" className={className}>
              Fix in Privacy →
            </Link>
          );
        })()}
      </div>
    </div>
  );
}
