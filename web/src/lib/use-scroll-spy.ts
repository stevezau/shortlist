import { useEffect, useState } from "react";

/** Track which step section is currently in view, so a step rail can highlight it. Returns the id
 *  of the topmost section whose heading has scrolled into (or above) the top of the viewport. */
export function useScrollSpy(ids: string[]): string {
  const key = ids.join(",");
  const [active, setActive] = useState(ids[0] ?? "");
  useEffect(() => {
    // Guard for environments without the API (jsdom under vitest, very old browsers): the rail
    // still renders and jump-links work; it just won't auto-highlight the step in view.
    if (typeof IntersectionObserver === "undefined") return;
    const sections = ids
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null);
    if (sections.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        // The step whose top is nearest just below the rail offset wins. Prefer intersecting
        // sections; among them, the one closest to the top of the viewport.
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActive(visible[0].target.id);
      },
      // A band near the top of the viewport: a section is "active" once its top passes into the
      // top ~30% and until it leaves — so the rail tracks the heading you're reading.
      { rootMargin: "-10% 0px -70% 0px", threshold: 0 },
    );
    sections.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
    // Re-bind when the set of step ids changes (i.e. a different library tab).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return active;
}
