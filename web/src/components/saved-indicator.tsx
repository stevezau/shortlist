import { CheckCircle2 } from "lucide-react";

/**
 * The "Saved" confirmation shown next to a Save button after a successful write. One component so
 * every settings section, the requests panel, and the per-user drawer confirm a save identically.
 */
export function SavedIndicator({
  show,
  as: Tag = "p",
}: {
  show: boolean;
  /** `span` when it sits inline beside other inline controls; `p` (default) otherwise. */
  as?: "p" | "span";
}) {
  if (!show) return null;
  return (
    <Tag
      role="status"
      className="flex items-center gap-1.5 text-sm text-success"
    >
      <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
      Saved
    </Tag>
  );
}
