import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";

const SELECTED = '[aria-selected="true"],[aria-pressed="true"],[aria-current]';
const FADE = "24px";

/**
 * Makes a horizontally scrolling strip of tabs or chips legible on a phone: the active item is
 * scrolled into view when the strip first appears, and an edge fades out on each side that still has
 * items off-screen. Returns `[ref, style]`: give both to the scroller, along with the `scrollStrip` classes.
 *
 * The fade is a mask on the scroller itself (`.scroll-strip`), so it stays put while the content scrolls and needs no
 * wrapper element. The jump is a direct `scrollLeft` write, never smooth, so it honours
 * `prefers-reduced-motion` without asking; it never calls `scrollIntoView`, which would also move the page.
 */
export function useScrollStrip<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const strip = ref.current;
    if (!strip) return;
    const left = strip.scrollLeft > 1;
    const right = strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 1;
    setEdges((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);

  useEffect(() => {
    const strip = ref.current;
    if (!strip) return;
    const active = strip.querySelector<HTMLElement>(SELECTED);
    if (active && strip.scrollWidth > strip.clientWidth) {
      strip.scrollLeft = active.offsetLeft - (strip.clientWidth - active.offsetWidth) / 2;
    }
    measure();
    strip.addEventListener("scroll", measure, { passive: true });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(strip);
    return () => {
      strip.removeEventListener("scroll", measure);
      observer?.disconnect();
    };
  }, [measure]);

  const style = {
    "--fade-left": edges.left ? FADE : "0px",
    "--fade-right": edges.right ? FADE : "0px",
  } as CSSProperties;

  return [ref, style] as const;
}

/** Classes for the scroller: the edge-fade mask (`.scroll-strip`, index.css, driven by the hook's
 *  custom properties) and no native scrollbar, which is chunky on a phone and adds nothing once the
 *  fade says there is more. */
export const scrollStrip = "scroll-strip [scrollbar-width:none] [&::-webkit-scrollbar]:hidden";
