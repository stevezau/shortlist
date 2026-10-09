import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Check, Loader2 } from "lucide-react";
import { useId, useState } from "react";
import { Link } from "react-router";

import { PageHeader } from "@/components/page-header";
import { MutationAlert } from "@/components/mutation-alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiErrorMessage } from "@/lib/api";
import { groupCollections, groupTitles } from "@/lib/group-titles";
import { useSSE } from "@/lib/sse";
import type { UninstallResult } from "@/lib/types";

const CONFIRM_PHRASE = "uninstall shortlist";

/** A monospace, auto-scrolling log — the same shape as the run activity feed. */
function LogBox({ lines }: { lines: string[] }) {
  return (
    <div
      role="log"
      aria-live="polite"
      className="max-h-64 space-y-1 overflow-y-auto rounded-md bg-muted/40 p-3 font-mono text-xs"
    >
      {lines.map((line, i) => (
        <p key={i} className="flex items-start gap-2">
          <Check
            className="mt-0.5 h-3 w-3 shrink-0 text-success"
            aria-hidden="true"
          />
          <span>{line}</span>
        </p>
      ))}
    </div>
  );
}

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

/**
 * What the uninstall will do, as counts. Labels have no count of their own: the only labels
 * Shortlist adds are on its collections, and they go with them.
 */
function PlanSummary({ result }: { result: UninstallResult }) {
  const filters =
    result.filters_restored === 1
      ? "1 share filter from its snapshot"
      : `${result.filters_restored} share filters from their snapshots`;
  return (
    <p className="font-medium">
      Restores {filters}, deletes {plural(result.collections_deleted.length, "collection", "collections")} and
      switches off {plural(result.rows_disabled, "row", "rows")}.
    </p>
  );
}

/** The collections the preview will delete, by library and person. A server that does not send the
 *  per-collection detail still gets its titles. */
