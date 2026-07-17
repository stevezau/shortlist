import { Eye, Loader2 } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { UninstallResult } from "@/lib/types";

export const UNINSTALL_CONFIRM_PHRASE = "uninstall shortlist";

export interface UninstallDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Fires the real uninstall (the API maps this to confirm: "UNINSTALL"). */
  onConfirm: () => void;
  pending: boolean;
  /** Fires a dry-run preview of what would change. */
  onPreview: () => void;
  previewPending: boolean;
  preview: UninstallResult | null;
}

/**
 * Typed-confirmation dialog for the full uninstall (design doc §4), with the
 * designed dry-run preview: see exactly what would change before committing.
 * The destructive button stays disabled until the exact phrase is typed.
 */
export function UninstallDialog({
  open,
  onOpenChange,
  onConfirm,
  pending,
  onPreview,
  previewPending,
  preview,
}: UninstallDialogProps) {
  const [typed, setTyped] = useState("");
  const inputId = useId();
  const confirmed = typed.trim().toLowerCase() === UNINSTALL_CONFIRM_PHRASE;

  // A live "…s elapsed" counter while the uninstall runs — the write loop is Plex-rate-limited to
  // ~1/sec, so it can run a while, and a bare spinner reads as frozen. This makes the wait honest.
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!pending) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const id = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      1000,
    );
    return () => window.clearInterval(id);
  }, [pending]);

  const handleOpenChange = (next: boolean) => {
    if (pending) return; // never close mid-uninstall — the write loop keeps running regardless
    if (!next) setTyped("");
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Uninstall Shortlist from this server?</DialogTitle>
          <DialogDescription>
            This deletes every Shortlist collection, removes the labels
            Shortlist added to your collections, and restores each user's share
            filters from the original pre-Shortlist snapshots. Your Plex server
            ends up as Shortlist found it. This cannot be undone.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onPreview}
            disabled={previewPending || pending}
          >
            {previewPending ? (
              <Loader2 className="animate-spin" aria-hidden="true" />
            ) : (
              <Eye aria-hidden="true" />
            )}
            Preview what would change
          </Button>
          {preview && (
            <div className="rounded-md border bg-card p-3 text-sm">
              <p>
                {preview.filters_restored} share filter
                {preview.filters_restored === 1 ? "" : "s"} restored ·{" "}
                {preview.collections_deleted.length} collection
                {preview.collections_deleted.length === 1 ? "" : "s"} deleted
              </p>
              {preview.collections_deleted.length > 0 && (
                <p className="mt-1 text-muted-foreground">
                  {preview.collections_deleted.join(" · ")}
                </p>
              )}
              <p className="mt-1 text-muted-foreground">{preview.message}</p>
            </div>
          )}
        </div>

        {pending && (
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
              Restoring share filters and deleting collections…
            </p>
            <p className="text-muted-foreground">
              {preview
                ? `Putting back ${preview.filters_restored} user share filter${
                    preview.filters_restored === 1 ? "" : "s"
                  } and removing ${preview.collections_deleted.length} collection${
                    preview.collections_deleted.length === 1 ? "" : "s"
                  }. `
                : ""}
              Plex rate-limits these writes to about one per second, so this can
              take a minute or two. Keep this window open — don&rsquo;t refresh
              or close it.
            </p>
            <p className="text-xs tabular-nums text-muted-foreground">
              {elapsed}s elapsed
            </p>
          </div>
        )}

        <div className="space-y-2">
          <Label htmlFor={inputId}>
            Type{" "}
            <span className="font-mono text-primary">
              {UNINSTALL_CONFIRM_PHRASE}
            </span>{" "}
            to confirm
          </Label>
          <Input
            id={inputId}
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => handleOpenChange(false)}
            disabled={pending}
          >
            Keep Shortlist
          </Button>
          <Button
            variant="destructive"
            disabled={!confirmed || pending}
            onClick={onConfirm}
          >
            {pending && <Loader2 className="animate-spin" aria-hidden="true" />}
            Uninstall and restore server
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
