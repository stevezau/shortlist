import { RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * A failed write, in plain English, with an optional button that re-fires exactly the same write.
 *
 * Every mutation reports failure through this, so a refused run, a rejected mute and a failed
 * user toggle all read the same way — and none of them can fail silently, which used to leave the
 * UI asserting a state the server had rejected.
 */
export function MutationAlert({
  error,
  fallback,
  lead,
  onRetry,
  className,
}: {
  error: unknown;
  /** Shown when the error isn't an ApiError (so it carries no server-written message). */
  fallback: string;
  /** What is true *now*, in front of the reason — e.g. "This row is still showing for them." */
  lead?: string;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-wrap items-center gap-2 text-sm text-destructive-text",
        className,
      )}
    >
      <span>
        {lead ? <span className="font-medium">{lead} </span> : null}
        {apiErrorMessage(error, fallback)}
      </span>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw aria-hidden="true" />
          Try again
        </Button>
      )}
    </div>
  );
}
