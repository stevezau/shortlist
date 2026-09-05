import { useEffect } from "react";
import { useLocation } from "react-router";

/**
 * Jump to `#some-id` once that element actually exists.
 *
 * A page whose anchors render behind a query can't rely on the browser's own anchor jump: on a COLD
 * load the browser looks for `#danger` while the page is still a skeleton, finds nothing, and gives
 * up — so `/settings#danger` from a bookmark or an external link just sat at the top. (In-page nav
 * clicks always worked; by then the element is mounted and the native jump finds it.)
 *
 * Pass `ready` as "the content this hash points into has rendered". It is load-bearing as a
 * DEPENDENCY, not as the guard: having it in the dep array is what re-runs the effect on the render
 * that finally mounts the sections. Drop it from the array and the fix silently stops working — the
 * effect runs once on mount, finds nothing, and never looks again. (The early return is only there
 * to skip pointless work; `?.` already tolerates a missing element.)
 *
 * @param ready Whether the anchor targets have rendered yet.
 */
export function useHashScroll(ready: boolean): void {
  const { hash } = useLocation();
  useEffect(() => {
    if (!ready || !hash) return;
    // Centred and smooth, matching the in-page jumps the row editor already does to this same
    // anchor — a bare `scrollIntoView()` pins the target to the very top of the viewport, so the
    // two ways of reaching one section landed differently.
    //
    // The media query is checked in JS because the CSS guard cannot reach this: `index.css` sets
    // `scroll-behavior: auto !important` under `prefers-reduced-motion`, and the `behavior` option
    // passed here overrides the computed value regardless (measured — the scroll animated
    // identically in both modes). Four other call sites in this codebase check it the same way.
    const reduce = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    )?.matches;
    document.getElementById(hash.slice(1))?.scrollIntoView({
      behavior: reduce ? "auto" : "smooth",
      block: "center",
    });
  }, [ready, hash]);
}
