import { useEffect, useRef, useState, type RefObject } from "react";

const SECTION_NAMES = ["Appearance", "Row settings", "Audience", "Titles & filters", "Schedule", "Plex placement", "Requests"] as const;

/** Navigation follows the visible section; moving focus must never undo its measured scroll. */
export function RowSectionNavigation({ root, showRequests }: {
  root: RefObject<HTMLDivElement | null>;
  showRequests: boolean;
}) {
  const nav = useRef<HTMLElement>(null);
  const [active, setActive] = useState<string>(SECTION_NAMES[0]);
  const highlight = useRef<{ group: HTMLElement; timer: ReturnType<typeof setTimeout> } | null>(null);
  const names = showRequests ? SECTION_NAMES : SECTION_NAMES.slice(0, -1);

  const readingLine = () => {
    if (!nav.current) return 0;
    const top = parseFloat(window.getComputedStyle(nav.current).top) || 0;
    return top + nav.current.getBoundingClientRect().height + 12;
  };

  useEffect(() => {
    const container = root.current;
    if (!container) return;
    let frame = 0;
    const update = () => {
      const groups = [...container.querySelectorAll<HTMLDetailsElement>("details[data-settings-group]")];
      if (!groups.length || !nav.current?.getBoundingClientRect().height) return;
      let current = groups[0]!;
      const line = readingLine();
      for (const group of groups) {
        if (group.getBoundingClientRect().top <= line + 1) current = group;
        else break;
      }
      if (document.documentElement.scrollHeight > window.innerHeight && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) current = groups.at(-1)!;
      setActive(current.dataset.settingsGroup ?? SECTION_NAMES[0]);
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(update); };
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    observer?.observe(container);
    if (nav.current) observer?.observe(nav.current);
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    container.addEventListener("toggle", schedule, true);
    schedule();
    return () => {
      cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      container.removeEventListener("toggle", schedule, true);
      if (highlight.current) {
        clearTimeout(highlight.current.timer);
        delete highlight.current.group.dataset.navigationHighlight;
        highlight.current = null;
      }
    };
  }, [root, showRequests]);

  const goTo = (name: string) => {
    const group = [...(root.current?.querySelectorAll<HTMLDetailsElement>("details[data-settings-group]") ?? [])]
      .find((candidate) => candidate.dataset.settingsGroup === name);
    if (!group) return;
    group.open = true;
    group.querySelector("summary")?.focus({ preventScroll: true });
    const top = Math.max(0, window.scrollY + group.getBoundingClientRect().top - readingLine());
    window.scrollTo({ top, behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
    setActive(name);
    if (highlight.current) {
      clearTimeout(highlight.current.timer);
      delete highlight.current.group.dataset.navigationHighlight;
    }
    group.dataset.navigationHighlight = "true";
    highlight.current = { group, timer: setTimeout(() => {
      delete group.dataset.navigationHighlight;
      highlight.current = null;
    }, 1400) };
  };

  return <nav ref={nav} aria-label="Row settings sections" className="sticky top-16 z-20 flex flex-wrap items-center gap-2 rounded-lg border bg-background/95 p-2 shadow-sm backdrop-blur-sm md:top-3">
    {names.map((name) => <button key={name} type="button" aria-current={active === name ? "location" : undefined}
      className={`rounded-md border px-3 py-2 text-xs font-medium motion-safe:transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${active === name ? "border-primary/50 bg-primary/15 text-primary" : "border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground"}`}
      onClick={() => goTo(name)}>{name}</button>)}
  </nav>;
}
