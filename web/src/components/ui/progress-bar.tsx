import { cn } from "@/lib/utils";

interface ProgressBarProps {
  /** Completed steps. Omit (with `total`) for an indeterminate bar. */
  done?: number;
  /** Total steps. When 0 or undefined the bar renders indeterminate. */
  total?: number;
  /** Accessible name, e.g. "Syncing watch history". */
  label: string;
  className?: string;
}

/**
 * A slim determinate/indeterminate progress bar built on theme tokens.
 *
 * Pass `done`/`total` for a determinate fill; omit them (or pass `total=0`) for an indeterminate
 * sweep, used while waiting on an opaque call whose duration we can't measure. The sweep animation
 * is gated behind `motion-safe` so `prefers-reduced-motion` gets a static bar (rules/frontend.md).
 */
export function ProgressBar({
  done,
  total,
  label,
  className,
}: ProgressBarProps) {
  const determinate = typeof total === "number" && total > 0;
  const pct = determinate
    ? Math.min(100, Math.round(((done ?? 0) / total) * 100))
    : undefined;

  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={determinate ? 0 : undefined}
      aria-valuemax={determinate ? 100 : undefined}
      aria-valuenow={pct}
      className={cn(
        "h-2 w-full overflow-hidden rounded-full bg-muted",
        className,
      )}
    >
      {determinate ? (
        <div
          className="h-full rounded-full bg-primary transition-[width] duration-300 ease-out"
          style={{ width: `${pct}%` }}
        />
      ) : (
        // Indeterminate: a fixed-width sliver sweeps across. Static (a third-width bar) when the
        // user prefers reduced motion, so the "working" state is still visible without animation.
        <div className="h-full w-1/3 rounded-full bg-primary motion-safe:animate-progress-indeterminate" />
      )}
    </div>
  );
}
