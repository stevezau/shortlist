import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The one header every screen uses, so the app has a single rhythm: a title, one line under it,
 * and an optional slot for actions on the right. No icon tile: the nav already shows the page's
 * icon, and the tile pushed every title off the left edge the rest of the page aligns to.
 *
 * `title` takes a node so a row name can render its placeholders as chips (`RowName`) rather than
 * as raw `{library_name}` braces.
 */
export function PageHeader({
  title,
  subtitle,
  actions,
  className,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    // `flex-wrap` so a wide action group drops to its own line instead of squeezing the subtitle:
    // without it, Runs' three buttons left the sentence a ~60px column reading one word per line.
    <header
      className={cn(
        "mb-6 flex flex-col gap-4 sm:flex-row sm:flex-wrap sm:items-start sm:justify-between",
        className,
      )}
    >
      {/* The `min-w` is what makes the wrap fire. `flex-1` is `flex:1 1 0%`, so the line-breaking
          step sees a zero-width item and never breaks — it just starves the title instead. Flex
          clamps that hypothetical size by `min-width`, so a floor here is the whole mechanism. */}
      <div className="min-w-0 flex-1 space-y-1 sm:min-w-[16rem]">
        <h1 className="text-2xl font-semibold tracking-tight [overflow-wrap:anywhere]">{title}</h1>
        {subtitle && <p className="text-sm text-muted-foreground">{subtitle}</p>}
      </div>
      {/* `max-w-full`: alone on its line, a group wider than the header (Users' search, filter bar
          and buttons at 1024px) wraps inside itself instead of pushing the page sideways — a
          `shrink-0` item's size is its content's, and nothing else caps it. */}
      {actions && <div className="flex max-w-full shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}
