import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

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
}: {
  icon: LucideIcon;
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
  /** Optional hover tooltip for guidance that's too long to sit in the visible hint. */
  title?: string;
}) {
  return (
    <div className="rounded-lg border bg-elevated px-4 py-3.5" title={title}>
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
        <Icon className={cn("h-3.5 w-3.5", TONE[tone])} aria-hidden="true" />
        {label}
      </div>
      <div className="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums">
        {value}
      </div>
      {hint && (
        <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>
      )}
    </div>
  );
}
