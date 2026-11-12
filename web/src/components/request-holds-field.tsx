import { AlertTriangle, ChevronDown, Plus } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";

import { ChosenItem, SearchResultRow, SeasonSearch } from "@/components/rows/seasons/season-search";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";
import { useHoldPreview, useTmdbCompanies, useTmdbTags } from "@/lib/queries";
import {
  MAX_HOLD_TAGS,
  SUGGESTED_HOLD_TAGS,
  SUGGESTED_SHOW_HOLD_TAGS,
  TMDB_SHOW_GENRES,
  exceptionCount,
  joinTitles,
  type HoldDraft,
  type HoldMedia,
} from "@/lib/request-holds";
import { TMDB_MOVIE_GENRES } from "@/lib/season-draft";
import { selectedClass, unselectedClass } from "@/lib/selected";
import type { HoldPreviewInput, SeasonTag } from "@/lib/types";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { cn } from "@/lib/utils";

/** What differs between the two panels: TMDB's genre list, the words, and the suggested tags. */
const PANELS: Record<
  HoldMedia,
  {
    title: string;
    one: string;
    many: string;
    genres: readonly { id: number; name: string }[];
    genreHelp: string;
    tagHelp: string;
    placeholder: string;
    suggested: readonly SeasonTag[];
  }
> = {
  movie: {
    title: "Movies",
    one: "movie",
    many: "movies",
    genres: TMDB_MOVIE_GENRES,
    genreHelp: "Genres are broad: Music also catches musicals like A Star Is Born. A tag is usually the better pick.",
    tagHelp: "Words TMDB tags films with. “concert film” catches concert footage without catching musicals.",
    placeholder: "e.g. anime, concert",
    suggested: SUGGESTED_HOLD_TAGS,
  },
  show: {
    title: "Shows",
    one: "show",
    many: "shows",
    genres: TMDB_SHOW_GENRES,
    genreHelp: "TMDB’s TV genres: Reality, Talk, Kids and News exist only for shows.",
    tagHelp: "Words TMDB tags shows with. “anime” catches anime without catching every cartoon.",
    placeholder: "e.g. anime, sitcom",
    suggested: SUGGESTED_SHOW_HOLD_TAGS,
  },
};

/**
 * "Don't request these automatically": TMDB genres and tags whose titles wait in the inbox instead of
 * being sent on their own, picked for movies and shows apart. A title with ANY pick in its own panel is
 * held, unless it meets one of that panel's exceptions; the inbox's Send still sends it.
 */
export function RequestHoldsField({
  movies,
  shows,
  ratingLabel,
  onMovies,
  onShows,
}: {
  movies: HoldDraft;
  shows: HoldDraft;
  /** The request rating source's name ("TMDB", "IMDb"…): the rating exception reads the same score. */
  ratingLabel: string;
  onMovies: (draft: HoldDraft) => void;
  onShows: (draft: HoldDraft) => void;
}) {
  return (
    <fieldset className="space-y-5">
      <legend className="w-full border-t pt-5 font-medium">Don&rsquo;t request these automatically</legend>
      <p className="text-sm text-muted-foreground">
        A movie or show with <strong className="font-medium text-foreground">any</strong> genre or tag picked in its
        panel waits in your Requests inbox instead of being sent. You can still send it from there.
      </p>
      {/* Side by side only where each panel still fits its tag rows two across; stacked below that. */}
      <div className="grid gap-4 xl:grid-cols-2">
        <HoldPanel media="movie" draft={movies} ratingLabel={ratingLabel} onChange={onMovies} />
        <HoldPanel media="show" draft={shows} ratingLabel={ratingLabel} onChange={onShows} />
      </div>
    </fieldset>
  );
}

