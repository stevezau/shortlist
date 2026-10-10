import { TextCursorInput, Zap } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { builtAt } from "@/components/rows/row-facts";
import { RowEnableToggle } from "@/components/rows/row-enable-toggle";
import { RowRunAction } from "@/components/rows/row-run-action";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { Collection, RowEffectiveness } from "@/lib/types";

const MEDIA_PLURAL: Record<string, string> = { movie: "movies", show: "shows" };

/** "40 movies · 40 shows", or "40 titles" when the copy is one kind; null when nothing is on record. */
function sharedTitlesLabel(counts: Record<string, number> | null | undefined): string | null {
  const entries = Object.entries(counts ?? {}).filter(([, count]) => count > 0);
  if (entries.length === 0) return null;
  if (entries.length === 1) return `${entries[0]?.[1].toLocaleString()} titles`;
  return entries
    .sort(([a], [b]) => Object.keys(MEDIA_PLURAL).indexOf(a) - Object.keys(MEDIA_PLURAL).indexOf(b))
    .map(([type, count]) => `${count.toLocaleString()} ${MEDIA_PLURAL[type] ?? "titles"}`)
    .join(" · ");
}

/** The row's record in one line: when it last built, what it delivered, and whether anyone watched. */
function HistoryLine({
  data,
  state,
  shared,
  onRetry,
}: {
  data: RowEffectiveness | undefined;
  /** A shared row is one collection for everyone, so it has no per-person delivery count. */
  shared: boolean;
  state: "loading" | "error" | "ready";
  onRetry: () => void;
}) {
  if (state === "loading") return <span>Loading this row&rsquo;s history…</span>;
  if (state === "error" || !data) {
    return (
      <span>
        Couldn&rsquo;t load this row&rsquo;s history.{" "}
        <button
          type="button"
          onClick={onRetry}
          className="rounded-sm underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Try again
        </button>
      </span>
    );
  }
  if (data.first_delivered_at === null || data.last_delivered_at === null) {
    return <span>Not built yet. Once it runs, this says what it delivered and whether people watch it.</span>;
  }
  const facts: ReactNode[] = [
    <>
      Last built <b className="font-semibold text-foreground">{builtAt(data.last_delivered_at)}</b>
    </>,
    // A shared row writes no per-person picks, so `delivered` is 0 for it by construction; printing
    // "0 titles delivered" read as "this row put nothing on Plex".
    shared ? (
      <>
        {sharedTitlesLabel(data.shared_titles)
          ? `${sharedTitlesLabel(data.shared_titles)} — one shared copy for everyone`
          : "One shared copy for everyone"}
      </>
    ) : (
      <>
        <b className="font-semibold text-foreground tabular-nums">{data.delivered.toLocaleString()}</b> titles
        delivered
      </>
    ),
  ];
  // The rate is only shown for picks old enough to judge: a row delivered last night lands 0% for no
  // reason but time, and reading that as failure sends someone to change settings that were fine.
  facts.push(
    data.matured === null ? (
      <>
        <b className="font-semibold text-foreground tabular-nums">{data.watched}</b> watched so far, a pick is judged
        once it&rsquo;s had {data.matured_days} days
      </>
    ) : (
      <>
        <b className="font-semibold text-foreground tabular-nums">
          {data.matured.rate === null ? "—" : `${Math.round(data.matured.rate * 100)}%`}
        </b>{" "}
        of judged picks watched
      </>
    ),
  );
  return (
    <span>
      {facts.map((fact, index) => (
        <span key={index}>
          {index > 0 && " · "}
          {fact}
        </span>
      ))}
    </span>
  );
}

/**
 * Everything on the row editor that changes Plex the moment it is pressed, fenced off from the draft
 * below it that only Save sends: the on/off switch, Rename on Plex, and Run now.
 *
 * The artwork is NOT here, although the mockup puts it here. Its source and text are part of the
 * draft and save with Save changes today; only the image upload inside it writes at once. Moving the
 * whole field under "apply immediately" would mislabel the half of it that waits for Save.
 */
export function RowLiveStrip({
  collection,
  enableDisabled,
  onEnableSaving,
  onEnableSaved,
  renameDisabled,
  onRename,
  unsaved,
  history,
  historyState,
  onRetryHistory,
}: {
  /** The row as saved, with the switch's own change folded in. */
  collection: Collection;
  enableDisabled: boolean;
  onEnableSaving: (saving: boolean) => void;
  onEnableSaved: (enabled: boolean) => void;
  renameDisabled: boolean;
  onRename: () => void;
  /** The form below differs from the saved row, which is what Run now builds. */
  unsaved: boolean;
  history: RowEffectiveness | undefined;
  historyState: "loading" | "error" | "ready";
  onRetryHistory: () => void;
}) {
  return (
    <section aria-labelledby="row-live-heading" className="rounded-xl border bg-card shadow-elevated">
      <div className="flex flex-col gap-3 p-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 space-y-0.5">
          <div className="flex items-center gap-3">
            <h2 id="row-live-heading" className="flex items-center gap-2 text-base font-semibold">
              <Zap aria-hidden="true" className="size-4 text-primary" />
              Live on Plex
            </h2>
            <Badge variant="success">Applies immediately</Badge>
          </div>
          <p className="text-sm text-muted-foreground">Changes here apply to Plex immediately.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div data-setting="enabled" className="contents">
            <RowEnableToggle
              collection={collection}
              showLabel="long"
              disabled={enableDisabled}
              onSaving={onEnableSaving}
              onSaved={onEnableSaved}
            />
          </div>
          <Button variant="outline" size="sm" disabled={renameDisabled} onClick={onRename}>
            <TextCursorInput aria-hidden="true" />
            Rename on Plex…
          </Button>
          <RowRunAction collection={collection} variant="outline" />
        </div>
      </div>
      <div className="space-y-2 border-t px-4 py-3 text-sm text-muted-foreground">
        {!collection.enabled && (
          // Saving `enabled: false` removes its collections there and then (`row_changes.py`, RECONCILE
          // collection.disable), and runs skip a row that is off.
          <p>
            Off — switching it off takes it off Plex straight away, and nothing is built until you turn it back on.
          </p>
        )}
        <p>
          <HistoryLine
            data={history}
            state={historyState}
            shared={collection.build === "shared"}
            onRetry={onRetryHistory}
          />
          {" · "}
          <Link
            to={`/runs?row=${encodeURIComponent(collection.slug)}`}
            className="rounded-sm underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            See runs →
          </Link>
        </p>
        {/* Run builds the row AS SAVED, which is a trap next to a form you have been editing —
            nothing about the button says the changes on screen won't be in it. */}
        {unsaved && (
          <p className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-foreground">
            You have unsaved changes. Run now builds this row as it was last saved — press{" "}
            <strong>Save changes</strong> first if you want them in it.
          </p>
        )}
      </div>
    </section>
  );
}
