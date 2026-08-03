import { useMutation } from "@tanstack/react-query";
import { Eraser, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, apiErrorMessage } from "@/lib/api";
import { useDeleteCollection } from "@/lib/queries";
import type { Collection } from "@/lib/types";

/**
 * The two ways to un-ship a row, with their confirmations.
 *
 * They are deliberately not interchangeable and must not look it: "Remove from Plex" takes the
 * collections off the server but keeps the row here, so the next run rebuilds it; "Delete" destroys
 * the row itself. Both reach into someone else's Plex server, so both confirm first, and the
 * removal previews what it WOULD take away (a dry run) before it takes anything.
 *
 * Shared by the rows list and the row editor so the wording, the dry run and the confirmations
 * cannot drift apart between the two places you can trigger them from.
 */
export function RowDestructiveActions({
  collection,
  onDeleted,
  size = "sm",
}: {
  collection: Collection;
  /** Where to go once the row no longer exists. The list stays put; the editor has to leave. */
  onDeleted?: () => void;
  size?: "sm" | "default";
}) {
  const remove = useDeleteCollection();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [cleanupOpen, setCleanupOpen] = useState(false);

  // A dry-run first (what WOULD be removed), then the real removal on confirm.
  const preview = useMutation({
    mutationFn: () => api.cleanupCollection(collection.id, true),
  });
  const cleanup = useMutation({
    mutationFn: () => api.cleanupCollection(collection.id, false),
  });

  return (
    <>
      <Button
        variant="ghost"
        size={size}
        onClick={() => {
          cleanup.reset();
          preview.reset();
          setCleanupOpen(true);
          preview.mutate();
        }}
        aria-label={`Remove ${collection.name} from Plex`}
        title="Take the row off Plex now, but keep it here to rebuild next run"
      >
        <Eraser aria-hidden="true" />
        Remove from Plex
      </Button>
      {/* The default row is deletable too. Hiding this on one card left the first row in the
          list without the button every other row had, and nothing on screen said why. */}
      <Button
        variant="ghost"
        size={size}
        loading={remove.isPending}
        onClick={() => setConfirmOpen(true)}
        aria-label={`Delete ${collection.name}`}
        title="Delete this row for good"
        className="text-destructive-text hover:text-destructive-text"
      >
        {!remove.isPending && <Trash2 aria-hidden="true" />}
        Delete
      </Button>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete “{collection.name}”?</DialogTitle>
            <DialogDescription>
              This removes the row and its Plex collections now, for everyone
              who has it. The titles themselves stay in your library. This can’t
              be undone.
            </DialogDescription>
          </DialogHeader>
          {/* Inside the dialog, not beside the button that opened it. A failed delete leaves this
              dialog OPEN, and everything behind an open dialog is aria-hidden — so an alert rendered
              out there is invisible to a screen reader and buried under the overlay for everyone
              else. The failure would look like a button that simply did nothing. */}
          {remove.isError && (
            <p role="alert" className="text-sm text-destructive-text">
              {apiErrorMessage(
                remove.error,
                "Couldn’t delete this row. Try again.",
              )}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              loading={remove.isPending}
              onClick={() =>
                remove.mutate(collection.id, {
                  onSuccess: () => {
                    setConfirmOpen(false);
                    onDeleted?.();
                  },
                })
              }
            >
              Delete row
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={cleanupOpen}
        onOpenChange={(open) => {
          setCleanupOpen(open);
          if (!open) {
            preview.reset();
            cleanup.reset();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove “{collection.name}” from Plex?</DialogTitle>
            <DialogDescription>
              Deletes this row’s collections from Plex for everyone who has it.
              The titles stay in your library and the row’s settings here are
              kept — it’ll be rebuilt on the next run unless you also turn it
              off or delete it.
            </DialogDescription>
          </DialogHeader>
          {preview.isPending && (
            <p className="text-sm text-muted-foreground">Checking Plex…</p>
          )}
          {preview.isSuccess && !cleanup.isSuccess && (
            <p className="text-sm">
              {preview.data.removed.length === 0
                ? "Nothing to remove — this row has no collections on Plex right now."
                : `This will remove ${preview.data.removed.length} collection${
                    preview.data.removed.length === 1 ? "" : "s"
                  } from Plex.`}
            </p>
          )}
          {cleanup.isSuccess && (
            <p role="status" className="text-sm text-success">
              {cleanup.data.message}
            </p>
          )}
          {(preview.isError || cleanup.isError) && (
            <p role="alert" className="text-sm text-destructive-text">
              {apiErrorMessage(
                preview.error ?? cleanup.error,
                "Couldn’t reach Plex. Try again.",
              )}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setCleanupOpen(false)}>
              {cleanup.isSuccess ? "Close" : "Cancel"}
            </Button>
            {!cleanup.isSuccess && (
              <Button
                variant="destructive"
                loading={cleanup.isPending}
                disabled={
                  preview.isPending ||
                  // A dry run that FAILED tells us nothing about what is on the server. Leaving the
                  // button live here would fire the real removal with nobody having seen the diff,
                  // which is the one thing plex-safety rule 8 exists to prevent.
                  preview.isError ||
                  (preview.isSuccess && preview.data.removed.length === 0)
                }
                onClick={() => cleanup.mutate()}
              >
                Remove from Plex
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
