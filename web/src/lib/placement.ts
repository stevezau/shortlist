import type { Placement, User } from "@/lib/types";

/** Where a row shows, in words. `Placement` has FOUR values — the "off" arm was missing, so a row
 *  hidden from every shelf was badged "Shows on: Home & Library": a confident, specific, false claim
 *  about who can see it. */
export function placementLabel(placement: Placement): string {
  switch (placement) {
    case "home":
      return "Home";
    case "library":
      return "Library";
    case "off":
      return "Nowhere";
    default:
      return "Home & Library";
  }
}

/** Decode/encode one audience's placement as its two independent surface flags. All four
 *  combinations are representable — "off" is a real state, not a fallback to Library. */
export function hasLibrary(p: Placement): boolean {
  return p === "both" || p === "library";
}

export function hasHome(p: Placement): boolean {
  return p === "both" || p === "home";
}

export function encode(library: boolean, home: boolean): Placement {
  if (library && home) return "both";
  if (home) return "home";
  if (library) return "library";
  return "off";
}

/** The owner's display name, for labelling the "Just me" column with the account it actually means.
 *  Null while the roster is still loading — the column then reads "Just me" with no subtitle rather
 *  than flashing a wrong name. */
export function ownerName(users: User[]): string | null {
  const owner = users.find((user) => user.user_type === "owner");
  if (!owner) return null;
  return owner.display_name || owner.nickname || owner.username || null;
}

/** Everyone who isn't the owner. Counts the whole roster, enabled or not — this labels who the
 *  column is ABOUT, not who a run will reach. */
export function othersCount(users: User[]): number {
  return users.filter((user) => user.user_type !== "owner").length;
}

/** "a", "a and b", "a, b and c" — for reading a placement back as a sentence. */
export function joinPhrases(parts: string[]): string {
  if (parts.length <= 1) return parts[0] ?? "";
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

function surfaces(home: boolean, library: boolean, whose: string): string {
  return joinPhrases(
    [
      home ? `${whose} Home screen` : "",
      library ? `${whose} Recommended shelf` : "",
    ].filter(Boolean),
  );
}

/**
 * The toggles restated as the outcome they produce. Without this the grid only describes Plex's
 * flags, leaving "what did I just do to this row" to be inferred from four switches and an essay.
 */
export function placementSummary(
  ownerLibrary: boolean,
  ownerHome: boolean,
  friendsLibrary: boolean,
  friendsHome: boolean,
  isShared: boolean,
): string {
  if (isShared) {
    const where: string[] = [];
    if (ownerHome && friendsHome) where.push("everyone’s Home screen");
    else if (ownerHome) where.push("your Home screen");
    else if (friendsHome) where.push("everyone else’s Home screen");
    if (ownerLibrary || friendsLibrary) where.push("the Recommended shelf");
    return `This row shows on ${joinPhrases(where)}.`;
  }

  const yours = surfaces(ownerHome, ownerLibrary, "your");
  const theirs = surfaces(friendsHome, friendsLibrary, "their");
  const first = yours
    ? `Your row shows on ${yours}.`
    : "Your row doesn’t claim a shelf.";
  const second = theirs
    ? `Everyone else’s row shows on ${theirs}.`
    : "Everyone else’s row doesn’t claim a shelf.";
  return `${first} ${second}`;
}
