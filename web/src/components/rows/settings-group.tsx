import { useEffect, useRef, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";

/** Mounted disclosures keep every draft control intact while quieter sections are folded. */
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
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const revealHash = () => {
      const id = window.location.hash.slice(1);
      const target = id ? document.getElementById(id) : null;
      if (target && ref.current?.contains(target)) ref.current.open = true;
    };
    revealHash();
    window.addEventListener("hashchange", revealHash);
    return () => window.removeEventListener("hashchange", revealHash);
  }, []);
  return (
    <details
      ref={ref}
      data-settings-group={title}
      open={defaultOpen}
      className="group rounded-lg border bg-card px-5 py-4 data-[navigation-highlight=true]:ring-2 data-[navigation-highlight=true]:ring-primary/50 motion-safe:transition-shadow"
    >
      <summary className="-mx-2 flex cursor-pointer list-none items-start gap-2 rounded-md px-2 py-1 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <ChevronRight
          aria-hidden="true"
          className="mt-1 size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90"
        />
        <span className="min-w-0 flex-1">
          <span role="heading" aria-level={2} className="block font-medium">{title}</span>
          <span className="block text-sm text-muted-foreground">
            {description}
          </span>
          {summary && (
            <span className="mt-1 block text-xs text-muted-foreground group-open:hidden">
              {summary}
            </span>
          )}
        </span>
      </summary>
      <div className="space-y-4 pt-4">{children}</div>
    </details>
  );
}
