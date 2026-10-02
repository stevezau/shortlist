import { useId, useState } from "react";

import { badgeVariants } from "@/components/ui/badge";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { useLibrarySearch } from "@/lib/queries";
import { MAX_PICKS } from "@/lib/season-draft";
import type { SeasonPick } from "@/lib/types";

import { ChosenItem, SearchResultRow, SeasonSearch } from "./season-search";

function titleWithYear(title: { title: string; year?: number | null }): string {
  return title.year ? `${title.title} (${title.year})` : title.title;
}

const samePick = (a: Pick<SeasonPick, "tmdb_id" | "media_type">, b: Pick<SeasonPick, "tmdb_id" | "media_type">) =>
  a.tmdb_id === b.tmdb_id && a.media_type === b.media_type;

/**
 * Films (or shows) the owner picks by hand for a season (#137 D3): searched in the libraries by title.
 * A hand pick is always used: no genre left out ever drops it.
 */
export function SeasonPicksPicker({
  picks,
  onChange,
}: {
  picks: SeasonPick[];
  onChange: (picks: SeasonPick[]) => void;
}) {
  const headingId = useId();
  const [query, setQuery] = useState("");
  const searched = useDebouncedValue(query, 250);
  const search = useLibrarySearch(searched);
  const full = picks.length >= MAX_PICKS;

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <div>
        <h4 id={headingId} className="text-sm font-semibold">
          Picked by hand
        </h4>
        <p className="text-sm text-muted-foreground">
          Search your libraries and add films one by one. Hand-picked films are always used.
        </p>
      </div>

      <SeasonSearch
        label="Search your libraries"
        placeholder="e.g. home alone"
        value={query}
        onValue={setQuery}
        searched={searched}
        search={search}
        resultsLabel="Titles found"
        empty={(q) => `Nothing in your libraries matches “${q}”.`}
      >
        {(title) => (
          <SearchResultRow
            key={`${title.media_type}/${title.tmdb_id}`}
            addLabel={`Add ${titleWithYear(title)}`}
            added={picks.some((pick) => samePick(pick, title))}
            full={full}
            onAdd={() => {
              onChange([
                ...picks,
                { tmdb_id: title.tmdb_id, media_type: title.media_type, title: title.title, year: title.year },
              ]);
              setQuery("");
            }}
          >
            <span className="font-medium">{title.title}</span>
            {title.year && <span className="text-muted-foreground"> ({title.year})</span>}
            {title.media_type === "show" && (
              <>
                {" "}
                <span className={badgeVariants({ variant: "secondary" })}>TV show</span>
              </>
            )}
          </SearchResultRow>
        )}
      </SeasonSearch>
      {full && <p className="text-sm text-muted-foreground">That’s the most a season can pick by hand ({MAX_PICKS}).</p>}

      {picks.length === 0 ? (
        <p className="text-sm text-muted-foreground">No films picked yet.</p>
      ) : (
        <ul aria-label="Picked films" className="flex flex-wrap gap-1.5">
          {picks.map((pick) => (
            <ChosenItem
              key={`${pick.media_type}/${pick.tmdb_id}`}
              removeLabel={`Remove ${pick.title}`}
              onRemove={() => onChange(picks.filter((p) => !samePick(p, pick)))}
            >
              {titleWithYear(pick)}
            </ChosenItem>
          ))}
        </ul>
      )}
    </section>
  );
}
