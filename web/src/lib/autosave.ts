import { useEffect, useRef, useState } from "react";

import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** The one debounce every auto-saving control shares, so text never persists mid-keystroke. */
export const AUTOSAVE_DELAY_MS = 600;

/**
 * Persist `value` shortly after it stops changing — the app's single save paradigm. (The only
 * exception is Settings → Connections, where a half-typed token must never auto-commit.)
 *
 * @param value - The edited state. Compared by content, so building it inline each render is fine.
 * @param save - Fires the mutation. Re-read on every render, so it always sees the latest state.
 * @returns A `retry` that fires the same save immediately — what a failed auto-save's "Try again"
 *   calls, since the value on screen hasn't changed and so can never re-arm the debounce itself.
 */
export function useAutosave(value: unknown, save: () => void): () => void {
  // Assigned in an effect rather than during render — see the note in lib/sse.ts. Safe because
  // saveRef is only read from the debounce timer and the returned retry callback, both of which
  // run after the commit.
  const saveRef = useRef(save);
  useEffect(() => {
    saveRef.current = save;
  });

  // Keyed on content, not identity: callers pass a fresh object each render, which as an effect
  // dependency would re-arm the timer forever.
  const key = JSON.stringify(value);
  const firstRender = useRef(true);

  useEffect(() => {
    // Merely opening a page must write nothing.
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    const timer = setTimeout(() => saveRef.current(), AUTOSAVE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [key]);

  return () => saveRef.current();
}

/** What every auto-saving settings section needs to drive its {@link SaveStatus} readout. */
export interface AutosavedSettings {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  /** True once a save has succeeded and nothing has changed since. */
  saved: boolean;
  /** Re-fires the same save immediately — for a failed auto-save's "Try again". */
  retry: () => void;
}

/**
 * The settings-section auto-save paradigm in one hook: a {@link useSaveSettings} mutation, the
 * `saved` flag that flips true on success and back to false the moment a new save starts, and the
 * {@link useAutosave} wiring — so a section only supplies its state and how to map it to a payload.
 *
 * @param value - The edited state to watch. Compared by content, so building it inline is fine.
 * @param toValues - Maps the current state to the settings payload to PUT. Return `null` to skip
 *   this save entirely (e.g. an invalid cron) — the `saved` flag is left untouched, not reset.
 * @returns The pending/error/saved flags and a `retry`, ready to spread into {@link SaveStatus}.
 */
export function useAutosavedSettings<T>(
  value: T,
  toValues: () => Settings | null,
): AutosavedSettings {
  const save = useSaveSettings();
  const [saved, setSaved] = useState(false);

  const retry = useAutosave(value, () => {
    const values = toValues();
    if (values === null) return;
    setSaved(false);
    save.mutate(values, { onSuccess: () => setSaved(true) });
  });

  return {
    isPending: save.isPending,
    isError: save.isError,
    error: save.error,
    saved,
    retry,
  };
}
