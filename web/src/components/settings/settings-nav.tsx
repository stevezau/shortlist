import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

export type NavSection = { id: string; label: string };

/**
 * Sticky side rail for the Settings page: jump to a section and highlight the one currently in view.
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
      <ul className="sticky top-6 space-y-1">
        {sections.map((section) => {
          const current = active === section.id;
          return (
            <li key={section.id}>
              <a
                href={`#${section.id}`}
                aria-current={current ? "true" : undefined}
                className={cn(
                  "block rounded-md px-3 py-1.5 text-sm transition-colors",
                  current
                    ? "bg-muted font-medium text-foreground"
                    : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                )}
              >
                {section.label}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
