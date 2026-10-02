import { useId, useState } from "react";

import { useDebouncedValue } from "@/lib/use-debounced-value";
import { useTmdbTags } from "@/lib/queries";
import { MAX_TAGS } from "@/lib/season-draft";
import type { SeasonTag } from "@/lib/types";

import { ChosenItem, SearchResultRow, SeasonSearch } from "./season-search";

/**
 * TMDB tags for a season (#137): search TMDB's tags by name, Add the ones that fit, and see how many
 * films each finds in the libraries. No tag id is ever typed or shown.
 */
export function SeasonTagPicker({
  tags,
  onChange,
  perTag,
  counting,
}: {
  tags: SeasonTag[];
  onChange: (tags: SeasonTag[]) => void;
  /** Tag id → its films in the libraries, from the latest count; undefined while there is none. */
  perTag: Record<string, number> | undefined;
  /** A count is on its way, so a tag it doesn't have yet is being counted rather than unknown. */
  counting: boolean;
}) {
  const headingId = useId();
  const [query, setQuery] = useState("");
  const searched = useDebouncedValue(query, 250);
  const search = useTmdbTags(searched);
  const chosen = new Set(tags.map((tag) => tag.id));
  const full = tags.length >= MAX_TAGS;

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <div>
        <h4 id={headingId} className="text-sm font-semibold">
          TMDB tags
        </h4>
        <p className="text-sm text-muted-foreground">
          Words people tag films with on TMDB, like “thanksgiving” or “fireworks”. New films with a tag
          join the season by themselves.
        </p>
      </div>

      <SeasonSearch
        label="Search TMDB tags"
        placeholder="e.g. thanksgiving"
        value={query}
        onValue={setQuery}
        searched={searched}
        search={search}
        resultsLabel="TMDB tags found"
        empty={(q) => `TMDB has no tag matching “${q}”. Try a broader word, or add a collection or your own picks below.`}
      >
        {(tag) => (
          <SearchResultRow
            key={tag.id}
            addLabel={`Add tag ${tag.name}`}
            added={chosen.has(tag.id)}
            full={full}
            onAdd={() => onChange([...tags, { id: tag.id, name: tag.name }])}
          >
            <span className="font-medium">{tag.name}</span>{" "}
            <span className="text-muted-foreground">
              — {tag.movies} {tag.movies === 1 ? "film" : "films"} on TMDB
            </span>
          </SearchResultRow>
        )}
      </SeasonSearch>
      {full && <p className="text-sm text-muted-foreground">That’s the most tags a season can have ({MAX_TAGS}).</p>}

      {tags.length === 0 ? (
        <p className="text-sm text-muted-foreground">No tags yet.</p>
      ) : (
        <ul aria-label="Chosen tags" className="grid gap-1.5 sm:grid-cols-2">
          {tags.map((tag) => {
            const count = perTag?.[String(tag.id)];
            return (
              <ChosenItem
                key={tag.id}
                removeLabel={`Remove tag ${tag.name}`}
                onRemove={() => onChange(tags.filter((t) => t.id !== tag.id))}
              >
                <span className="font-medium">{tag.name}</span>{" "}
                {count !== undefined ? (
                  <span className="text-muted-foreground">{`${count} in your libraries`}</span>
                ) : (
                  counting && <span className="text-muted-foreground">counting…</span>
                )}
              </ChosenItem>
            );
          })}
        </ul>
      )}
    </section>
  );
}
