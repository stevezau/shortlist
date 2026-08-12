import { ArrowRight, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { cn } from "@/lib/utils";

type Tone = "default" | "success" | "warning" | "destructive";

const TONE: Record<Tone, string> = {
  default: "text-primary",
  success: "text-success",
  warning: "text-warning",
  destructive: "text-destructive-text",
};

/** One headline number with a label and an icon. Reads at a glance; a dense summary line does not. */
export function StatTile({
  icon: Icon,
  label,
  value,
  hint,
  tone = "default",
  title,
  to,
}: {
  icon: LucideIcon;
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
  /** Optional hover tooltip for guidance that's too long to sit in the visible hint. */
  title?: string;
  /** Makes the whole tile a link. The number becomes the way in to what it counts. */
  to?: string;
}) {
  // `min-w-0`: these sit in a `grid-cols-2`, and a grid item's default `min-width: auto` is its
  // min-content width — so a pair of tiles refused to shrink below 322px and ran off a 320px
  // screen. With it they take the column they were given.
  const className = cn(
    "block min-w-0 rounded-lg border bg-elevated px-4 py-3.5",
    // A linked tile has to look like one before it is hovered, or nobody finds it: the arrow on the
    // label is the always-visible affordance, the border/background lift is the confirmation.
    to &&
      "transition-colors hover:border-primary/40 hover:bg-elevated/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
  );

  const body = (
    <>
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
        <Icon className={cn("h-3.5 w-3.5", TONE[tone])} aria-hidden="true" />
        {label}
        {to && <ArrowRight className="h-3 w-3 shrink-0" aria-hidden="true" />}
      </div>
      <div className="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums break-words">
        {value}
      </div>
      {hint && (
        <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>
      )}
    </>
  );

  return to ? (
    <Link to={to} className={className} title={title}>
      {body}
    </Link>
  ) : (
    <div className={className} title={title}>
      {body}
    </div>
  );
}
