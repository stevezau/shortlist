import { Loader2, ShieldAlert, ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PRIVACY_TIER_LABELS } from "@/lib/constants";

export type PrivacyPhase = "idle" | "running" | "passed" | "failed";

export interface PrivacyPanelProps {
  phase: PrivacyPhase;
  /** Live log lines streamed from `privacy.probe.step` events. */
  logLines: string[];
  /** Tier results (T1/T2/PROBE) once the check finished, else null. */
  tiers: Record<string, boolean> | null;
  /** Error text when the check itself failed to run (not a privacy fail). */
  error: string | null;
  skipped: boolean;
  onRun: () => void;
  onSkip: () => void;
}

function TierBadges({ tiers }: { tiers: Record<string, boolean> }) {
  return (
    <div className="flex flex-wrap gap-2">
      {Object.entries(tiers).map(([tier, ok]) => (
        // Raw tier code kept as a tooltip so support can still map back to T1/T2/PROBE.
        <Badge
          key={tier}
          variant={ok ? "success" : "destructive"}
          title={tier.toUpperCase()}
        >
          {PRIVACY_TIER_LABELS[tier.toUpperCase()] ?? tier.toUpperCase()}:{" "}
          {ok ? "kept private" : "visible to others"}
        </Badge>
      ))}
    </div>
  );
}

function LiveLog({ lines }: { lines: string[] }) {
  if (lines.length === 0) return null;
  return (
    <ol
      aria-live="polite"
      aria-label="Privacy Check progress"
      className="max-h-48 space-y-1 overflow-y-auto rounded-md border bg-card p-3 font-mono text-xs text-muted-foreground"
    >
      {lines.map((line, i) => (
        <li key={i}>{line}</li>
      ))}
    </ol>
  );
}

/**
 * Presentational panel for wizard step 5 (design doc §3). The container owns
 * the mutation + SSE wiring; this renders the four phases and the
 * skip-behind-a-fold escape hatch.
 */
export function PrivacyPanel({
  phase,
  logLines,
  tiers,
  error,
  skipped,
  onRun,
  onSkip,
}: PrivacyPanelProps) {
  return (
    <div className="space-y-4">
      {phase === "idle" && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Shortlist creates a throwaway test row, hides it from a stand-in
            viewer account, and confirms that account truly can't see it — then
            cleans everything up. Your server is untouched either way.
          </p>
          <Button onClick={onRun}>
            <ShieldCheck aria-hidden="true" />
            Run Privacy Check (~90 seconds)
          </Button>
        </div>
      )}

      {phase === "running" && (
        <div className="space-y-3">
          <p className="inline-flex items-center gap-2 text-sm">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Checking your server keeps rows private…
          </p>
          <LiveLog lines={logLines} />
        </div>
      )}

      {phase === "passed" && (
        <div
          role="status"
          className="space-y-3 rounded-lg border border-success/50 bg-success/10 p-4"
        >
          <p className="inline-flex items-center gap-2 text-lg font-semibold text-success">
            <ShieldCheck className="h-5 w-5" aria-hidden="true" />
            Your server keeps rows private
          </p>
          {tiers && <TierBadges tiers={tiers} />}
          <LiveLog lines={logLines} />
        </div>
      )}

      {phase === "failed" && (
        <div
          role="alert"
          className="space-y-3 rounded-lg border border-destructive/50 bg-destructive/10 p-4"
        >
          <p className="inline-flex items-center gap-2 text-lg font-semibold text-destructive">
            <ShieldAlert className="h-5 w-5" aria-hidden="true" />
            Privacy Check failed
          </p>
          <p className="text-sm">
            {error ??
              "The probe row was visible where it shouldn't be. Rows built now would NOT be private on this server."}
          </p>
          {tiers && <TierBadges tiers={tiers} />}
          <LiveLog lines={logLines} />
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="outline" onClick={onRun}>
              Run the check again
            </Button>
          </div>
          <details className="text-sm">
            <summary className="cursor-pointer text-muted-foreground">
              I understand the risk
            </summary>
            <div className="mt-2 space-y-2">
              <p className="text-muted-foreground">
                Continuing means every user's row may be visible to every other
                user on this server. You can re-run the Privacy Check later from
                the dashboard.
              </p>
              <Button variant="destructive" size="sm" onClick={onSkip}>
                Skip — continue without privacy verification
              </Button>
            </div>
          </details>
        </div>
      )}

      {skipped && phase !== "passed" && (
        <p className="text-sm text-primary">
          Privacy verification skipped — you can continue, but rows may not be
          private.
        </p>
      )}
    </div>
  );
}
