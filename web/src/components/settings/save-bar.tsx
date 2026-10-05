import { type ReactNode, useCallback, useContext, useMemo, useRef, useState } from "react";

import { MutationAlert } from "@/components/mutation-alert";
import { SavedIndicator } from "@/components/saved-indicator";
import { type Report, SaveBarContext, type SaveState, SaveStatesContext } from "@/components/settings/save-bar-context";

/**
 * Collects the save state of every auto-saving section inside it, so a tab can show ONE readout
 * instead of one per section. Saving itself is unchanged: each section still writes its own keys
 * a moment after a change, exactly as before.
 */
export function SaveBarProvider({ children }: { children: ReactNode }) {
  const [states, setStates] = useState<Record<string, SaveState>>({});
  const retries = useRef<Record<string, () => void>>({});
  const report = useCallback<Report>((id, state, retry) => {
    if (retry) retries.current[id] = retry;
    setStates((current) => {
      if (state === null) {
        const { [id]: _gone, ...rest } = current;
        return rest;
      }
      const previous = current[id];
      if (
        previous &&
        previous.isPending === state.isPending &&
        previous.isError === state.isError &&
        previous.error === state.error &&
        previous.saved === state.saved
      )
        return current;
      return { ...current, [id]: state };
    });
  }, []);
  const statesValue = useMemo(() => ({ states, retry: (id: string) => retries.current[id]?.() }), [states]);
  return (
    <SaveBarContext.Provider value={report}>
      <SaveStatesContext.Provider value={statesValue}>{children}</SaveStatesContext.Provider>
    </SaveBarContext.Provider>
  );
}

/** The one sticky readout at the foot of a tab: what is saving, what saved, and any save that failed. */
export function SaveBar() {
  const context = useContext(SaveStatesContext);
  const entries = Object.entries(context?.states ?? {});
  const failed = entries.find(([, state]) => state.isError && !state.isPending);
  const pending = entries.some(([, state]) => state.isPending);
  const saved = !pending && entries.some(([, state]) => state.saved);

  return (
    <div className="sticky bottom-0 z-20 -mx-1 mt-2 px-1 pb-3 pt-2">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-lg border bg-card/95 px-4 py-3 text-sm shadow-lg backdrop-blur-sm">
        {failed ? (
          <MutationAlert
            error={failed[1].error}
            fallback="Saving failed. Check the server log and try again."
            lead="Not saved."
            onRetry={() => context?.retry(failed[0])}
            className="w-full"
          />
        ) : (
          <>
            <div aria-live="polite" className="flex items-center gap-3">
              {pending ? <span>Saving…</span> : saved ? <SavedIndicator show as="span" /> : <span>Every change here saves on its own.</span>}
            </div>
            <p className="text-xs text-muted-foreground">Switches save as you flip them; typed values save a moment after you stop.</p>
          </>
        )}
      </div>
    </div>
  );
}
