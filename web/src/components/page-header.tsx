import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The one header every top-level screen uses, so the app has a single rhythm: an icon tile, a
 * title, a one-line subtitle, and an optional slot for actions on the right.
 */
export function PageHeader({
  icon: Icon,
  title,
  subtitle,
  actions,
  className,
}: {
  icon: LucideIcon;
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    // `flex-wrap` so a wide action group drops to its own line instead of squeezing the subtitle:
    // without it, Runs' three buttons left the sentence a ~60px column reading one word per line.
    <header
      className={cn(
        "mb-6 flex flex-col gap-4 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between",
        className,
      )}
    >
      {/* The `min-w` is what makes the wrap fire. `flex-1` is `flex:1 1 0%`, so the line-breaking
          step sees a zero-width item and never breaks — it just starves the title instead. Flex
          clamps that hypothetical size by `min-width`, so a floor here is the whole mechanism. */}
      <div className="flex min-w-[16rem] flex-1 items-start gap-3">
        <span
          aria-hidden="true"
          className="mt-0.5 grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-border bg-elevated text-primary"
        >
          <Icon className="h-5 w-5" />
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          {subtitle && (
            <p className="text-sm text-muted-foreground">{subtitle}</p>
          )}
        </div>
      </div>
      {actions && (
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      )}
    </header>
  );
}
