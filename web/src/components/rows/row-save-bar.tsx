import { changesSummary, type DraftChange } from "@/components/rows/row-draft-diff";
import { Button } from "@/components/ui/button";

/**
 * The one place the editor's draft is saved, pinned to the bottom of the viewport.
 *
 * On a long page the save button would otherwise be off-screen, and "where did Save go" was the
 * friction a dialog never had. It names what is about to change, because "Save changes" on a page
 * with forty settings is otherwise a leap of faith.
 *
 * Save is never disabled for having nothing to save: a form that refuses to save what it thinks is
 * unchanged is unfixable when the comparison is the thing that is wrong.
 */
export function RowSaveBar({
  changes,
  isNew,
  saving,
  saveDisabled,
  onSave,
  onDiscard,
  onCancel,
}: {
  changes: DraftChange[];
  isNew: boolean;
  saving: boolean;
  saveDisabled: boolean;
  onSave: () => void;
  /** Put the form back to the saved row. */
  onDiscard: () => void;
  /** Leave without adding a new row. */
  onCancel: () => void;
}) {
  const summary = changesSummary(changes);
  return (
    <div
      role="region"
      aria-label="Unsaved changes"
      className="sticky bottom-0 z-10 -mx-4 flex flex-wrap items-center justify-end gap-x-2 gap-y-2 border-t bg-background/95 px-4 py-3 backdrop-blur-sm md:-mx-8 md:px-8"
    >
      <p
        role="status"
        className="w-full min-w-0 truncate text-sm text-muted-foreground sm:mr-auto sm:w-auto sm:flex-1"
        title={summary || undefined}
      >
        {changes.length > 0 ? (
          <>
            <span aria-hidden="true" className="mr-2 inline-block size-1.5 rounded-full bg-warning align-middle" />
            <b className="font-semibold text-foreground">
              {changes.length} unsaved change{changes.length === 1 ? "" : "s"}
            </b>
            {" · "}
            {summary}
          </>
        ) : isNew ? (
          "Nothing is on Plex until you add the row."
        ) : (
          "Row settings up to date"
        )}
      </p>
      {isNew ? (
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      ) : (
        <Button variant="ghost" onClick={onDiscard} disabled={changes.length === 0}>
          Discard
        </Button>
      )}
      <Button onClick={onSave} loading={saving} disabled={saveDisabled}>
        {isNew ? "Add row" : "Save changes"}
      </Button>
    </div>
  );
}
