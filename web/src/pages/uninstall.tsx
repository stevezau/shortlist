import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, Check, Eye, Loader2 } from "lucide-react";
import { useId, useState } from "react";
import { Link } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api";
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

function ChangeSummary({ result }: { result: UninstallResult }) {
  return (
    <p>
      {result.filters_restored} share filter
      {result.filters_restored === 1 ? "" : "s"} ·{" "}
      {result.collections_deleted.length} collection
      {result.collections_deleted.length === 1 ? "" : "s"} ·{" "}
      {result.rows_disabled} row{result.rows_disabled === 1 ? "" : "s"}
    </p>
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

  const preview = useMutation({ mutationFn: () => api.uninstall(true) });
  const uninstall = useMutation({ mutationFn: () => api.uninstall(false) });

  // The live log: each `uninstall.progress` event streamed from the server is one line.
  useSSE({
    onUninstallProgress: (event) => setLog((prev) => [...prev, event.label]),
  });

  const running = uninstall.isPending;
  const done = uninstall.isSuccess ? uninstall.data : null;

  return (
    <div className="max-w-2xl space-y-6">
      <BackLink to="/settings" label="Settings" />
      <PageHeader
        icon={AlertTriangle}
        title="Uninstall Shortlist"
        subtitle="Remove Shortlist from this server and put Plex back exactly as it was."
      />

      {done ? (
        <Card>
          <CardContent className="space-y-3 pt-6">
            <p className="flex items-center gap-2 text-lg font-medium text-success">
              <Check aria-hidden="true" /> Uninstall complete
            </p>
            <p className="text-sm text-muted-foreground">
              {done.filters_restored} share filter
              {done.filters_restored === 1 ? "" : "s"} restored ·{" "}
              {done.collections_deleted.length} collection
              {done.collections_deleted.length === 1 ? "" : "s"} deleted ·{" "}
              {done.rows_disabled} row{done.rows_disabled === 1 ? "" : "s"}{" "}
              switched off. Your Plex server is as Shortlist found it, and
              nothing will rebuild. Set Shortlist up again any time to start
              fresh.
            </p>
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

            <div className="space-y-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => preview.mutate()}
                disabled={preview.isPending || running}
              >
                {preview.isPending ? (
                  <Loader2 className="animate-spin" aria-hidden="true" />
                ) : (
                  <Eye aria-hidden="true" />
                )}
                Preview what would change
              </Button>
              {preview.data && (
                <div className="rounded-md border bg-card p-3 text-sm">
                  <ChangeSummary result={preview.data} />
                  {preview.data.collections_deleted.length > 0 && (
                    <p className="mt-1 text-muted-foreground">
                      {preview.data.collections_deleted.join(" · ")}
                    </p>
                  )}
                  <p className="mt-1 text-muted-foreground">
                    {preview.data.message}
                  </p>
                </div>
              )}
            </div>

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
                  Uninstalling — Plex allows about one write per second, so this
                  can take a minute or two. Stay on this page.
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
              <p role="alert" className="text-sm text-destructive">
                {apiErrorMessage(
                  uninstall.error,
                  "Uninstall failed — nothing was left half-done. See the server log, then try again.",
                )}
              </p>
            )}

            <div className="flex flex-wrap gap-2 pt-1">
              <Button asChild variant="outline" disabled={running}>
                <Link to="/settings">Keep Shortlist</Link>
              </Button>
              <Button
                variant="destructive"
                disabled={!confirmed || running}
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
