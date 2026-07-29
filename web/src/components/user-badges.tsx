import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
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

/** The human name of a Plex restriction preset, for copy that names what the owner actually set. */
const PROFILE_NAMES: Record<string, string> = {
  little_kid: "Younger Kid",
  older_kid: "Older Kid",
  teen: "Teen",
};

export function profileName(user: User): string {
  const key = user.restriction_profile ?? "";
  return PROFILE_NAMES[key] ?? key;
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
      title={`Plex's ${profileName(user)} restriction profile is set on this account. Plex hides every collection from it, so no row is built — and Plex refuses privacy filters for profiled accounts. Set the Restriction Profile to None in Plex to give this person recommendations.`}
    >
      {profileName(user)}
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
      title="Not enough watch history yet — their row is built from popular titles until there is."
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
