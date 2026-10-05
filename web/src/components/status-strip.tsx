import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export type StatusTone = "ok" | "warn" | "error" | "neutral";

const DOT: Record<StatusTone, string> = {
  ok: "bg-success",
  warn: "bg-warning",
  error: "bg-destructive",
  neutral: "bg-muted-foreground",
};

/**
 * A row of facts that answers "how did it go" at a glance — the dashboard's four and a run's five.
 *
 * One panel, hairline dividers between cells, never a card per fact: a card inside a card is the
 * one depth this app does not use. The dividers are the panel's border colour showing through a 1px
 * grid gap, so they follow the cells wherever the grid wraps (four across on a desktop, two on a
 * phone) without a border rule per breakpoint.
 */
export function StatusStrip({
  label,
  children,
  className,
}: {
  /** Names the region for a screen reader ("Status", "Run summary"). */
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      aria-label={label}
      className={cn("overflow-hidden rounded-lg border bg-card shadow-elevated", className)}
    >
      {children}
    </section>
  );
}

/**
 * One line of cells inside a {@link StatusStrip}. Pass the grid columns in `className` as full class
 * strings — Tailwind cannot see an interpolated one.
 *
 * An odd cell left alone on the last line of the two-column phone grid spans both columns, rather
 * than leaving a slot of bare divider colour beside it. Callers whose wider grid fits every cell
 * reset that with `sm:`/`lg:` `[&>*:last-child:nth-child(odd)]:col-span-1`.
 */
export function StatusRow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "grid grid-cols-2 gap-px bg-border [&+&]:border-t [&>*:last-child:nth-child(odd)]:col-span-2",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** One fact: a small label (with a status dot or an icon), the value, and a line under it. */
export function StatusCell({
  label,
  icon: Icon,
  tone,
  value,
  sub,
  title,
  size = "lg",
  testId,
}: {
  label: string;
  /** Shown before the label when there is no `tone`. */
  icon?: LucideIcon;
  /** A status dot before the label — this fact is good, a warning, an error, or nothing yet. */
  tone?: StatusTone;
  value: ReactNode;
  sub?: ReactNode;
  /** Hover text for guidance too long for the line under the value. */
  title?: string;
  /** `sm` for a secondary line of figures that should not compete with the main one. */
  size?: "lg" | "sm";
  testId?: string;
}) {
  return (
    <div className="min-w-0 bg-card px-4 py-3" title={title} data-testid={testId}>
      <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-faint-foreground">
        {tone ? (
          <span className={cn("h-2 w-2 shrink-0 rounded-full", DOT[tone])} aria-hidden="true" />
        ) : (
          Icon && <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        )}
        {label}
      </div>
      <div
        className={cn(
          "mt-1 flex flex-wrap items-center gap-2 font-semibold leading-tight tabular-nums [overflow-wrap:anywhere]",
          size === "lg" ? "text-lg" : "text-base",
        )}
      >
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{sub}</div>}
    </div>
  );
}
