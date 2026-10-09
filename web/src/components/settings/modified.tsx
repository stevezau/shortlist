import { createContext, useContext, useEffect } from "react";

import { useSettingDefaults } from "@/lib/queries";

/** A setting that differs from its built-in default: what the default is, and how to go back to it. */
export type Modified = { defaultLabel: string; reset: () => void };

/** How a section tells the tab's jump list how many of its settings differ from their defaults. */
export const ModifiedCountContext = createContext<((id: string, count: number) => void) | null>(null);

/**
 * Report a section's modified-setting count to the jump list beside it, and clear it on unmount.
 * Rendered without a jump list (a unit test, another page) it does nothing.
 */
export function useReportModifiedCount(id: string, count: number): void {
  const report = useContext(ModifiedCountContext);
  useEffect(() => {
    report?.(id, count);
  }, [report, id, count]);
  useEffect(() => () => report?.(id, 0), [report, id]);
}

/**
 * Builds the "Modified" marker for each setting a section shows.
 *
 * `current` is the value as the section holds it (in its own units); `fromDefault` turns the server's
 * default into those same units, and `reset` puts it back through the section's own state, so the
 * normal auto-save persists it. Until the defaults load nothing is marked: an unknown default is not
 * a modification.
 */
export function useModifiedMarks() {
  const defaults = useSettingDefaults().data;
  return function mark<T>(
    key: string,
    current: T,
    options: { fromDefault?: (raw: unknown) => T; label: (value: T) => string; reset: (value: T) => void },
  ): Modified | undefined {
    if (!defaults || !(key in defaults)) return undefined;
    const fallback = options.fromDefault ? options.fromDefault(defaults[key]) : (defaults[key] as T);
    if (JSON.stringify(current) === JSON.stringify(fallback)) return undefined;
    return { defaultLabel: options.label(fallback), reset: () => options.reset(fallback) };
  };
}

/** The count of settings in a list that are modified. */
export function countModified(marks: (Modified | undefined)[]): number {
  return marks.filter(Boolean).length;
}

/** A dot and "Modified", beside a setting's name. */
export function ModifiedBadge({ modified }: { modified?: Modified }) {
  if (!modified) return null;
  return (
    <span className="ml-2 inline-flex items-center gap-1.5 align-middle text-xs font-normal text-muted-foreground">
      <span aria-hidden="true" className="size-1.5 rounded-full bg-foreground" />
      Modified
    </span>
  );
}

/** "default: 12 titles · Reset to default", under a modified setting. */
export function ModifiedDefault({ modified, name }: { modified?: Modified; name: string }) {
  if (!modified) return null;
  return (
    <p className="text-xs text-muted-foreground">
      default: {modified.defaultLabel} ·{" "}
      <button
        type="button"
        onClick={modified.reset}
        aria-label={`Reset ${name} to default`}
        className="rounded-sm underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Reset to default
      </button>
    </p>
  );
}
