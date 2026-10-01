import { useEffect, useState, type RefObject } from "react";

/** The trace reading line sits below its mobile picker, or below the desktop page gutter. */
export function traceReadingLine(picker: HTMLElement | null): number {
  const height = picker?.getBoundingClientRect().height ?? 0;
  return picker && height > 0
    ? (parseFloat(window.getComputedStyle(picker).top) || 0) + height + 12
    : 24;
}

/** Follow the section whose heading has reached the same line used by navigation jumps. */
export function useScrollSpy(ids: string[], picker: RefObject<HTMLElement | null>): string {
  const key = ids.join(",");
  const [active, setActive] = useState(ids[0] ?? "");
  useEffect(() => {
    const sections = ids
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null);
    if (sections.length === 0) return;

    let frame = 0;
    const update = () => {
      const line = traceReadingLine(picker.current);
      let current = sections[0]!;
      for (const section of sections) {
        if (section.getBoundingClientRect().top <= line + 1) current = section;
        else break;
      }
      setActive(current.id);
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(update); };
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    sections.forEach((section) => resize?.observe(section));
    if (picker.current) resize?.observe(picker.current);
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    schedule();
    return () => {
      cancelAnimationFrame(frame);
      resize?.disconnect();
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
    // Re-bind when a different library supplies a different set of section ids.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, picker]);
  return active;
}
