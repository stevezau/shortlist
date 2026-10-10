import { Skeleton } from "@/components/ui/skeleton";
import { previewInput } from "@/lib/season-draft";
import { seasonVerdict, titleNoun, type SeasonRow } from "@/lib/season-verdict";
import { useSeasonPreview } from "@/lib/queries";
import type { Season, SeasonPreset } from "@/lib/types";

import { SeasonPreviewError } from "./season-preview-error";
import { SeasonVerdictChip } from "./season-verdict-chip";

/**
 * How many of the row's kind of title a season finds in the row's libraries, and what that means for the
 * row — counted by the server (`POST /api/seasons/preview`) when this mounts, so a list only asks for what
 * is on screen.
 */
export function SeasonFilmCount({
  source,
  row,
  inLibraries = false,
  chipWhenOk = false,
}: {
  source: Season | SeasonPreset;
  row: SeasonRow;
  /** Say "in your libraries" too, where nothing else says where they are counted. */
  inLibraries?: boolean;
  /** Show the match-count verdict too, not only a warning. */
  chipWhenOk?: boolean;
}) {
  const preview = useSeasonPreview(previewInput(source, row));

  if (preview.isPending) {
    return (
      <span className="inline-flex items-center">
        <span className="sr-only">{`Counting ${titleNoun(row.media, 2)}…`}</span>
        <Skeleton aria-hidden="true" className="h-5 w-24" />
      </span>
    );
  }
  if (preview.isError) {
    return <SeasonPreviewError compact error={preview.error} onRetry={() => void preview.refetch()} />;
  }

  const { total } = preview.data;
  const verdict = seasonVerdict(preview.data, row);
  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className="text-sm">{`${total} ${titleNoun(row.media, total)}${inLibraries ? " in your libraries" : ""}`}</span>
      {(chipWhenOk || verdict.level !== "ok") && <SeasonVerdictChip verdict={verdict} />}
    </span>
  );
}
