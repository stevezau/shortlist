import { useId, useRef, useState } from "react";

import { badgeVariants } from "@/components/ui/badge";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { usePlexCollections } from "@/lib/queries";
import { MAX_COLLECTIONS } from "@/lib/season-draft";
import { titleNoun, type RowMedia } from "@/lib/season-verdict";
import type { SeasonCollection, SeasonPreview } from "@/lib/types";

import { ChosenItem, SearchResultRow, SeasonSearch } from "./season-search";

/** #137 D5: a collection is referenced by library and title, never ratingKey, because Kometa deletes its
 *  seasonal collections out of season and makes them again, with new keys, each year. */
export const MISSING_COLLECTION =
  "Not in your library right now. Kometa only creates its seasonal collections in season; on nights it's missing, this season uses its other sources.";

const sameCollection = (a: Pick<SeasonCollection, "section_key" | "title">, b: Pick<SeasonCollection, "section_key" | "title">) =>
  a.section_key === b.section_key && a.title === b.title;

/**
 * Plex collections for a season (#137 D3, D6): any collection in the libraries, Kometa's included,
 * read only — Shortlist never changes it.
 */
export function SeasonCollectionPicker({
  collections,
  onChange,
  perCollection,
  counting,
  media,
}: {
  collections: SeasonCollection[];
  onChange: (collections: SeasonCollection[]) => void;
  /** Each chosen collection's films the season uses, from the latest count. */
  perCollection: SeasonPreview["per_collection"] | undefined;
  counting: boolean;
  /** The row's media: a chosen collection's count is of what this row would use from it. */
  media: RowMedia;
}) {
  const headingId = useId();
  const [query, setQuery] = useState("");
  const searchBox = useRef<HTMLInputElement>(null);
  const searched = useDebouncedValue(query, 250);
  const search = usePlexCollections(searched);
  const full = collections.length >= MAX_COLLECTIONS;

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <div>
        <h4 id={headingId} className="text-sm font-semibold">
          From your library
        </h4>
        <p className="text-sm text-muted-foreground">
          A collection you already have in Plex, Kometa’s included. Shortlist only reads it — it never
          changes it.
        </p>
      </div>

      <SeasonSearch
        inputRef={searchBox}
        label="Search your Plex collections"
        placeholder="e.g. christmas"
        value={query}
        onValue={setQuery}
        searched={searched}
        search={search}
        resultsLabel="Collections found"
        empty={(q) => `No collection in your libraries has “${q}” in its title.`}
      >
        {(found) => (
          <SearchResultRow
            key={`${found.section_key}/${found.title}`}
            addLabel={`Add ${found.title} (${found.section_title})`}
            added={collections.some((c) => sameCollection(c, found))}
            full={full}
            onAdd={() => {
              onChange([
                ...collections,
                { section_key: found.section_key, section_title: found.section_title, title: found.title },
              ]);
              searchBox.current?.focus();
            }}
          >
            <span className="font-medium">{found.title}</span>{" "}
            {found.smart && <span className={badgeVariants({ variant: "secondary" })}>Smart</span>}{" "}
            <span className="block text-muted-foreground sm:inline">
              {found.section_title} · {found.count} {titleNoun(found.media_type, found.count)}
            </span>
          </SearchResultRow>
        )}
      </SeasonSearch>
      {full && (
        <p className="text-sm text-muted-foreground">
          That’s the most collections a season can have ({MAX_COLLECTIONS}).
        </p>
      )}

      {collections.length === 0 ? (
        <p className="text-sm text-muted-foreground">No collections yet.</p>
      ) : (
        <ul aria-label="Chosen collections" className="space-y-1.5">
          {collections.map((collection) => {
            const counted = perCollection?.find((c) => sameCollection(c, collection));
            return (
              <ChosenItem
                key={`${collection.section_key}/${collection.title}`}
                removeLabel={`Remove ${collection.title}`}
                onRemove={() => onChange(collections.filter((c) => !sameCollection(c, collection)))}
              >
                <span className="font-medium">{collection.title}</span>{" "}
                <span className="text-muted-foreground">
                  {collection.section_title}
                  {counted?.found && ` · ${counted.in_library} ${titleNoun(media, counted.in_library)} used`}
                  {!counted && counting && " · counting…"}
                </span>
                {counted && !counted.found && <span className="mt-1 block text-warning">{MISSING_COLLECTION}</span>}
              </ChosenItem>
            );
          })}
        </ul>
      )}
    </section>
  );
}
