import { runStatusLabel } from "@/lib/format";
import { hasPrivacyWarning } from "@/lib/run-privacy";
import type { Run, RunsSummary } from "@/lib/types";

export type RunHealth = { tone: "ok" | "warn" | "error" | "neutral"; label: string };

/**
 * What a run's result is called, everywhere it is named: the Runs list, the run page, the dashboard.
 *
 * "OK with warnings" is derived, not a server status (see {@link hasPrivacyWarning}), so every
 * surface has to go through here or one of them says "OK" for a run another calls a warning.
 */
export function runHealth(run: { status: string; privacy?: Run["privacy"] }): RunHealth {
  if (run.status === "error") return { tone: "error", label: "Failed" };
  if (run.status === "ok") {
    return hasPrivacyWarning(run) ? { tone: "warn", label: "OK with warnings" } : { tone: "ok", label: "OK" };
  }
  return { tone: "neutral", label: runStatusLabel(run.status) };
}

/** How the run history reads in one line: how many failed, how many warned, or all clean.
 *  Warnings are counted over the runs the page has loaded; the summary endpoint has no such count. */
export function historyHint(summary: RunsSummary, loaded: Pick<Run, "status" | "privacy">[]): string {
  // The caller only renders this when `total > 0`; an empty summary would otherwise fall through to
  // `ok === total` and claim every run finished cleanly when there are none.
  if (summary.total === 0) return "none yet";
  if (summary.error > 0) return `${summary.error} failed`;
  const warned = loaded.filter(hasPrivacyWarning).length;
  if (warned > 0) return `${warned} with warnings`;
  if (summary.ok === summary.total) return "all finished cleanly";
  return `${summary.ok} finished cleanly`;
}
