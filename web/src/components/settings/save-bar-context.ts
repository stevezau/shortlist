import { createContext, useContext, useEffect, useRef } from "react";

import type { AutosavedSettings } from "@/lib/autosave";

export type SaveState = Pick<AutosavedSettings, "isPending" | "isError" | "error" | "saved">;
export type Report = (id: string, state: SaveState | null, retry?: () => void) => void;

/** How a section tells the tab's save bar what it is doing (see `SaveBarProvider`). */
export const SaveBarContext = createContext<Report | null>(null);
/** What the save bar reads: every section's latest state, and a way to retry one. */
export const SaveStatesContext = createContext<{ states: Record<string, SaveState>; retry: (id: string) => void } | null>(null);

/**
 * Report a section's save state to the tab's save bar.
 *
 * @returns True when a save bar is listening — the section then leaves out its own readout. Rendered
 *   on its own (a unit test, another page) it keeps showing its own.
 */
export function useSaveBarReport(id: string, state: AutosavedSettings): boolean {
  const report = useContext(SaveBarContext);
  const { isPending, isError, error, saved, retry } = state;
  const retryRef = useRef(retry);
  useEffect(() => {
    retryRef.current = retry;
  });
  useEffect(() => {
    report?.(id, { isPending, isError, error, saved }, () => retryRef.current());
  }, [report, id, isPending, isError, error, saved]);
  useEffect(() => () => report?.(id, null), [report, id]);
  return report !== null;
}
