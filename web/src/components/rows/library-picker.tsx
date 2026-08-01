import { RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";
import { useLibraries } from "@/lib/queries";
import type { PlexLibrary } from "@/lib/types";

type Media = "movie" | "show" | "both";

/** The media type a row curates, derived from the libraries it targets. */
function deriveMedia(selectedKeys: string[], libraries: PlexLibrary[]): Media {
  const types = new Set(
    selectedKeys
      .map((k) => libraries.find((l) => l.key === k)?.type)
      .filter((t): t is "movie" | "show" => Boolean(t)),
  );
  if (types.size !== 1) return "both";
  return types.has("movie") ? "movie" : "show";
}

/**
 * What this row keeps doing while the library list is unavailable — which is NOT "build in all of
 * them". A row with saved `library_keys` goes on building only in those; the picker just can't
 * name them.
 */
function currentTargets(libraryKeys: string[]): string {
  if (libraryKeys.length === 0)
    return "For now it keeps building in every library, as it does today.";
  return libraryKeys.length === 1
    ? "For now it keeps building in the one library you already picked — that doesn’t change."
    : `For now it keeps building in the ${libraryKeys.length} libraries you already picked — that doesn’t change.`;
}

/**
 * Per-row delivery-target picker. A Plex collection lives in one library, so a row builds one
 * collection per library it's pointed at. `library_keys` empty means "every library OF THIS ROW'S
 * MEDIA TYPE" — which is what the engine does with it (`RowSpec.library_keys`) — and any subset
 * targets just those. `media` is derived back from the selection, so a row only ever curates types
 * it can actually deliver.
 *
 * `media` has to come IN as well as out, or an empty `library_keys` renders as every box ticked
 * regardless of type: a movies-only row showed its TV libraries ticked while never building there,
 * and the shelf-position list right below it disagreed by naming only the movie ones. Worse, toggling
 * anything then re-derived `media` as "both" from those phantom ticks — and a `{top_seed}` row at one
 * seed can only seed one type, so its other library silently built empty.
 */
export function LibraryPicker({
  libraryKeys,
  media,
  onChange,
}: {
  libraryKeys: string[];
  media: Media;
  onChange: (next: { library_keys: string[]; media: Media }) => void;
}) {
  const query = useLibraries();

  return (
    <div className="space-y-2 border-t pt-4">
      <Label>Libraries</Label>
      {query.isPending ? (
        <Skeleton className="h-16 w-full" />
      ) : query.isError ? (
        <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-3">
          <p role="alert" className="text-sm text-foreground">
            {apiErrorMessage(
              query.error,
              "Couldn’t list your Plex libraries — check the Plex connection in Settings.",
            )}{" "}
            {currentTargets(libraryKeys)}
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
          >
            <RefreshCw aria-hidden="true" />
            Try again
          </Button>
        </div>
      ) : query.data.length === 0 ? (
        <div className="space-y-2 rounded-md border border-dashed bg-muted/30 p-3">
          <p className="text-sm text-muted-foreground">
            This Plex server has no movie or TV libraries yet, so there is
            nowhere to build a row. Add one in Plex, then try again.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
          >
            <RefreshCw aria-hidden="true" />
            Try again
          </Button>
        </div>
      ) : (
        (() => {
          const libraries = query.data;
          const allKeys = libraries.map((l) => l.key);
          // Libraries this row can actually deliver into. [] means "all of them", which for a
          // typed row means all of THAT type — never the ones it would skip.
          const inScope = (key: string) =>
            media === "both" ||
            libraries.find((l) => l.key === key)?.type === media;
          const scopedKeys = allKeys.filter(inScope);
          const selected =
            libraryKeys.length === 0 ? scopedKeys : libraryKeys;
          const toggle = (key: string) => {
            const has = selected.includes(key);
            if (has && selected.length === 1) return; // a row must target at least one library
            const next = has
              ? selected.filter((k) => k !== key)
              : [...selected, key];
            const nextMedia = deriveMedia(next, libraries);
            // Every library of the row's (new) type ticked -> store [] so the row follows the
            // server and picks up libraries added later. Compared against that type's libraries,
            // not against all of them, or a movies-only row could never store the "follow" form.
            const nextScoped = allKeys.filter(
              (key) =>
                nextMedia === "both" ||
                libraries.find((l) => l.key === key)?.type === nextMedia,
            );
            const keys = next.length === nextScoped.length ? [] : next;
            onChange({ library_keys: keys, media: nextMedia });
          };
          return (
            <>
              <div className="flex flex-wrap gap-2">
                {libraries.map((lib) => {
                  const checked = selected.includes(lib.key);
                  return (
                    <label
                      key={lib.key}
                      className="flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm transition-colors hover:bg-muted/50"
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggle(lib.key)}
                        className="h-4 w-4 accent-primary"
                      />
                      <span className="font-medium">{lib.title}</span>
                      <span className="text-xs text-muted-foreground">
                        {lib.type === "movie" ? "movies" : "shows"}
                      </span>
                    </label>
                  );
                })}
              </div>
              <p className="text-sm text-muted-foreground">
                This row builds a collection in each ticked library. All ticked
                = every library, including any you add later.
              </p>
            </>
          );
        })()
      )}
    </div>
  );
}
