import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ThemeDiff, ThemePreview } from "@/lib/types";

/** What a refinement would add and remove, with the choice to keep or drop it (#138). */
export function DiffCard({
  preview,
  diff,
  onKeep,
  onDiscard,
  keepLabel = "Use the new list",
  note = "Use the new list puts it in this editor. It is saved with the row when you press Save changes.",
}: {
  preview: ThemePreview;
  diff: ThemeDiff;
  onKeep: () => void;
  onDiscard: () => void;
  keepLabel?: string;
  note?: string;
}) {
  return (
    <section aria-label="What would change" className="space-y-3 rounded-lg border border-border-strong bg-elevated p-4">
      <h3 className="text-sm font-semibold">What would change</h3>
      {diff.rules_changed && <p className="text-sm">The limits changed (length, year or rating).</p>}
      <TitleList label={`Titles added (${diff.added_count})`} titles={diff.added} />
      <TitleList label={`Titles removed (${diff.removed_count})`} titles={diff.removed} />
      <TitleList label={`Tags added (${diff.tags_added.length})`} titles={diff.tags_added} />
      <TitleList label={`Tags removed (${diff.tags_removed.length})`} titles={diff.tags_removed} />
      <TitleList label={`Genres added (${diff.genres_added.length})`} titles={diff.genres_added} />
      <TitleList label={`Genres removed (${diff.genres_removed.length})`} titles={diff.genres_removed} />
      <p className="text-sm text-muted-foreground">
        Named titles: {diff.before_count} before, {diff.after_count} after. {diff.unchanged.length}{" "}
        {diff.unchanged.length === 1 ? "stays" : "stay"} the same. The new list has {preview.stats.after_rules}{" "}
        titles on your server after limits.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" onClick={onKeep}>
          {keepLabel}
        </Button>
        <Button type="button" variant="ghost" onClick={onDiscard}>
          Keep the current list
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">{note}</p>
    </section>
  );
}

function TitleList({ label, titles }: { label: string; titles: string[] }) {
  if (titles.length === 0) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <ul className="flex flex-wrap gap-1.5 text-sm">
        {titles.map((title) => (
          <li key={title}>
            <Badge variant="outline">{title}</Badge>
          </li>
        ))}
      </ul>
    </div>
  );
}
