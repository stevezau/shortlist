import { useLayoutEffect, useRef, useState } from "react";

/**
 * The `top` for a `position: sticky` panel that may be taller than the screen.
 *
 * A panel that fits sticks `gap` below the top edge. A taller one gets a NEGATIVE top: it scrolls with
 * the page until its last line is `gap` above the bottom edge, then stays there — and scrolling the
 * page back up brings its heading back with the page. One scrollbar, the page's, and nothing in the
 * panel is ever out of reach. (The alternative, a panel with its own scrollbar, left its heading cut
 * off at the top of the page whenever it had been scrolled on its own.)
 */
export function stickyTop(
  panelHeight: number,
  viewportHeight: number,
  gap: number,
  bottomGap = gap,
): number {
  return Math.min(gap, viewportHeight - panelHeight - bottomGap);
}

/**
 * `stickyTop` kept current as the panel's content or the window changes size. `bottomGap` keeps the
 * panel's last line clear of anything pinned to the bottom of the screen, such as a sticky Save bar.
 */
export function useStickyTop<T extends HTMLElement>(gap = 24, bottomGap = gap) {
  const ref = useRef<T>(null);
  const [top, setTop] = useState(gap);

  useLayoutEffect(() => {
    const panel = ref.current;
    if (!panel) return;
    const update = () => setTop(stickyTop(panel.offsetHeight, window.innerHeight, gap, bottomGap));
    update();
    window.addEventListener("resize", update);
    // jsdom has no ResizeObserver; the window listener alone still covers a real browser's resize.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(panel);
    return () => {
      window.removeEventListener("resize", update);
      observer?.disconnect();
    };
  }, [gap, bottomGap]);

  return [ref, top] as const;
}