function HoldPanel({
  media,
  draft,
  ratingLabel,
  onChange,
}: {
  media: HoldMedia;
  draft: HoldDraft;
  ratingLabel: string;
  onChange: (draft: HoldDraft) => void;
}) {
  const panel = PANELS[media];
  const genresId = useId();
  const tagsId = useId();
  const [query, setQuery] = useState("");
  const searchBox = useRef<HTMLInputElement>(null);
  const searched = useDebouncedValue(query, 250);
  const search = useTmdbTags(searched, media);
  const { genres, tags } = draft;
  const chosen = new Set(tags.map((tag) => tag.id));
  const full = tags.length >= MAX_HOLD_TAGS;
  const suggestions = panel.suggested.filter((tag) => !chosen.has(tag.id));
  const addTag = (tag: SeasonTag) => onChange({ ...draft, tags: [...tags, { id: tag.id, name: tag.name }] });

  return (
    // A fieldset of its own, so each panel is a named group: both offer Documentary, Comedy and
    // "stand-up comedy", and a screen reader must hear which one a button belongs to.
    <fieldset className="min-w-0 space-y-5 rounded-lg border bg-card p-4">
      <legend className="sr-only">{panel.title}</legend>
      <h3 aria-hidden="true" className="font-medium">
        {panel.title}
      </h3>

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
          {panel.genres.map((genre) => {
            const on = genres.includes(genre.id);
            return (
              <Button
                key={genre.id}
                type="button"
                size="sm"
                variant="outline"
                className={on ? selectedClass : unselectedClass}
                aria-pressed={on}
                onClick={() =>
                  onChange({ ...draft, genres: on ? genres.filter((g) => g !== genre.id) : [...genres, genre.id] })
                }
              >
                {genre.name}
              </Button>
            );
          })}
        </div>
        <p className="text-xs text-muted-foreground">{panel.genreHelp}</p>
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
        <p className="text-sm text-muted-foreground">{panel.tagHelp}</p>
        {tags.length > 0 && (
          <ul aria-label="Held tags" className="grid gap-1.5 sm:grid-cols-2">
            {tags.map((tag) => (
              <ChosenItem
                key={tag.id}
                removeLabel={`Remove tag ${tag.name}`}
                onRemove={() => onChange({ ...draft, tags: tags.filter((t) => t.id !== tag.id) })}
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
          placeholder={panel.placeholder}
          value={query}
          onValue={setQuery}
          searched={searched}
          search={search}
          resultsLabel="TMDB tags found"
          empty={(q) => `TMDB has no tag matching “${q}”. Try a broader word.`}
        >
          {(tag) => {
            const count = (media === "show" ? tag.shows : tag.movies) ?? null;
            const noun = media === "show" ? (count === 1 ? "show" : "shows") : count === 1 ? "film" : "films";
            return (
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
                {count !== null && (
                  <span className="text-muted-foreground">
                    — {count} {noun} on TMDB
                  </span>
                )}
              </SearchResultRow>
            );
          }}
        </SeasonSearch>
        {full && <p className="text-sm text-muted-foreground">That&rsquo;s the most tags you can hold ({MAX_HOLD_TAGS}).</p>}
      </section>

      <HoldExceptions media={media} draft={draft} ratingLabel={ratingLabel} onChange={onChange} />

      <HoldPreviewPanel media={media} draft={draft} />
    </fieldset>
  );
}

/** The panel's exceptions, folded away: most owners never need one, and an open form of them would
 *  read as something to fill in. The collapsed row still says how many are set. */
function HoldExceptions({
  media,
  draft,
  ratingLabel,
  onChange,
}: {
  media: HoldMedia;
  draft: HoldDraft;
  ratingLabel: string;
  onChange: (draft: HoldDraft) => void;
}) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const studiosId = useId();
  const ratingId = useId();
  const [query, setQuery] = useState("");
  const searchBox = useRef<HTMLInputElement>(null);
  const searched = useDebouncedValue(query, 250);
  const search = useTmdbCompanies(searched);
  const { companies, minRating } = draft;
  const chosen = new Set(companies.map((c) => c.id));
  const full = companies.length >= MAX_HOLD_TAGS;
  const count = exceptionCount(draft);
  const panel = PANELS[media];

  return (
    <div className="rounded-md border">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between gap-3 rounded-md px-3 py-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="font-medium">
          Exceptions <span className="font-normal text-muted-foreground">· advanced</span>
        </span>
        <span className="flex items-center gap-1 text-xs text-muted-foreground tabular-nums">
          {count === 0 ? "none" : count === 1 ? "1 exception" : `${count} exceptions`}
          <ChevronDown
            aria-hidden="true"
            className={cn("h-3.5 w-3.5 transition-transform motion-reduce:transition-none", open && "rotate-180")}
          />
        </span>
      </button>
      {open && (
        <div id={bodyId} className="space-y-4 border-t px-3 py-3">
          <p className="text-sm text-muted-foreground">
            A match above still goes out on its own if it meets one of these.
          </p>

          <section aria-labelledby={studiosId} className="space-y-2">
            <h4 id={studiosId} className="text-sm font-medium">
              From a studio
            </h4>
            {media === "show" && (
              // TMDB has no network search, so what can be picked is a show's production companies.
              <p className="text-xs text-muted-foreground">
                Matched on TMDB&rsquo;s production companies, not the network a show airs on.
              </p>
            )}
            {companies.length > 0 && (
              <ul aria-label="Studios sent anyway" className="grid gap-1.5 sm:grid-cols-2">
                {companies.map((company) => (
                  <ChosenItem
                    key={company.id}
                    removeLabel={`Remove studio ${company.name}`}
                    onRemove={() => onChange({ ...draft, companies: companies.filter((c) => c.id !== company.id) })}
                  >
                    <span className="font-medium">{company.name}</span>
                  </ChosenItem>
                ))}
              </ul>
            )}
            <SeasonSearch
              inputRef={searchBox}
              label="Search TMDB studios"
              placeholder="e.g. A24, Pixar"
              value={query}
              onValue={setQuery}
              searched={searched}
              search={search}
              resultsLabel="TMDB studios found"
              empty={(q) => `TMDB has no studio matching “${q}”.`}
            >
              {(company) => (
                <SearchResultRow
                  key={company.id}
                  addLabel={`Add studio ${company.name}`}
                  added={chosen.has(company.id)}
                  full={full}
                  onAdd={() => {
                    onChange({ ...draft, companies: [...companies, { id: company.id, name: company.name }] });
                    searchBox.current?.focus();
                  }}
                >
                  <span className="font-medium">{company.name}</span>
                  {company.country && <span className="text-muted-foreground"> ({company.country})</span>}
                </SearchResultRow>
              )}
            </SeasonSearch>
          </section>

          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Label htmlFor={ratingId}>Rated at least</Label>
              <Input
                id={ratingId}
                type="number"
                min={0}
                max={10}
                step={0.1}
                placeholder="off"
                value={minRating ?? ""}
                // "" must become null, not 0: `Number("") === 0`, and a bar of 0 would send every match.
                onChange={(e) =>
                  onChange({ ...draft, minRating: e.target.value === "" ? null : Number(e.target.value) })
                }
                className="w-24"
              />
              <span className="text-sm">on {ratingLabel}</span>
            </div>
            <p className="text-xs text-muted-foreground">
              {minRating === null
                ? `Leave it blank to hold every matching ${panel.one}, however well rated.`
                : `A matching ${panel.one} rated ${minRating} or higher goes out anyway.`}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

/** What the picks would hold among the titles of this kind waiting in the inbox right now, so a pick that
 *  is broader than it looks (a tag on musicals too) shows itself before a run acts on it. */
function HoldPreviewPanel({ media, draft }: { media: HoldMedia; draft: HoldDraft }) {
  const panel = PANELS[media];
  // Debounced as a string: a fresh object each render would restart the timer on every render.
  const key = JSON.stringify({
    media_type: media,
    genres: [...draft.genres].sort((a, b) => a - b),
    tags: draft.tags.map((t) => t.id).sort((a, b) => a - b),
    except_companies: draft.companies.map((c) => c.id).sort((a, b) => a - b),
    except_min_rating: draft.minRating,
  });
  const settled = useDebouncedValue(key, 400);
  const body = useMemo(() => JSON.parse(settled) as Required<HoldPreviewInput>, [settled]);
  const picked = body.genres.length + body.tags.length > 0;
  const preview = useHoldPreview(body, picked);

  let content;
  if (!picked) {
    content = (
      <p className="text-sm text-muted-foreground">
        Nothing picked, so any {panel.one} can be requested automatically.
      </p>
    );
  } else if (preview.isPending) {
    content = (
      <div className="space-y-1.5">
        <p className="sr-only">Checking your inbox…</p>
        <Skeleton aria-hidden="true" className="h-5 w-2/3" />
        <Skeleton aria-hidden="true" className="h-5 w-1/2" />
      </div>
    );
  } else if (preview.isError) {
    content = (
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <p>{apiErrorMessage(preview.error, "Couldn’t check your inbox just now.")}</p>
        <Button type="button" variant="outline" size="sm" onClick={() => void preview.refetch()}>
          Retry
        </Button>
      </div>
    );
  } else {
    const { checked, held, unread } = preview.data;
    const excepted = preview.data.excepted ?? [];
    const stories = held.filter((h) => h.story);
    content = (
      <div className={cn("space-y-2", preview.isFetching && "opacity-70")}>
        <p className="text-sm font-medium">
          {checked === 0
            ? `No ${panel.many} are waiting in your inbox to check against.`
            : held.length === 0
              ? `Holds none of the ${checked} ${panel.many} waiting in your inbox.`
              : `Would hold ${held.length} of the ${checked} ${panel.many} waiting in your inbox:`}
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
          <ul
            aria-label={`${panel.title} these picks would hold`}
            className="divide-y rounded-md border bg-card"
          >
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
        {excepted.length > 0 && (
          <p className="text-xs text-muted-foreground">
            Sent anyway (exception): {joinTitles(excepted.map((e) => e.title))}.
          </p>
        )}
        {unread > 0 && (
          <p className="text-xs text-muted-foreground">
            TMDB didn&rsquo;t answer for {unread} {unread === 1 ? panel.one : panel.many}. A run holds those until it
            can check.
          </p>
        )}
      </div>
    );
  }

  return (
    <section
      aria-label={`What the ${panel.one} picks would hold`}
      aria-live="polite"
      className="rounded-md bg-muted/50 p-3"
    >
      {content}
    </section>
  );
}
