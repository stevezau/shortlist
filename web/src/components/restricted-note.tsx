import { ShieldAlert } from "lucide-react";

import { profileName } from "@/lib/user-profile";
import type { User } from "@/lib/types";

/**
 * Why an account with a Plex parental PRESET gets no row, and what to change to give them one.
 *
 * Shown only for a preset — not for every Plex Home user. plex.tv reports `restricted` for both, so
 * this note used to appear on ordinary managed accounts and tell their owner Plex was hiding content
 * from them, which was untrue (#20). Those accounts now get rows and privacy filters like anyone else.
 *
 * The copy names the two consequences separately, because they land differently for the reader: Plex
 * hides the collections (so a row would be invisible), AND Plex refuses the share filters Shortlist
 * uses (so it cannot make this account private either). One setting fixes both.
 */
export function RestrictedNote({
  user,
  className,
}: {
  user: User;
  className?: string;
}) {
  if (!user.restriction_profile) return null;
  return (
    <div
      className={`flex gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm ${className ?? ""}`}
    >
      <ShieldAlert
        className="mt-0.5 h-4 w-4 shrink-0 text-destructive-text"
        aria-hidden="true"
      />
      <div className="space-y-1">
        <p className="font-medium">
          Plex&rsquo;s <strong>{profileName(user)}</strong> restriction profile
          is set on this account &mdash; no row is built for them.
        </p>
        <p className="text-muted-foreground">
          Plex hides every collection from an account with a restriction
          profile, including any row Shortlist would create, so building one
          would spend a run on something they can never see. Plex also refuses
          to save the privacy filters Shortlist writes, which is why this
          account is left out of them entirely.
        </p>
        <p className="text-muted-foreground">
          To give this person recommendations, open Plex &rarr; Settings &rarr;
          Users &amp; Sharing and set their <strong>Restriction Profile</strong>{" "}
          to <strong>None</strong>. You can still limit what they see by rating
          or label there &mdash; Plex only allows that once the profile is None.
          Their row is built on the next run.
        </p>
      </div>
    </div>
  );
}
