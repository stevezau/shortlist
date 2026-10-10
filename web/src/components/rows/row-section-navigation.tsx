import { useEffect, useRef, useState, type MouseEvent } from "react";

import { selectedClass } from "@/lib/selected";
import { scrollStrip, useScrollStrip } from "@/lib/use-scroll-strip";
import { cn } from "@/lib/utils";

export type RowSection = {
  id: string;
  label: string;
  /** A few words beside the label: how many settings the row overrides, or that it follows the server. */
  hint?: string;
};

/**
 * The row editor's one way around the page: a sticky list of links to sections that are all on the
 * page at once.
 *
 * It replaced section chips that opened folded groups. Folding hid each group's warnings until
 * someone opened it, and two navigation models on one page (chips and disclosures) meant a click
 * could scroll, unfold, or both. Links to sections that are always open do one thing.
 *
 * The highlight follows the section in view, read by an IntersectionObserver over a band near the
 * top of the viewport. Between two sections (nothing in the band) the last one stays current rather
 * than flickering back to the first.
 */
export function RowSectionNavigation({ sections }: { sections: RowSection[] }) {
  const [active, setActive] = useState(sections[0]?.id ?? "");
  const [stripRef, stripStyle] = useScrollStrip<HTMLElement>();
  const inBand = useRef(new Set<string>());
  const key = sections.map((section) => section.id).join(",");

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const order = key.split(",");
    const targets = order
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null);
    // The band starts below the sticky page chrome and ends 40% of the way down, so the section
    // being read is the one at the top of the screen, not one merely peeking in at the bottom.
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) inBand.current.add(entry.target.id);
          else inBand.current.delete(entry.target.id);
        }
        const first = order.find((id) => inBand.current.has(id));
        if (first) setActive(first);
      },
      { rootMargin: "-96px 0px -60% 0px" },
    );
    targets.forEach((target) => observer.observe(target));
    // The last section is short, so at the very bottom of the page its top may never reach the band.
    const atBottom = () => {
      const page = document.documentElement;
      if (page.scrollHeight > window.innerHeight && window.scrollY + window.innerHeight >= page.scrollHeight - 2) {
        const last = order.at(-1);
        if (last) setActive(last);
      }
    };
    window.addEventListener("scroll", atBottom, { passive: true });
    const band = inBand.current;
    return () => {
      observer.disconnect();
      window.removeEventListener("scroll", atBottom);
      band.clear();
    };
  }, [key]);

  const jump = (event: MouseEvent<HTMLAnchorElement>, id: string) => {
    const section = document.getElementById(id);
    if (!section) return;
    event.preventDefault();
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    section.scrollIntoView?.({ behavior: reduced ? "auto" : "smooth", block: "start" });
    // Focus follows the jump so the next Tab continues inside the section, not back at the list.
    section.querySelector<HTMLElement>("h2")?.focus({ preventScroll: true });
    // Keep the router's own history state: only the hash changes, so the URL stays linkable.
    window.history.replaceState(window.history.state, "", `#${id}`);
    setActive(id);
  };

  return (
    <nav
      ref={stripRef}
      style={stripStyle}
      aria-label="Row settings sections"
      className={cn(
        scrollStrip,
        // A horizontal scroller under the phone header; a vertical list beside the form from `lg` up.
        "sticky top-14 z-20 -mx-4 flex gap-1 overflow-x-auto border-b bg-background/95 px-4 py-2 backdrop-blur-sm",
        "md:top-0 md:-mx-8 md:px-8",
        "lg:top-6 lg:mx-0 lg:flex-col lg:overflow-visible lg:border-0 lg:bg-transparent lg:p-0 lg:backdrop-blur-none",
      )}
    >
      <p className="mb-1 hidden px-3 text-sm font-medium text-muted-foreground lg:block">Needs the Save button</p>
      {sections.map((section) => {
        const current = active === section.id;
        return (
          <a
            key={section.id}
            href={`#${section.id}`}
            aria-current={current ? "location" : undefined}
            onClick={(event) => jump(event, section.id)}
            className={cn(
              "flex shrink-0 items-center justify-between gap-2 whitespace-nowrap rounded-md border px-3 py-1.5 text-sm motion-safe:transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              current
                ? // The amber edge runs along the bottom in the phone scroller and down the left in the
                  // vertical list, the same two recipes as every other selected state (`selected.ts`).
                  cn(selectedClass, "lg:shadow-selected-y")
                : "border-transparent text-muted-foreground hover:bg-elevated hover:text-foreground",
            )}
          >
            {section.label}
            {section.hint && (
              <span className="ml-2 text-xs font-normal text-muted-foreground">{section.hint}</span>
            )}
          </a>
        );
      })}
    </nav>
  );
}
