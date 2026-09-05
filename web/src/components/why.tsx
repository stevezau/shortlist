import { Info } from "lucide-react";
import { useState } from "react";

/**
 * The "why" behind something on screen, behind an (i) rather than under it.
 *
 * These explanations, printed inline, double the height of the line they belong to and make a list
 * harder to scan — a list is read at a glance and the reasoning is consulted occasionally, so they
 * do not deserve equal weight.
 *
 * A real `<button>`, not a `title` tooltip: a hover-only explanation does not exist on a phone, and
 * this app is read on one. Click or focus toggles it, `aria-expanded` says which, and the text lands
 * in the DOM where a screen reader can reach it rather than in an attribute it may skip.
 *
 * Lives here rather than in `dashboard/` because it is not a dashboard idea: it is this app's answer
 * to "a long explanation that must stay reachable without taking a line" wherever that comes up —
 * the dashboard's findings, a person's pick outcomes, a request stuck in a state only a paragraph
 * can explain.
 */
export function Why({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      {" "}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={open ? "Hide why" : "Why?"}
        className="inline-flex translate-y-px items-center rounded-full text-muted-foreground/60 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Info className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
      {open && (
        <span className="mt-0.5 block text-xs leading-snug text-muted-foreground/70">
          {text}
        </span>
      )}
    </>
  );
}
