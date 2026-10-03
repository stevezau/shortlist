import { TemplateVarsHint } from "@/components/rows/template-vars-hint";
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

/**
 * Where a saved row's new name is typed. Applying it rewrites the collection for every person who
 * has the row, one at a time with progress, so the work itself happens on the rename screen this
 * hands off to — and never through Save, which would leave the database and Plex disagreeing.
 */
export function RowRenameDialog({
  open,
  onOpenChange,
  value,
  onChange,
  changed,
  seasonal,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  value: string;
  onChange: (value: string) => void;
  /** The typed name differs from the saved one: there is something to apply. */
  changed: boolean;
  /** The row follows seasons, so its name may carry the season too. */
  seasonal: boolean;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Rename on Plex</DialogTitle>
          <DialogDescription>
            Renaming rewrites this row on Plex for everyone who has it, so it
            gets its own screen. Nothing else here is touched.
          </DialogDescription>
        </DialogHeader>
        <div className="min-w-0 space-y-2">
          <Label htmlFor="row-rename">New name</Label>
          <Input id="row-rename" value={value} onChange={(event) => onChange(event.target.value)} />
          {/* The placeholders a new row's name box lists: without them the box reads as plain text,
              and nothing says {user} or {library_name} would work here. */}
          <TemplateVarsHint seasonal={seasonal} />
          {changed && (
            <p role="status" className="text-sm text-warning">
              Not applied yet &mdash; press <strong>Rename on Plex</strong> to
              change it on Plex. Saving this page won&rsquo;t.
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          {/* Only once the name actually differs from the saved one. Enabled on an unchanged name it
              offered to rewrite every collection on Plex, for every person, to the name they
              already had — minutes of writes for no change. */}
          <Button disabled={!changed || !value.trim()} onClick={onConfirm}>
            Rename on Plex
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
