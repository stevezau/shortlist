import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";

/**
 * A collapsible group of row settings, closed by default and captioned with what is inside it.
 *
 * The row dialog carries ~19 settings. Shown all at once they read as nineteen decisions, when in
 * practice a row is defined by about five — the rest have defaults that are right nearly always.
 * Flattening them all to the same visual weight put "Request tag" beside "Name" and made a simple
 * row look like a configuration exercise.
 *
 * `summary` is what keeps this from being a filing cabinet: each closed section states its CURRENT
 * values ("Movies and TV Shows · 2 sources · refreshes nightly"), so the answer to "is what I want in
 * here?" is on screen without opening anything. A disclosure that hides its contents *and* what they
 * are is worse than the flat list it replaced.
 *
 * Native <details>, like `PlacementHelp` — keyboard and screen-reader accessible with no library, and
 * it survives the dialog being scrolled or re-rendered without any open/closed state of our own.
 */
export function RowSection({
  title,
  summary,
  defaultOpen = false,
  children,
}: {
  title: string;
  /** The current values inside, in a few words. Shown whether open or closed. */
  summary: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details open={defaultOpen} className="group border-t pt-4">
      <summary className="-mx-2 flex cursor-pointer list-none items-center gap-2 rounded-md px-2 py-1.5 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <ChevronRight
          aria-hidden="true"
          className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90"
        />
        <span className="text-sm font-medium">{title}</span>
        {/* Dimmed and truncated: it is orientation, not a second copy of the controls. */}
        <span className="min-w-0 flex-1 truncate text-right text-xs text-muted-foreground">
          {summary}
        </span>
      </summary>
      <div className="space-y-4 pl-6 pt-4">{children}</div>
    </details>
  );
}
