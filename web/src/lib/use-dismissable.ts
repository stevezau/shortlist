import { useEffect, type RefObject } from "react";

/** Close an open dropdown on an outside click or Escape — what every dropdown is expected to do.
 *
 * Shared rather than written per component: the header has two of these side by side, and only one
 * of them had it. Clicking away from the other left it open over the page until you clicked its
 * button again, which reads as the panel being stuck.
 *
 * `mousedown`, not `click`: a click fires after the press completes, so a press that starts outside
 * and releases inside would not close it — and a menu that swallows the first click of a drag is
 * worse than one that closes eagerly.
 */
export function useDismissable(
  open: boolean,
  ref: RefObject<HTMLElement | null>,
  close: () => void,
): void {
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) close();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, ref, close]);
}
