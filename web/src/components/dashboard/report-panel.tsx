import type { ReactNode } from "react";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/** The padding every line of a flush panel's list carries, so the dashboard's lists line up. */
export const panelRowClass = "px-4 py-3 sm:px-5";

/**
 * One dashboard summary panel: its title and one line under it, a hairline, then the body.
 *
 * `flush` hands the body edge to edge to a list whose lines carry {@link panelRowClass} and sit on
 * hairlines of their own (`divide-y`); otherwise the body is padded like the header.
 */
export function ReportPanel({
  title,
  hint,
  actions,
  flush = false,
  children,
  className,
}: {
  title: string;
  hint?: ReactNode;
  /** A control for the head row, beside the title. */
  actions?: ReactNode;
  flush?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    // `min-w-0` because this Card is a GRID ITEM, and a grid item's default `min-width: auto`
    // resolves to its min-content width. Without it the card sized itself to its widest line (508px
    // on a 358px column), overflowed the page, and — because it then had room to spare — nothing
    // inside ever truncated. The dashboard scrolled 134px sideways on a phone.
    <Card className={cn("min-w-0 overflow-hidden", className)}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b px-4 py-3.5 sm:px-5">
        <div className="min-w-0">
          <h2 className="text-base font-semibold tracking-tight">{title}</h2>
          {hint && <p className="mt-0.5 text-sm text-muted-foreground">{hint}</p>}
        </div>
        {actions}
      </div>
      {flush ? children : <div className="space-y-3 px-4 py-4 sm:px-5">{children}</div>}
    </Card>
  );
}
