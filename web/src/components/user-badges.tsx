import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { profileName } from "@/lib/user-profile";
import type { User } from "@/lib/types";

/**
 * What KIND of Plex account this is. Every user has one, so this always renders something — a
 * "Type" column that showed "owner" for one person and an em dash for everyone else read as
 * "unknown", when the answer was simply "a shared user", which is the ordinary case.
 */
export function UserTypeBadge({ user }: { user: User }) {
  if (user.user_type === "owner") {
    return (
      <Badge
        variant="outline"
        title="You own this Plex server. Plex can't hide anyone's row from this account, so it sees every row Shortlist creates."
      >
        Owner
      </Badge>
    );
  }
  if (user.user_type === "managed") {
    return (
      <Badge
        variant="secondary"
        title="A Plex Home profile managed from your account (a child or restricted profile), rather than someone with their own Plex login."
      >
        Managed
      </Badge>
    );
  }
  return (
    <Badge
      variant="secondary"
      title="Someone you've shared this server with, using their own Plex account."
    >
      Shared
    </Badge>
  );
}

/**
 * Only for an account Plex genuinely hides everything from — one with a parental PRESET.
 *
 * `restricted` alone is true for every Plex Home managed account, preset or not, so badging on it
 * told people with an ordinary Home user that Plex was hiding content from them when it wasn't, and
 * greyed out their enable toggle for no reason (#20).
 */
export function RestrictedBadge({ user }: { user: User }) {
  if (!user.restriction_profile) return null;
  return (
    <Badge
      variant="destructive"
      title={`Plex's ${profileName(user)} restriction profile is set on this account. Plex usually hides collections from it, so no row is built — and Plex refuses privacy filters for profiled accounts. Set the Restriction Profile to None in Plex to give this person recommendations.`}
    >
      {profileName(user)}
    </Badge>
  );
}

/**
 * This account can see rows that belong to other people, and Shortlist cannot hide them.
 *
 * Only ever non-zero for an account Plex refuses a share filter for. Shortlist used to assume such an
 * account saw no collections at all and skipped it silently; an `older_kid` account on a real server
 * listed three (measured 2026-08-11). The run measures it now, and this is the scannable form of what
 * it found — deliberately absent when the count is 0, so the badge means something when it appears.
 */
export function UnhiddenRowsBadge({ user }: { user: User }) {
  const exposed = user.unhidden_rows ?? 0;
  if (exposed < 1) return null;
  return (
    <Badge
      variant="destructive"
      // The remedy must match the rest of the feature: turning the person off removes THEIR row,
      // not their view of everyone else's, so it is deliberately not offered here either.
      title={`This account can see ${exposed} ${exposed === 1 ? "row" : "rows"} belonging to other people. Hiding a row means a Plex share filter, and Plex refuses to save one while a restriction profile is set — so Shortlist cannot hide ${exposed === 1 ? "it" : "them"}. Set this account's Restriction Profile to None in Plex; turning this person off in Shortlist does not fix it.`}
    >
      Sees {exposed} {exposed === 1 ? "row" : "rows"} of others&rsquo;
    </Badge>
  );
}

/**
 * The owner asked Shortlist to leave this account's Plex sharing settings alone.
 *
 * A setting, not a fault, so it is deliberately not `destructive` — but it is never silent either:
 * this account can see other people's rows, and hidden state that changes who sees what is exactly
 * the kind that gets forgotten and then reported as a leak.
 */
export function SharingUntouchedBadge({ user }: { user: User }) {
  if (user.manage_sharing) return null;
  return (
    <Badge
      variant="outline"
      title="You've asked Shortlist not to change this account's Plex sharing settings, so it adds no label exclusions for them. They can see other people's rows unless their own Plex restrictions stop them."
    >
      Sharing untouched
    </Badge>
  );
}

/**
 * Not a type — a STATE, and a temporary one: this person has too little watch history to
 * personalise from, so their row is built from popular titles until they've watched more. Shown
 * beside their watch history, where it explains the number sitting next to it.
 */
export function ColdStartBadge({ user }: { user: User }) {
  if (!user.cold_start) return null;
  return (
    <Badge
      variant="warning"
      title="Not enough watch history yet. What they get until then — popular titles, or no row at all — is the cold-start setting in Settings → Finding titles."
    >
      New viewer
    </Badge>
  );
}

/**
 * Type + state together, for the places that show one compact summary of a person (the dashboard
 * card and their own page). The Users table splits them across its own columns instead, so each
 * sits under the heading that describes it.
 */
export function UserBadges({
  user,
  emptyFallback = null,
}: {
  user: User;
  emptyFallback?: ReactNode;
}) {
  void emptyFallback; // every user now has a type, so there is never nothing to show
  return (
    <>
      <UserTypeBadge user={user} />
      <RestrictedBadge user={user} />
      <ColdStartBadge user={user} />
    </>
  );
}

/**
 * Plex no longer lists this account.
 *
 * `enabled=false` was carrying two unrelated meanings — the owner switched them off, or they lost
 * access to the server — and the list rendered both identically, so a departed account was an
 * unexplained row with no action attached to it. This is the difference, and it is what puts the
 * Remove control beside them.
 */
export function DepartedBadge({ user }: { user: User }) {
  if (!user.departed) return null;
  return (
    <Badge
      variant="outline"
      title="Plex no longer lists this account, so Shortlist switched them off and removed their rows. Remove them here to drop their pick history and clear them out of the list."
    >
      Left the server
    </Badge>
  );
}
