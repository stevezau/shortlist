import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { rowsNotHidden, rowsNotTheirs } from "@/lib/privacy-attention";
import { usePrivacyStatus } from "@/lib/queries";
import type { User } from "@/lib/types";
import { profileName } from "@/lib/user-profile";
import { profileBlocksRows } from "@/lib/user-state";

const PLEX_USERS_URL = "https://app.plex.tv/desktop/#!/settings/users";

/**
 * Shown on the page of a person who is Off: said once, in one place, instead of a switch per row.
 *
 * The privacy fact is counted in ROWS, the owner's unit, from the same live reading the Users list
 * and the Privacy page use (`rowsNotHidden`) — never the run's per-library collection count, which
 * is a larger number for the same account. A restriction profile gets the two-step Plex fix.
 */
export function OffBanner({ user }: { user: User }) {
  const privacy = usePrivacyStatus();
  const name = user.display_name || user.username;
  const account = privacy.data?.accounts.find((a) => a.user_id === user.id);
  const live = account && privacy.data && !privacy.data.error && !privacy.data.rows_error;
  const exposed = live ? rowsNotHidden(account, privacy.data) : 0;
  // No live answer for this person: the run's own count is the only evidence left, so use it, labelled.
  const lastRun = user.unhidden_rows;
  const profile = profileBlocksRows(user) ? profileName(user) : "";

  return (
    <Card data-testid="off-banner" className="overflow-hidden p-0">
      <div className="flex items-center gap-3 px-6 py-4">
        <Badge variant="outline" className="text-muted-foreground">
          Off
        </Badge>
        <p className="font-medium">Off &mdash; No new rows are built for {name}.</p>
      </div>
      {live && exposed > 0 && (
        <div className="border-t bg-warning/10 px-6 py-3 text-sm text-warning">
          <span className="font-medium">
            {name} {rowsNotTheirs(exposed)}.
          </span>
          {profile && " Plex rejects hide rules for accounts with a Restriction Profile."}{" "}
          Turning {name} off in Shortlist does not fix this.
        </div>
      )}
      {!live && !privacy.isPending && (
        <div className="border-t bg-warning/10 px-6 py-3 text-sm text-warning">
          {lastRun > 0 ? (
            <>
              <span className="font-medium">
                Couldn&rsquo;t check live. The last run found {name} could see {lastRun}{" "}
                {lastRun === 1 ? "collection" : "collections"} that aren&rsquo;t theirs.
              </span>{" "}
              Turning {name} off in Shortlist does not fix this.
            </>
          ) : (
            <span className="font-medium">
              Couldn&rsquo;t check what {name} can see right now. Read again on the Privacy page.
            </span>
          )}
        </div>
      )}
      {profile && (
        <div className="border-t px-6 py-4">
          <ol className="space-y-2 text-sm">
            <li className="flex gap-3">
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-raised text-xs font-semibold">
                1
              </span>
              <span>
                In Plex: Settings &rarr; Users &amp; Sharing &rarr; {name} &rarr; Restriction
                Profile &rarr; None. It is set to {profile} now.
              </span>
            </li>
            <li className="flex gap-3">
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-raised text-xs font-semibold">
                2
              </span>
              <span>The next run hides everyone else&rsquo;s rows from {name}.</span>
            </li>
          </ol>
          <Button asChild variant="outline" className="mt-4">
            <a href={PLEX_USERS_URL} target="_blank" rel="noreferrer">
              Open Plex Users &amp; Sharing
            </a>
          </Button>
        </div>
      )}
    </Card>
  );
}
