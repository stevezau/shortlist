import { useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { apiErrorMessage } from "@/lib/api";
import { usePatchUser } from "@/lib/queries";
import type { User } from "@/lib/types";

/**
 * Whether Shortlist may edit this person's Plex sharing settings.
 *
 * A separate axis from the on/off switch in the Users list, not a third position on it: that one
 * decides whether they get a ROW, this decides whether we touch THEIR share filters, and an account
 * can have a row with its sharing untouched. Kept off the Users table on purpose — it is rare, it
 * reduces privacy, and it does not belong where a mis-click lands.
 *
 * Phrased positively because it is a switch: "don't change their settings" as a switch label is a
 * double negative the moment you turn it on. The consequence is stated where the choice is made
 * (frontend rules: controls say exactly what happens), and again as a live warning once it is off.
 */
export function UserSharing({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const [saved, setSaved] = useState(false);
  const who = user.display_name || user.username;

  if (user.user_type === "owner") {
    return (
      <Card>
        <CardContent className="pt-6">
          <p className="text-sm text-muted-foreground">
            Plex has no sharing settings for the account that owns the server,
            so there is nothing here for Shortlist to change either way.
          </p>
        </CardContent>
      </Card>
    );
  }

  const save = (manage: boolean) => {
    setSaved(false);
    patchUser.mutate(
      { id: user.id, patch: { manage_sharing: manage } },
      { onSuccess: () => setSaved(true) },
    );
  };

  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-0.5">
            <div className="flex items-center gap-2">
              <Label htmlFor="user-manage-sharing">
                Manage their Plex sharing settings
              </Label>
              <SavedIndicator show={saved} />
            </div>
            {/* This toggle is the ONE write in the product that widens what an account can see
                (plex-safety rule 3). The old copy — "leave their Plex sharing exactly as you set
                it" — read as a considerate no-op and never said the consequence out loud, while the
                badge this same choice produces already said it plainly. The decision is made here,
                so the outcome belongs here. */}
            <p className="text-sm text-muted-foreground">
              <strong className="text-foreground">On:</strong> Shortlist edits{" "}
              {who}&rsquo;s Plex sharing so the only personal row they see is
              their own.
            </p>
            <p className="text-sm text-muted-foreground">
              <strong className="text-destructive-text">Off:</strong> {who} will
              be able to see everyone else&rsquo;s personal rows. Shortlist
              removes what it added to their account and never touches it again.
              Nobody else&rsquo;s account changes — everyone still has{" "}
              {who}&rsquo;s row hidden, and a shared row you&rsquo;ve limited to
              certain people stays hidden either way.
            </p>
          </div>
          <Switch
            id="user-manage-sharing"
            checked={user.manage_sharing}
            onCheckedChange={save}
            disabled={patchUser.isPending}
            aria-label={`Manage Plex sharing settings for ${user.username}`}
          />
        </div>
        {!user.manage_sharing && (
          <p className="text-sm text-warning">
            {who} can see other people&rsquo;s rows unless their own Plex
            restrictions stop them. That is usually what you want when
            they&rsquo;ve got an &ldquo;allow only&rdquo; label list of their
            own — it is already keeping Shortlist&rsquo;s rows out of their
            view.
          </p>
        )}
        {patchUser.isError && (
          <p role="alert" className="text-sm text-destructive-text">
            {apiErrorMessage(
              patchUser.error,
              "Couldn’t save this. Their Plex sharing is unchanged.",
            )}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
