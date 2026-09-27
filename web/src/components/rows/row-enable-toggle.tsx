import { useState } from "react";

import { MutationAlert } from "@/components/mutation-alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { toInput } from "@/lib/collections";
import { useSaveCollection } from "@/lib/queries";
import type { Collection } from "@/lib/types";

/**
 * A row's on/off switch, with the confirmation turning it off deserves.
 *
 * Shared by the Rows card and the row editor. It was the card's alone, and the editor's own copy
 * admitted the gap — it told you to "use the toggle on the Rows page", sending you to another screen
 * to do the one reversible thing to the row you had open. Removing and deleting stay fenced off at
 * the bottom of the editor, because those reach into other people's Plex and Cancel does not undo
 * them; this is neither, so it belongs with Run now.
 */
export function RowEnableToggle({
  collection,
  showLabel = false,
  disabled = false,
  onSaving,
  onSaved,
}: {
  collection: Collection;
  /** The editor has room for a word beside the switch; the card's action strip does not. */
  showLabel?: boolean;
  /** Holds the switch and its Try again, e.g. while a form holding the row saves it. */
  disabled?: boolean;
  /** Called with true when a change starts saving and false once it's done, either way. */
  onSaving?: (saving: boolean) => void;
  /** Called once a change is saved, so a form holding the row can take the new value. */
  onSaved?: (enabled: boolean) => void;
}) {
  const save = useSaveCollection();
  const [confirmDisable, setConfirmDisable] = useState(false);
  // The body is the whole row, so it is built from the row as it is when sent — a retry too. Replaying
  // the request that failed would carry back whatever the row held then, undoing a save made since.
  const setEnabled = (enabled: boolean) => {
    onSaving?.(true);
    save.mutate(
      { id: collection.id, body: { ...toInput(collection), enabled } },
      {
        onSuccess: () => onSaved?.(enabled),
        onSettled: () => onSaving?.(false),
      },
    );
  };

  return (
    <>
      <span className="flex items-center gap-2">
        <Switch
          checked={collection.enabled}
          disabled={disabled}
          onCheckedChange={(enabled) =>
            enabled ? setEnabled(true) : setConfirmDisable(true)
          }
          aria-label={`Enable ${collection.name}`}
        />
        {showLabel && (
          <span className="text-sm text-muted-foreground">
            {collection.enabled ? "On" : "Off"}
          </span>
        )}
      </span>

      {/* The Switch mirrors the SAVED row, so a rejected save just snaps it back — and silently
          reverting is exactly what a click that never landed looks like. */}
      {save.isError && (
        <MutationAlert
          className="w-full"
          error={save.error}
          lead={
            collection.enabled
              ? "This row is still on."
              : "This row is still off."
          }
          fallback="Couldn’t change this row. Try again."
          onRetry={() => {
            const last = save.variables;
            if (last) setEnabled(last.body.enabled);
          }}
          retryDisabled={disabled}
        />
      )}

      {/* A confirmation, because the toggle reaches past this screen: saving it removes the row's
          collections from Plex for everyone who had it, there and then (`row_changes.py`,
          RECONCILE collection.disable). */}
      <Dialog open={confirmDisable} onOpenChange={setConfirmDisable}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Turn off &ldquo;{collection.name}&rdquo;?</DialogTitle>
            <DialogDescription>
              Switching it off takes it off Plex straight away, for everyone
              who has it. Its settings stay here, and nothing is built until you
              turn it back on. The titles themselves stay in your library.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDisable(false)}>
              Keep it on
            </Button>
            <Button
              onClick={() => {
                setEnabled(false);
                setConfirmDisable(false);
              }}
            >
              Turn it off
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
