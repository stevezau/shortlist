import { Skeleton } from "@/components/ui/skeleton";
import { previewInput } from "@/lib/season-draft";
import { seasonVerdict } from "@/lib/season-verdict";
import { useSeasonPreview } from "@/lib/queries";
import type { Season, SeasonPreset } from "@/lib/types";

import { SeasonPreviewError } from "./season-preview-error";
import { SeasonVerdictChip } from "./season-verdict-chip";

/**
 * How many films a season finds in the libraries, and what that means for this row — counted by the
 * server (`POST /api/seasons/preview`) when this mounts, so a list only asks for what is on screen.
 */
export function SeasonFilmCount({
  source,
  rowSize,
  perPerson,
  noun = "films",
  chipWhenOk = false,
}: {
  source: Season | SeasonPreset;
  rowSize: number;
  perPerson: boolean;
  /** "films", or "films in your libraries" where nothing else says where they are counted. */
  noun?: "films" | "films in your libraries";
  /** Show "Enough for this row" too, not only a warning. */
  chipWhenOk?: boolean;
}) {
  const preview = useSeasonPreview(previewInput(source));

  if (preview.isPending) {
    return (
      <span className="inline-flex items-center">
        <span className="sr-only">Counting films…</span>
        <Skeleton aria-hidden="true" className="h-5 w-24" />
      </span>
    );
  }
  if (preview.isError) {
    return <SeasonPreviewError compact error={preview.error} onRetry={() => void preview.refetch()} />;
  }

  const { total } = preview.data;
  const verdict = seasonVerdict(total, rowSize, perPerson);
  const words = noun.replace(/^films/, total === 1 ? "film" : "films");
  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className="text-sm">{`${total} ${words}`}</span>
      {(chipWhenOk || verdict.level !== "ok") && <SeasonVerdictChip verdict={verdict} />}
    </span>
  );
}
