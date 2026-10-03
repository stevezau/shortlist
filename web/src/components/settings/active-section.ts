import { useEffect, useState } from "react";

/**
 * The section being read among `ids`, even when the address still names a previously clicked
 * anchor. A section in a hidden tab has no box, so it never counts: the jump list follows only the
 * tab on screen.
 */
export function useActiveSection(ids: readonly string[], initial: string): string {
  const [active, setActive] = useState(initial);
  const key = ids.join(" ");

  useEffect(() => {
    const sectionIds = key.split(" ");
    let frame = 0;
    const update = () => {
      const sections = sectionIds
        .map((id) => document.getElementById(id))
        .filter((node): node is HTMLElement => node !== null && node.closest("[hidden]") === null);
      if (!sections.length) return;
      const readingLine = window.matchMedia?.("(max-width: 767px)").matches ? 200 : 140;
      let current = sections[0]!;
      for (const section of sections) {
        if (section.getBoundingClientRect().top <= readingLine) current = section;
        else break;
      }
      // The final short section may never reach the reading line before the page ends.
      const page = document.documentElement;
      if (page.scrollHeight > window.innerHeight && window.scrollY + window.innerHeight >= page.scrollHeight - 2)
        current = sections.at(-1)!;
      setActive(current.id);
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(update);
    };
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    schedule();
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, [key]);

  return active;
}
