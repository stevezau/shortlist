import { AlertTriangle, Plus } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";

import { ChosenItem, SearchResultRow, SeasonSearch } from "@/components/rows/seasons/season-search";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";
import { useHoldPreview, useTmdbTags } from "@/lib/queries";
import { TMDB_MOVIE_GENRES } from "@/lib/season-draft";
import { selectedClass, unselectedClass } from "@/lib/selected";
import { MAX_HOLD_TAGS, SUGGESTED_HOLD_TAGS } from "@/lib/request-holds";
import type { HoldPreviewInput, SeasonTag } from "@/lib/types";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { cn } from "@/lib/utils";

/**
 * "Don't request these automatically": TMDB genres and tags whose movies wait in the inbox instead of
 * being sent on their own. A movie with ANY pick is held; the inbox's Send still sends it.
 */
export function RequestHoldsField({
  genres,
  tags,
  onGenres,
  onTags,
}: {
  genres: number[];
  tags: SeasonTag[];
  onGenres: (genres: number[]) => void;
  onTags: (tags: SeasonTag[]) => void;
}) {
  const genresId = useId();
  const tagsId = useId();
  const [query, setQuery] = useState("");
  const searchBox = useRef<HTMLInputElement>(null);
  const searched = useDebouncedValue(query, 250);
  const search = useTmdbTags(searched);
  const chosen = new Set(tags.map((tag) => tag.id));
  const full = tags.length >= MAX_HOLD_TAGS;
  const suggestions = SUGGESTED_HOLD_TAGS.filter((tag) => !chosen.has(tag.id));
  const addTag = (tag: SeasonTag) => onTags([...tags, { id: tag.id, name: tag.name }]);

  return (
    <fieldset className="space-y-5">
      <legend className="w-full border-t pt-5 font-medium">Don&rsquo;t request these automatically</legend>
      <p className="text-sm text-muted-foreground">
        A movie with <strong className="font-medium text-foreground">any</strong> genre or tag picked here
        waits in your Requests inbox instead of being sent. You can still send it from there.
      </p>

      <section aria-labelledby={genresId} className="space-y-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h4 id={genresId} className="text-sm font-medium">
            Genres
          </h4>
          <span className="text-xs text-muted-foreground tabular-nums">
            {genres.length ? `${genres.length} picked` : "None picked"}
          </span>
        </div>
        <div className="flex flex-wrap gap-2">
          {TMDB_MOVIE_GENRES.map((genre) => {
            const on = genres.includes(genre.id);
            return (
              <Button
                key={genre.id}
                type="button"
                size="sm"
                variant="outline"
                className={on ? selectedClass : unselectedClass}
                aria-pressed={on}
                onClick={() => onGenres(on ? genres.filter((g) => g !== genre.id) : [...genres, genre.id])}
              >
                {genre.name}
              </Button>
            );
          })}
        </div>
        <p className="text-xs text-muted-foreground">
          Genres are broad: Music also catches musicals like A Star Is Born. A tag is usually the better pick.
        </p>
      </section>

      <section aria-labelledby={tagsId} className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h4 id={tagsId} className="text-sm font-medium">
            TMDB tags
          </h4>
          <span className="text-xs text-muted-foreground tabular-nums">
            {tags.length ? `${tags.length} picked` : "None picked"}
          </span>
        </div>
        <p className="text-sm text-muted-foreground">
          Words TMDB tags films with. &ldquo;concert film&rdquo; catches concert footage without catching musicals.
        </p>
        {tags.length > 0 && (
          <ul aria-label="Held tags" className="grid gap-1.5 sm:grid-cols-2">
            {tags.map((tag) => (
              <ChosenItem
                key={tag.id}
                removeLabel={`Remove tag ${tag.name}`}
                onRemove={() => onTags(tags.filter((t) => t.id !== tag.id))}
              >
                <span className="font-medium">{tag.name}</span>
              </ChosenItem>
            ))}
          </ul>
        )}
        {suggestions.length > 0 && !full && (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Suggested</span>
            {suggestions.map((tag) => (
              <Button
                key={tag.id}
                type="button"
                size="sm"
                variant="outline"
                className="border-dashed text-muted-foreground"
                aria-label={`Add tag ${tag.name}`}
                onClick={() => addTag(tag)}
              >
                <Plus aria-hidden="true" className="h-3.5 w-3.5" />
                {tag.name}
              </Button>
            ))}
          </div>
        )}
        <SeasonSearch
          inputRef={searchBox}
          label="Search TMDB tags"
          placeholder="e.g. anime, concert"
          value={query}
          onValue={setQuery}
          searched={searched}
          search={search}
          resultsLabel="TMDB tags found"
          empty={(q) => `TMDB has no tag matching “${q}”. Try a broader word.`}
        >
          {(tag) => (
            <SearchResultRow
              key={tag.id}
              addLabel={`Add tag ${tag.name}`}
              added={chosen.has(tag.id)}
              full={full}
              onAdd={() => {
                addTag(tag);
                searchBox.current?.focus();
              }}
            >
              <span className="font-medium">{tag.name}</span>{" "}
              <span className="text-muted-foreground">
                — {tag.movies} {tag.movies === 1 ? "film" : "films"} on TMDB
              </span>
            </SearchResultRow>
          )}
        </SeasonSearch>
        {full && <p className="text-sm text-muted-foreground">That&rsquo;s the most tags you can hold ({MAX_HOLD_TAGS}).</p>}
      </section>

      <HoldPreviewPanel genres={genres} tags={tags} />
    </fieldset>
  );
}

