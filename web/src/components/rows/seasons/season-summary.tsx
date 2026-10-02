import type { UseQueryResult } from "@tanstack/react-query";
import { useId } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { seasonVerdict, titleNoun, type SeasonRow } from "@/lib/season-verdict";
import type { SeasonPreview, SeasonPreviewInput } from "@/lib/types";
import { cn } from "@/lib/utils";

import { SeasonPreviewError } from "./season-preview-error";

export type CountedPreview = SeasonPreview & { draft: SeasonPreviewInput };

/** What to do about a verdict, in the row's own word for its titles. */
function verdictHelp(level: "few" | "alike" | "ok", titles: string): string {
  if (level === "few") return `The row comes out short. Add a tag, a collection or some ${titles}.`;
  if (level === "alike") return `With under 100 ${titles}, everyone’s picks come from the same few.`;
  return "";
}

/**
 * What a draft season finds (#137 D10), beside the editor's form: how many of the row's kind of title in
 * the row's libraries, whether that's enough for the row the editor was opened from, where they come from,
 * a sample, and which other rows use the season. Every number is the server's count, as a run would make it.
 */
export function SeasonSummary({
  preview,
  row,
  alsoUsedBy,
}: {
  preview: UseQueryResult<CountedPreview>;
  row: SeasonRow;
  /** Other rows that follow this season, by name. */
  alsoUsedBy: string[];
}) {
  const headingId = useId();

  return (
    <aside aria-labelledby={headingId} className="space-y-4 rounded-lg border bg-card p-4 lg:sticky lg:top-0">
      <h3 id={headingId} className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        This season draws from
      </h3>
      <SummaryBody preview={preview} row={row} />
      {alsoUsedBy.length > 0 && (
        <p className="border-t pt-3 text-sm">
          <span className="text-muted-foreground">Also used by:</span> {alsoUsedBy.join(", ")}
        </p>
      )}
    </aside>
  );
}

function SummaryBody({ preview, row }: { preview: UseQueryResult<CountedPreview>; row: SeasonRow }) {
  const titles = titleNoun(row.media, 2);
  if (preview.isPending) {
    return (
      <div className="space-y-3">
        <p role="status" className="text-sm">
          {`Counting ${titles} in your libraries… the first count takes a few seconds.`}
        </p>
        <Skeleton aria-hidden="true" className="h-10 w-24" />
        <Skeleton aria-hidden="true" className="h-12 w-full" />
      </div>
    );
  }
  if (preview.isError) {
    return <SeasonPreviewError error={preview.error} onRetry={() => void preview.refetch()} />;
  }

  const data = preview.data;
  const verdict = seasonVerdict(data.total, row);
  const help = verdictHelp(verdict.level, titles);
  const used = data.draft;
  const breakdown: { label: string; value: number }[] = [
    ...((used.tags ?? []).length > 0 ? [{ label: "From TMDB tags", value: data.from_tags }] : []),
    ...(used.genre != null ? [{ label: "From the genre", value: data.from_genre }] : []),
    ...((used.collections ?? []).length > 0 ? [{ label: "From your collections", value: data.from_collections }] : []),
    ...((used.picks ?? []).length > 0 ? [{ label: "Picked by hand", value: data.from_picks }] : []),
  ];

  return (
    <>
      <div aria-live="polite" className="space-y-3">
        <p className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-4xl font-bold tabular-nums leading-none">{data.total}</span>
          <span className="text-sm text-muted-foreground">{titleNoun(row.media, data.total)} in your libraries</span>
        </p>
        {preview.isFetching && <p className="text-xs text-muted-foreground">Updating the count…</p>}
        <div
          className={cn(
            "space-y-1 rounded-md p-3 text-sm",
            verdict.level === "ok" ? "bg-success/10" : "bg-warning/10",
          )}
        >
          <p className={cn("font-medium", verdict.level === "ok" ? "text-success" : "text-warning")}>{verdict.text}</p>
          {help && <p className="text-muted-foreground">{help}</p>}
        </div>
      </div>

      {breakdown.length > 0 && (
        <dl className="space-y-1 text-sm">
          {breakdown.map((row, index) => (
            <div key={row.label} className="flex justify-between gap-3">
              <dt className="text-muted-foreground">{row.label}</dt>
              <dd className="tabular-nums">{index === 0 ? row.value : `+${row.value} more`}</dd>
            </div>
          ))}
        </dl>
      )}

      <div className="space-y-1 border-t pt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">A sample</h4>
        {data.sample.length === 0 ? (
          <p className="text-sm text-muted-foreground">{`Nothing yet. Add a tag, a collection or a ${titleNoun(row.media, 1)} to see some.`}</p>
        ) : (
          <ul className="space-y-0.5 text-sm">
            {data.sample.map((title, index) => (
              <li key={`${index}-${title}`}>{title}</li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
