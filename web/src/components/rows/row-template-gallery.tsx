import { Pencil } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ROW_TEMPLATES, type RowTemplate } from "@/lib/row-templates";

/**
 * The "what can I build here?" step, shown before the row editor.
 *
 * Adding a row used to open a blank 17-field form, which only ever helped someone who already knew
 * what they wanted. Every tile names the two or three settings it changes, so choosing one also
 * teaches the knobs — and "Start from scratch" is always there for anyone who doesn't want the help.
 */
export function RowTemplateGallery({
  open,
  onPick,
  onClose,
}: {
  open: boolean;
  /** Null = start from scratch. */
  onPick: (template: RowTemplate | null) => void;
  onClose: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Add a row</DialogTitle>
          <DialogDescription>
            Start from one of these and change anything you like, or build one
            from scratch.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {ROW_TEMPLATES.map((template) => (
            <button
              key={template.id}
              type="button"
              onClick={() => onPick(template)}
              className="flex flex-col gap-1.5 rounded-lg border p-4 text-left transition-colors hover:border-primary/50 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="text-xl" aria-hidden="true">
                {template.emoji}
              </span>
              <span className="font-medium">{template.title}</span>
              <span className="text-sm text-muted-foreground">
                {template.blurb}
              </span>
              <span className="mt-1 flex flex-wrap gap-1">
                {template.highlights.map((highlight) => (
                  <span
                    key={highlight}
                    className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                  >
                    {highlight}
                  </span>
                ))}
              </span>
            </button>
          ))}

          <button
            type="button"
            onClick={() => onPick(null)}
            className="flex flex-col gap-1.5 rounded-lg border border-dashed p-4 text-left transition-colors hover:border-primary/50 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Pencil className="h-5 w-5 text-muted-foreground" aria-hidden />
            <span className="font-medium">Start from scratch</span>
            <span className="text-sm text-muted-foreground">
              An empty row you fill in yourself.
            </span>
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
