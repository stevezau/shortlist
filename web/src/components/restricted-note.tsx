import { ShieldAlert } from "lucide-react";

import { profileName } from "@/lib/user-profile";
import type { User } from "@/lib/types";

/**
 * Why an account with a Plex parental PRESET gets no row — and, when the last run measured it, that
 * this account can see rows belonging to other people.
 *
 * Shown only for a preset — not for every Plex Home user. plex.tv reports `restricted` for both, so
 * this note used to appear on ordinary managed accounts and tell their owner Plex was hiding content
 * from them, which was untrue (#20). Those accounts now get rows and privacy filters like anyone else.
 *
 * "Usually hides", not "always". This note used to state that Plex hides EVERY collection from a
 * profiled account, and Shortlist skipped those accounts on exactly that reasoning — but an
 * `older_kid` account on a real server listed three collections (measured 2026-08-11, #76). The run
 * measures instead of assuming now, and `unhidden_rows` is what it found.
 */
export function RestrictedNote({
  user,
  className,
}: {
  user: User;
  className?: string;
}) {
  if (!user.restriction_profile) return null;
  const exposed = user.unhidden_rows ?? 0;
  const name = user.display_name || user.username;
  return (
    <div
      className={`flex gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm ${className ?? ""}`}
    >
      <ShieldAlert
        className="mt-0.5 h-4 w-4 shrink-0 text-destructive-text"
        aria-hidden="true"
      />
      {exposed > 0 ? (
        // Ordered for the reader, not for the system: who and how bad, then why nobody here can fix
        // it, then the choice. The mechanism (share filters, label excludes) is deliberately absent —
        // knowing it changes nothing the owner does.
        <div className="space-y-2">
          <p className="font-medium text-destructive-text">
            {name} can see {exposed}{" "}
            {exposed === 1 ? "row that belongs" : "rows that belong"} to other
            people.
          </p>
          <p className="text-muted-foreground">
            Plex won&rsquo;t let Shortlist hide anything from an account with a
            parental profile set, and this one is{" "}
            <strong>{profileName(user)}</strong> &mdash; so this can&rsquo;t be
            fixed from here.
          </p>
          <p className="text-muted-foreground">
            <strong>Clear the profile in Plex:</strong> Settings &rarr; Users
            &amp; Sharing &rarr; set <strong>Restriction Profile</strong> to{" "}
            <strong>None</strong>. Shortlist can then hide everyone else&rsquo;s
            rows from them, and build {name} a row of their own. You can still
            limit what they see by rating or label there.
          </p>
          <p className="text-muted-foreground">
            Turning {name} off in Shortlist does <strong>not</strong> fix this.
            That removes their own row; what they can see is everyone
            else&rsquo;s, and hiding those needs the very filter Plex is
            refusing.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          <p className="font-medium">
            Plex&rsquo;s <strong>{profileName(user)}</strong> restriction
            profile is set on this account &mdash; no row is built for them.
          </p>
          <p className="text-muted-foreground">
            Plex usually hides collections from an account with a restriction
            profile, so a row here would probably be invisible to them. It also
            won&rsquo;t save the privacy filters Shortlist writes, which is why
            this account is left out of those.
          </p>
          <p className="text-muted-foreground">
            To give {name} recommendations, open Plex &rarr; Settings &rarr;
            Users &amp; Sharing and set their{" "}
            <strong>Restriction Profile</strong> to <strong>None</strong>. You
            can still limit what they see by rating or label there. Their row is
            built on the next run.
          </p>
        </div>
      )}
    </div>
  );
}
