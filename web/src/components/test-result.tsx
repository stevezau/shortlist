import { CheckCircle2, XCircle } from "lucide-react";

import { apiErrorMessage } from "@/lib/api";
import type { ConnectionTestResult } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The pass/fail line under a "Test connection" button: a green check + message when the test
 * resolved ok, a red cross + message when it failed or threw. One component so every connection
 * card, Arr card, OMDb field, and wizard step reports a test identically.
 *
 * Pass `result` for a resolved {@link ConnectionTestResult}, or `error` for a thrown error (with
 * `errorFallback` as its plain-English default). Nothing renders until one is supplied.
 */
export function TestResult({
  result,
  error,
  errorFallback = "The test could not be completed.",
  as: Tag = "p",
  className,
}: {
  result?: ConnectionTestResult;
  error?: unknown;
  errorFallback?: string;
  /** `span` when the line sits inline beside the button; `p` (default) when it stands alone. */
  as?: "p" | "span";
  className?: string;
}) {
  if (result) {
    const Icon = result.ok ? CheckCircle2 : XCircle;
    return (
      <Tag
        role={result.ok ? undefined : "alert"}
        className={cn(
          "flex items-center gap-1.5 text-sm",
          result.ok ? "text-success" : "text-destructive",
          className,
        )}
      >
        <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
        {result.message}
      </Tag>
    );
  }
  if (error !== undefined && error !== null) {
    return (
      <Tag
        role="alert"
        className={cn(
          "flex items-center gap-1.5 text-sm text-destructive",
          className,
        )}
      >
        <XCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
        {apiErrorMessage(error, errorFallback)}
      </Tag>
    );
  }
  return null;
}
