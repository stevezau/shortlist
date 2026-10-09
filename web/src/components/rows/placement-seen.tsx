import { hasHome, hasLibrary, joinPhrases } from "@/lib/placement";
import type { Placement } from "@/lib/types";

/** The surfaces a placement turns on, as a phrase; null when it claims none. */
function surfacesOn(placement: Placement): string | null {
  const surfaces = [
    hasHome(placement) ? "the Home screen" : "",
    hasLibrary(placement) ? "the Recommended shelf" : "",
  ].filter(Boolean);
  return surfaces.length > 0 ? joinPhrases(surfaces) : null;
}

/**
 * Who sees the row where, in two columns: a person who gets it, and the owner. The toggles below
 * decide it; this reads them back as what each side ends up looking at.
 */
export function PlacementSeen({
  placement,
  placementFriends,
  personName,
}: {
  placement: Placement;
  placementFriends: Placement;
  /** A person the row reaches, to name the left column; null names the group instead. */
  personName: string | null;
}) {
  const theirs = surfacesOn(placementFriends);
  const yours = surfacesOn(placement);
  return (
    <div className="grid divide-y rounded-lg border md:grid-cols-2 md:divide-x md:divide-y-0">
      <div className="space-y-1 p-4">
        <p className="text-sm font-medium text-muted-foreground">
          {personName ? `What ${personName} sees` : "What the people who get it see"}
        </p>
        <p className="text-sm">
          {theirs
            ? `Their own row on ${theirs}.`
            : "Their row doesn’t claim a shelf. It sits in the library’s Collections tab."}
        </p>
      </div>
      <div className="space-y-1 p-4">
        <p className="text-sm font-medium text-muted-foreground">What you see (owner)</p>
        <p className="text-sm">
          {yours ? `Your own row on ${yours}.` : "Your row doesn’t claim a shelf."}{" "}
          Plex shows you every person’s row in the library’s Collections tab, and never on your Home.
        </p>
      </div>
    </div>
  );
}
