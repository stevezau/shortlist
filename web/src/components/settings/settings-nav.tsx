import type { LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

export type NavSection = { id: string; label: string; icon: LucideIcon };

/**
 * Sticky side rail for the Settings page: jump to a section and highlight the one currently in view.
 * Styled to match the app's main sidebar (icon + label, amber active accent) so it reads as part of
 * the same nav language rather than a plain list.
 *
 * Hidden below `lg` (the page is a single scroll on mobile). Jumping uses native anchor links, so it
 * works even without JS; the scroll-spy highlight is a progressive enhancement via IntersectionObserver
 * (guarded so it degrades to "first section active" where the API is unavailable, e.g. jsdom).
 */
export function SettingsNav({ sections }: { sections: NavSection[] }) {
  const [active, setActive] = useState(sections[0]?.id ?? "");

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        const inView = entries
          .filter((e) => e.isIntersecting)
          .sort(
            (a, b) => a.boundingClientRect.top - b.boundingClientRect.top,
          )[0];
        if (inView) setActive(inView.target.id);
      },
      // Fire when a section's heading is in the upper part of the viewport.
      { rootMargin: "-15% 0px -75% 0px" },
    );
    const seen = sections
      .map((s) => document.getElementById(s.id))
      .filter((el): el is HTMLElement => el !== null);
    seen.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [sections]);

  return (
    <nav aria-label="Settings sections" className="hidden lg:block">
      <div className="sticky top-6 space-y-1">
        <p className="px-3 pb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground/70">
          On this page
        </p>
        {sections.map(({ id, label, icon: Icon }) => {
          const current = active === id;
          return (
            <a
              key={id}
              href={`#${id}`}
              aria-current={current ? "true" : undefined}
              className={cn(
                "group relative flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                current
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              {/* Left accent bar on the active item — matches the main sidebar's "you are here". */}
              <span
                aria-hidden="true"
                className={cn(
                  "absolute left-0 top-1/2 h-5 -translate-y-1/2 rounded-r-full bg-primary transition-all",
                  current ? "w-1 opacity-100" : "w-0 opacity-0",
                )}
              />
              <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
              {label}
            </a>
          );
        })}
      </div>
    </nav>
  );
}