/** What the picks would hold among the movies waiting in the inbox right now, so a pick that is broader
 *  than it looks (a tag on musicals too) shows itself before a run acts on it. */
function HoldPreviewPanel({ genres, tags }: { genres: number[]; tags: SeasonTag[] }) {
  // Debounced as a string: a fresh object each render would restart the timer on every render.
  const key = JSON.stringify({
    genres: [...genres].sort((a, b) => a - b),
    tags: tags.map((t) => t.id).sort((a, b) => a - b),
  });
  const settled = useDebouncedValue(key, 400);
  const draft = useMemo(() => JSON.parse(settled) as Required<HoldPreviewInput>, [settled]);
  const picked = draft.genres.length + draft.tags.length > 0;
  const preview = useHoldPreview(draft, picked);

  let body;
  if (!picked) {
    body = <p className="text-sm text-muted-foreground">Nothing picked, so any movie can be requested automatically.</p>;
  } else if (preview.isPending) {
    body = (
      <div className="space-y-1.5">
        <p className="sr-only">Checking your inbox…</p>
        <Skeleton aria-hidden="true" className="h-5 w-2/3" />
        <Skeleton aria-hidden="true" className="h-5 w-1/2" />
      </div>
    );
  } else if (preview.isError) {
    body = (
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <p>{apiErrorMessage(preview.error, "Couldn’t check your inbox just now.")}</p>
        <Button type="button" variant="outline" size="sm" onClick={() => void preview.refetch()}>
          Retry
        </Button>
      </div>
    );
  } else {
    const { checked, held, unread } = preview.data;
    const stories = held.filter((h) => h.story);
    body = (
      <div className={cn("space-y-2", preview.isFetching && "opacity-70")}>
        <p className="text-sm font-medium">
          {checked === 0
            ? "No movies are waiting in your inbox to check against."
            : held.length === 0
              ? `Holds none of the ${checked} movies waiting in your inbox.`
              : `Would hold ${held.length} of the ${checked} movies waiting in your inbox:`}
        </p>
        {stories.length > 0 && (
          <p className="flex items-start gap-2 rounded-md bg-warning/10 p-2 text-sm text-warning">
            <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
            <span>
              {stories.length === 1 ? "1 of these is a story film" : `${stories.length} of these are story films`},
              not footage or a documentary. A pick may be broader than you meant.
            </span>
          </p>
        )}
        {held.length > 0 && (
          <ul aria-label="Movies these picks would hold" className="divide-y rounded-md border bg-card">
            {held.map((h) => (
              <li key={h.tmdb_id} className="flex flex-wrap items-baseline justify-between gap-x-3 px-3 py-1.5 text-sm">
                <span className="min-w-0">
                  {h.story && <AlertTriangle aria-label="Story film" className="mr-1 inline h-3.5 w-3.5 text-warning" />}
                  {h.title}
                  {h.year ? <span className="text-muted-foreground"> ({h.year})</span> : null}
                </span>
                <span className="text-xs text-muted-foreground">{h.reason}</span>
              </li>
            ))}
          </ul>
        )}
        {unread > 0 && (
          <p className="text-xs text-muted-foreground">
            TMDB didn&rsquo;t answer for {unread} {unread === 1 ? "movie" : "movies"}. A run holds those until it can check.
          </p>
        )}
      </div>
    );
  }

  return (
    <section aria-label="What these picks would hold" aria-live="polite" className="rounded-md bg-muted/50 p-3">
      {body}
    </section>
  );
}
