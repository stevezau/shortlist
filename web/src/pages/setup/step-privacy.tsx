import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { PrivacyPanel, type PrivacyPhase } from "@/components/privacy-panel";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type { PrivacyCheckResult } from "@/lib/types";

import type { StepProps } from "./step-props";

/**
 * Step 5 — the Privacy Check (design doc §3 step 5). Live log lines arrive
 * on the SSE stream while POST /api/privacy/check runs the probe. Next stays
 * blocked until it passes or the owner explicitly accepts the risk.
 */
export function StepPrivacy({ data, update }: StepProps) {
  const [logLines, setLogLines] = useState<string[]>([]);
  const [result, setResult] = useState<PrivacyCheckResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  useSSE({
    onPrivacyProbeStep: (event) =>
      setLogLines((lines) => [...lines, event.message]),
  });

  const check = useMutation({
    // Step 5 runs the FULL probe (throwaway collection + canary view), not the read-only pass.
    mutationFn: () => api.runPrivacyCheck({ probe: true }),
    onMutate: () => {
      setLogLines([]);
      setResult(null);
      setError(null);
    },
    onSuccess: (checkResult) => {
      setResult(checkResult);
      if (checkResult.passed) update({ privacy_passed: true });
      void queryClient.invalidateQueries({ queryKey: queryKeys.privacy });
    },
    onError: (caught) => {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "The Privacy Check could not run. Check the server log and try again.",
      );
    },
  });

  let phase: PrivacyPhase = "idle";
  if (check.isPending) phase = "running";
  else if (result?.passed || data.privacy_passed) phase = "passed";
  else if (result !== null || error !== null) phase = "failed";

  return (
    <PrivacyPanel
      phase={phase}
      logLines={logLines}
      tiers={result?.tiers ?? null}
      error={error}
      skipped={data.privacy_skipped === true}
      onRun={() => check.mutate()}
      onSkip={() => update({ privacy_skipped: true })}
    />
  );
}
