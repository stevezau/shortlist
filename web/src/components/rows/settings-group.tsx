import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";

/**
 * A titled group of row settings on the editor page.
 *
 * Open by default, unlike the accordions this replaced. Those existed because the editor was a
 * modal capped at 90% of the viewport, so nineteen settings could only fit by hiding most of them —
 * and what they hid included the warnings that only matter BEFORE you save. On a page there is room
 * to leave everything visible, and a heading with a sentence under it does the orienting the
 * collapsed summaries were doing.
 *
 * `defaultOpen={false}` is kept for the two groups that really are optional (artwork, request
 * tags), where a closed section is a fair signal that most people can skip it.
 */
export function SettingsGroup({
  title,
  description,
  summary,
  defaultOpen = true,
  children,
}: {
  title: string;
  /** One line on what this group decides. The heading alone leaves "…and what does that mean?". */
  description: string;
  /** Current values in a few words — only rendered while closed, where it is the sole clue to
   *  whether opening the group is worth it. */
  summary?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details
      open={defaultOpen}
      className="group rounded-lg border bg-card px-5 py-4"
    >
      <summary className="-mx-2 flex cursor-pointer list-none items-start gap-2 rounded-md px-2 py-1 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <ChevronRight
          aria-hidden="true"
          className="mt-1 size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90"
        />
        <span className="min-w-0 flex-1">
          <span className="block font-medium">{title}</span>
          <span className="block text-sm text-muted-foreground">
            {description}
          </span>
          {summary && (
            <span className="mt-1 block truncate text-xs text-muted-foreground group-open:hidden">
              {summary}
            </span>
          )}
        </span>
      </summary>
      <div className="space-y-4 pt-4">{children}</div>
    </details>
  );
}
