import { createContext, useContext } from "react";

/**
 * Where the row editor's "Server defaults" list lives.
 *
 * The inheritable settings are rendered by the fields they belong to (a kind's own block, the
 * schedule…), but they read as one list of one-line rows. A field that finds a target here renders
 * its row into it; without one (a field on its own) it renders where it is.
 */
export const OverridesTarget = createContext<HTMLElement | null>(null);

export function useOverridesTarget(): HTMLElement | null {
  return useContext(OverridesTarget);
}

/** Where each inheritable setting sits in the list. The rows are placed by CSS `order`, so the
 *  list reads the same whichever field mounted first. */
const ORDER: Record<string, number> = {
  refresh_days: 10,
  watched_pct: 20,
  recency: 30,
  recent_count: 40,
  max_seeds: 50,
  idle_hold_days: 60,
  cold_start: 70,
};

export function overrideOrder(setting: string): number {
  return ORDER[setting] ?? 100;
}

