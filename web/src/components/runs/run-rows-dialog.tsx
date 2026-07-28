import { useState } from "react";

import { Badge } from "@/components/ui/badge";
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
import { useCollections } from "@/lib/queries";

/**
 * "Run selected rows" — since rows carry their own schedules, a manual run can target specific rows
 * rather than every one. Only enabled rows are runnable; all start selected. Emits the chosen row ids
 * (the engine's `collection_ids` scope — privacy is unaffected, only the delivery loop narrows).
 */
export function RunRowsDialog({
  onRun,
  isPending,
}: {
  onRun: (collectionIds: number[]) => void;
  isPending: boolean;
}) {
  const [open, setOpen] = useState(false);
  const collections = useCollections();
  const rows = (collections.data ?? []).filter((row) => row.enabled);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  // Reset to "all rows" each time the dialog opens, so it never opens with a stale partial
  // selection. Done in the open handler rather than an effect: opening is an event, so the reset
  // belongs on the event. The effect version also had to depend on collections.data (and silence
  // exhaustive-deps), which meant a background refetch could wipe an in-progress selection.
  const openDialog = () => {
    setSelected(new Set(rows.map((row) => row.id)));
    setOpen(true);
  };

  const toggle = (id: number, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  return (
    <>
      <Button variant="outline" onClick={openDialog}>
        Run selected rows…
      </Button>
      <Dialog
        open={open}
        onOpenChange={(next) => (next ? openDialog() : setOpen(false))}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Run selected rows</DialogTitle>
            <DialogDescription>
              Rebuild just these rows now. Every row still stays private — only
              the chosen rows are refreshed.
            </DialogDescription>
          </DialogHeader>

          {rows.length === 0 ? (
            <p className="py-4 text-sm text-muted-foreground">
              No enabled rows to run. Enable a row on the Rows page first.
            </p>
          ) : (
            <div className="max-h-72 space-y-1 overflow-y-auto rounded-lg border bg-elevated p-2">
              {rows.map((row) => (
                <label
                  key={row.id}
                  className="flex cursor-pointer items-center justify-between gap-3 rounded-md px-2 py-1.5 hover:bg-accent"
                >
                  <span className="flex min-w-0 items-center gap-2 text-sm">
                    <span className="truncate">{row.name}</span>
                    <Badge
                      variant={row.build === "shared" ? "warning" : "secondary"}
                    >
                      {row.build === "shared" ? "Shared" : "Per person"}
                    </Badge>
                  </span>
                  <Switch
                    checked={selected.has(row.id)}
                    onCheckedChange={(on) => toggle(row.id, on)}
                    aria-label={`Include ${row.name}`}
                  />
                </label>
              ))}
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={selected.size === 0}
              loading={isPending}
              onClick={() => {
                onRun([...selected]);
                setOpen(false);
              }}
            >
              Run {selected.size} {selected.size === 1 ? "row" : "rows"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