function CollectionsToDelete({ result }: { result: UninstallResult }) {
  const detail = result.collections_detail ?? [];
  if (detail.length === 0) {
    return result.collections_deleted.length > 0 ? (
      <p className="text-muted-foreground">{groupTitles(result.collections_deleted).join(", ")}</p>
    ) : null;
  }
  return (
    <div className="space-y-3 py-2">
      {groupCollections(detail).map(({ library, people }) => (
        <section key={library} className="space-y-1">
          <h3 className="text-sm font-medium">{library}</h3>
          <ul className="space-y-0.5 text-muted-foreground">
            {people.map(({ person, titles }) => (
              <li key={person} className="flex flex-wrap gap-x-2">
                <span className="text-foreground">{person}</span>
                <span>{titles.join(", ")}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

/**
 * The accounts uninstall could NOT put back, named. The two reasons need different things from the
 * owner — a failure is worth retrying, a departed account never will be — so they are never merged
 * into one count. Silence here is how issue #96 would have looked if it had been fixed by simply
 * swallowing the error.
 */
function AccountsNotRestored({
  result,
  preview = false,
}: {
  result: UninstallResult;
  preview?: boolean;
}) {
  const { filters_failed: failed, filters_unreachable: unreachable } = result;
  const skipped = result.filters_skipped;
  if (!failed.length && !unreachable.length && !skipped.length) return null;
  const names = (rows: { user: string }[]) =>
    rows.map((r) => r.user).join(", ");
  return (
    <div className="space-y-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
      {failed.length > 0 && (
        <p>
          <span className="font-medium">Could not be restored:</span>{" "}
          {names(failed)}.{" "}
          {failed.length === 1
            ? "Their share filter still carries"
            : "Their share filters still carry"}{" "}
          the entries Shortlist added. Run the uninstall again to retry.
        </p>
      )}
      {unreachable.length > 0 && (
        <p>
          <span className="font-medium">plex.tv didn&rsquo;t list:</span>{" "}
          {names(unreachable)}.{" "}
          {preview
            ? "Your records say they are still on this server, so this looks like plex.tv giving an incomplete answer. Worth waiting a few minutes before you uninstall."
            : "Your records say they are still on this server, so their share filters still carry the entries Shortlist added. Run the uninstall again to retry."}
        </p>
      )}
      {skipped.length > 0 && (
        <p>
          <span className="font-medium">No longer on this server:</span>{" "}
          {names(skipped)}.{" "}
          {skipped.length === 1
            ? "Their Plex account has"
            : "Their Plex accounts have"}{" "}
          left the share, so Shortlist can no longer reach their settings.
          Nothing to fix — this is expected.
        </p>
      )}
    </div>
  );
}

/**
 * The full Uninstall flow on its own page (not a modal), with a live per-step log streamed over SSE
 * so the owner sees exactly what's happening — restoring each user's filter, deleting each
 * collection, switching rows off — while it runs, then a completion summary.
 */
export function UninstallPage() {
  const [typed, setTyped] = useState("");
  const [log, setLog] = useState<string[]>([]);
  const inputId = useId();
  const confirmed = typed.trim().toLowerCase() === CONFIRM_PHRASE;

  // The dry run IS the page: the counts are what the confirm acts on, so it loads on arrival and
  // the confirm stays locked until it has. Not cached between visits — the counts must be current.
  const preview = useQuery({
    queryKey: ["uninstall", "preview"],
    queryFn: () => api.uninstall(true),
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  const uninstall = useMutation({ mutationFn: () => api.uninstall(false) });

  // The live log: each `uninstall.progress` event streamed from the server is one line.
  useSSE({
    onUninstallProgress: (event) => setLog((prev) => [...prev, event.label]),
  });

  const running = uninstall.isPending;
  const done = uninstall.isSuccess ? uninstall.data : null;

  return (
    <div className="max-w-2xl space-y-6">
      <PageHeader
        title={
          <>
            <Link to="/settings" className="font-normal text-muted-foreground hover:text-foreground">
              Settings
            </Link>
            <span className="font-normal text-faint-foreground">{" / "}</span>
            Uninstall Shortlist
          </>
        }
        subtitle="Remove Shortlist from this server and put Plex back exactly as it was."
      />

      {done ? (
        <Card>
          <CardContent className="space-y-3 pt-6">
            {done.filters_failed.length > 0 ||
            done.filters_unreachable.length > 0 ? (
              <p className="flex items-start gap-2 text-lg font-medium text-warning">
                <AlertTriangle className="mt-1 shrink-0" aria-hidden="true" />{" "}
                Uninstall finished, with
                some accounts left to retry
              </p>
            ) : (
              <p className="flex items-center gap-2 text-lg font-medium text-success">
                <Check aria-hidden="true" /> Uninstall complete
              </p>
            )}
            <p className="text-sm text-muted-foreground">
              {done.filters_restored} share filter
              {done.filters_restored === 1 ? "" : "s"} restored ·{" "}
              {done.collections_deleted.length} collection
              {done.collections_deleted.length === 1 ? "" : "s"} deleted ·{" "}
              {done.rows_disabled} row{done.rows_disabled === 1 ? "" : "s"}{" "}
              switched off. {done.message} Nothing will rebuild — set Shortlist
              up again any time to start fresh.
            </p>
            <AccountsNotRestored result={done} />
            {log.length > 0 && <LogBox lines={log} />}
            <div className="flex flex-wrap gap-2 pt-1">
              <Button asChild>
                <Link to="/">Go to dashboard</Link>
              </Button>
              <Button asChild variant="outline">
                <Link to="/settings">Back to settings</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : (
        <Card className="border-destructive/40">
          <CardContent className="space-y-4 pt-6">
            <p className="text-sm text-muted-foreground">
              This deletes every Shortlist collection, removes the labels
              Shortlist added, restores each user&rsquo;s share filters from the
              original pre-Shortlist snapshots, and switches off every row so
              nothing rebuilds. Your Plex server ends up as Shortlist found it.
              This cannot be undone.
            </p>

            {preview.isPending && (
              <div role="status" className="space-y-2 border-y py-3 text-sm">
                <p className="text-muted-foreground">Working out what uninstall will change…</p>
                <Skeleton className="h-4 w-3/4" />
              </div>
            )}
            {preview.isError && (
              <MutationAlert
                error={preview.error}
                lead="Uninstall stays locked until this preview loads."
                fallback="Couldn’t work out what uninstall would change. Try again."
                onRetry={() => void preview.refetch()}
                retryDisabled={preview.isFetching}
              />
            )}
            {preview.data && (
              <div className="space-y-1 border-y py-3 text-sm">
                <PlanSummary result={preview.data} />
                <CollectionsToDelete result={preview.data} />
                <p className="text-muted-foreground">{preview.data.message}</p>
                <div className="pt-1">
                  <AccountsNotRestored result={preview.data} preview />
                </div>
              </div>
            )}

            {running && (
              <div
                role="status"
                aria-live="polite"
                className="space-y-2 rounded-md border border-primary/30 bg-primary/5 p-3 text-sm"
              >
                <p className="flex items-center gap-2 font-medium">
                  <Loader2
                    className="h-4 w-4 shrink-0 animate-spin"
                    aria-hidden="true"
                  />
                  Uninstalling — restoring each user&rsquo;s share filters goes
                  through plex.tv, as fast as plex.tv accepts; Shortlist only
                  slows down if plex.tv rate-limits it. Every step streams
                  below.
                </p>
                <LogBox lines={log.length ? log : ["Starting…"]} />
              </div>
            )}

            {!running && (
              <div className="space-y-2">
                <Label htmlFor={inputId}>
                  Type{" "}
                  <span className="font-mono text-primary">
                    {CONFIRM_PHRASE}
                  </span>{" "}
                  to confirm
                </Label>
                <Input
                  id={inputId}
                  value={typed}
                  onChange={(event) => setTyped(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  className="max-w-sm"
                />
              </div>
            )}

            {uninstall.isError && (
              <p role="alert" className="text-sm text-destructive-text">
                {apiErrorMessage(
                  uninstall.error,
                  "Uninstall failed. Some accounts may not have been restored. See the server log, then run the uninstall again to retry.",
                )}
              </p>
            )}

            <div className="flex flex-wrap gap-2 pt-1">
              <Button asChild variant="outline" disabled={running}>
                <Link to="/settings">Keep Shortlist</Link>
              </Button>
              <Button
                variant="destructive"
                disabled={!confirmed || !preview.isSuccess || running}
                onClick={() => {
                  setLog([]);
                  uninstall.mutate();
                }}
              >
                {running && (
                  <Loader2 className="animate-spin" aria-hidden="true" />
                )}
                Uninstall and restore server
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
