import { Pencil } from "lucide-react";
import { Link } from "react-router";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useRequestRowSources } from "@/lib/queries";
import {
  CONNECTIONS_SETTINGS,
  NO_REQUEST_SOURCE,
  noRequestSource,
} from "@/lib/row-kinds";
import {
  ROW_TEMPLATE_GROUPS,
  ROW_TEMPLATES,
  type RowTemplate,
} from "@/lib/row-templates";

/**
 * The "what can I build here?" step, shown before the row editor.
 *
 * Adding a row used to open a blank 17-field form, which only ever helped someone who already knew
 * what they wanted. Every tile names the two or three settings it changes, so choosing one also
 * teaches the knobs — and "Start from scratch" is always there for anyone who doesn't want the help.
 */
/** A tile that can't be picked yet, with what to set up first and where. */
function DisabledTemplateTile({ template }: { template: RowTemplate }) {
  return (
    <div
      aria-disabled="true"
      className="flex flex-col gap-1.5 rounded-lg border border-dashed p-4 text-left opacity-80"
    >
      <span className="text-xl" aria-hidden="true">
        {template.emoji}
      </span>
      <span className="font-medium text-muted-foreground">{template.title}</span>
      <span className="text-sm text-muted-foreground">
        {NO_REQUEST_SOURCE}{" "}
        <Link
          to={CONNECTIONS_SETTINGS}
          className="underline underline-offset-2 hover:text-foreground"
        >
          Settings &rsaquo; Connections
        </Link>
      </span>
    </div>
  );
}

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
  // A "Your requests" row needs something that can say who asked for what. The setup check reads
  // every source in turn, so it runs only while the gallery is open; until it answers, the tile is
  // offered — the editor's own panel says the same thing, in more detail, once the row is opened.
  const sources = useRequestRowSources("", open);
  const noSource = sources.data !== undefined && noRequestSource(sources.data);
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Add a row</DialogTitle>
          <DialogDescription>
            Pick a starting point. It fills in the settings for you; you can
            change any of them, including the kind of row, afterwards.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-6">
          {ROW_TEMPLATE_GROUPS.map((group) => {
            const templates = ROW_TEMPLATES.filter(
              (template) => template.kind === group.kind,
            );
            if (templates.length === 0) return null;
            return (
              <div key={group.kind} className="flex flex-col gap-2">
                <div>
                  <h3 className="font-medium">{group.heading}</h3>
                  <p className="text-sm text-muted-foreground">
                    {group.description}
                  </p>
                </div>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {templates.map((template) =>
                    template.values.requests_row && noSource ? (
                      <DisabledTemplateTile
                        key={template.id}
                        template={template}
                      />
                    ) : (
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
                    ),
                  )}
                </div>
              </div>
            );
          })}

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
