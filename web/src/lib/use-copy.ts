import { useRef, useState } from "react";

type CopyState = "idle" | "copied" | "error";

/**
 * One copy-to-clipboard implementation, shared by every "Copy" button in the app.
 *
 * `navigator.clipboard.writeText` can reject — plain HTTP, a denied permission, an unsupported
 * browser — and a failed copy must not throw an unhandled rejection and do nothing visible, so every
 * caller gets an explicit `"error"` state to render.
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
