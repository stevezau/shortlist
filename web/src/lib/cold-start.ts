/**
 * What a row does for someone with too little watch history to recommend from.
 *
 * Shared by the global setting (Settings → Finding titles) and the per-row override (Row editor), so
 * the two screens can never drift into naming the same choice differently — the mistake the
 * seeds/recent-watches pair already shipped once.
 *
 * Mirrors `COLD_STARTS` in `shortlist/server/api/collections.py` and
 * `recommendations.cold_start` in `shortlist/server/settings_store.py`.
 */
export const COLD_STARTS = ["popular", "skip"] as const;

export type ColdStart = (typeof COLD_STARTS)[number];

/** A row stores `null` to mean "inherit the global". */
export type RowColdStart = ColdStart | null;

export const COLD_START_LABELS: Record<ColdStart, string> = {
  popular: "Show the server’s highest-rated titles",
  skip: "Don’t build their row",
};

/** The consequence, not a restatement of the label — what actually lands on their Plex. */
export const COLD_START_HINTS: Record<ColdStart, string> = {
  popular:
    "They still get a row, filled with what rates highest on this server, until they’ve watched enough for real recommendations.",
  skip: "No row is created, and any row they already have is removed. It appears on its own once they’ve watched enough.",
};

export function asColdStart(value: unknown): ColdStart {
  return value === "skip" ? "skip" : "popular";
}
