import { useRef, useState } from "react";

export type CopyState = "idle" | "copied" | "error";

/**
 * One copy-to-clipboard implementation, shared by every "Copy" button in the app.
 *
 * `navigator.clipboard.writeText` can reject — plain HTTP, a denied permission, an unsupported
 * browser — and the most important caller (the API token) used to have no `catch` at all, so a
 * failed copy there threw an unhandled rejection and did nothing visible. Every caller now gets an
 * explicit `"error"` state to render, not just a silent no-op.
 *
 * `copy` also accepts a `Promise<string>` so a caller that has to fetch the text first (the
 * diagnostics bundle) can report EITHER failure — the fetch or the clipboard write — through the
 * same `"error"` state, instead of the fetch's own try/catch.
 */
export function useCopy(resetAfterMs = 2000): {
  state: CopyState;
  copy: (value: string | Promise<string>) => void;
} {
  const [state, setState] = useState<CopyState>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const copy = (value: string | Promise<string>) => {
    void Promise.resolve(value)
      .then((text) => navigator.clipboard.writeText(text))
      .then(
        () => setState("copied"),
        () => setState("error"),
      );
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), resetAfterMs);
  };

  return { state, copy };
}
