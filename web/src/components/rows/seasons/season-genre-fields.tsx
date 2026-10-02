import { useId } from "react";

import { Label } from "@/components/ui/label";
import { MAX_EXCLUDED_GENRES, TMDB_MOVIE_GENRES, genreName } from "@/lib/season-draft";

import { ChosenItem } from "./season-search";
import { SELECT_CLASS } from "./select-class";

/**
 * A season's genres (#137 D3): one whole genre to add, at TMDB's well-rated floor — the way Halloween
 * adds Horror — and genres to leave out of the tag and collection films. A hand pick is never left out.
 */
export function SeasonGenreFields({
  genre,
  onGenre,
  excluded,
  onExcluded,
}: {
  genre: number | null;
  onGenre: (genre: number | null) => void;
  excluded: number[];
  onExcluded: (excluded: number[]) => void;
}) {
  const headingId = useId();
  const genreId = useId();
  const genreHintId = useId();
  const leaveOutId = useId();
  const leaveOutHintId = useId();
  const full = excluded.length >= MAX_EXCLUDED_GENRES;

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h4 id={headingId} className="text-sm font-semibold">
        Genres
      </h4>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor={genreId}>Also include a genre</Label>
          <select
            id={genreId}
            aria-describedby={genreHintId}
            className={SELECT_CLASS}
            value={genre ?? ""}
            onChange={(event) => onGenre(event.target.value === "" ? null : Number(event.target.value))}
          >
            <option value="">No genre</option>
            {TMDB_MOVIE_GENRES.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
          <p id={genreHintId} className="text-sm text-muted-foreground">
            Adds that genre’s well-rated films (200+ votes on TMDB), the way Halloween adds Horror.
          </p>
        </div>

        <div className="space-y-2">
          <Label htmlFor={leaveOutId}>Leave out films of these genres</Label>
          <select
            id={leaveOutId}
            aria-describedby={leaveOutHintId}
            className={SELECT_CLASS}
            value=""
            disabled={full}
            onChange={(event) => {
              if (event.target.value !== "") onExcluded([...excluded, Number(event.target.value)]);
            }}
          >
            <option value="">{full ? `Up to ${MAX_EXCLUDED_GENRES} genres` : "Choose a genre to leave out…"}</option>
            {TMDB_MOVIE_GENRES.filter((g) => !excluded.includes(g.id)).map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
          {excluded.length > 0 && (
            <ul aria-label="Genres left out" className="flex flex-wrap gap-1.5">
              {excluded.map((id) => (
                <ChosenItem
                  key={id}
                  removeLabel={`Stop leaving out ${genreName(id)}`}
                  onRemove={() => onExcluded(excluded.filter((g) => g !== id))}
                >
                  {genreName(id)}
                </ChosenItem>
              ))}
            </ul>
          )}
          <p id={leaveOutHintId} className="text-sm text-muted-foreground">
            Hand-picked films are never left out.
          </p>
        </div>
      </div>
    </section>
  );
}
