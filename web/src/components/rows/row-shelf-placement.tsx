import { QueryBoundary } from "@/components/query-boundary";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useLibraries, useLibraryCollections } from "@/lib/queries";
import type { CollectionInput, HubAnchorMap, PlexLibrary } from "@/lib/types";

const selectClass =
  "h-9 w-full rounded-md border bg-elevated px-3 text-sm focus-visible:outline-none " +
  "focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60";

type Entry = HubAnchorMap[string];
type Mode = "default" | "after" | "before";

/** A row targets a library when it lists it, or (when it lists none) any library of its media type. */
function targetsLibrary(
  library: PlexLibrary,
  libraryKeys: string[],
  media: CollectionInput["media"],
): boolean {
  const mediaMatch = media === "both" || library.type === media;
  return libraryKeys.length === 0
    ? mediaMatch
    : libraryKeys.includes(library.key);
}

/** An existing entry means a set anchor; its absence means "inherit the global default". */
function modeOf(entry: Entry | undefined): Mode {
  if (!entry) return "default";
  return entry.before ? "before" : "after";
}

function LibraryAnchor({
  library,
  entry,
  onChange,
}: {
  library: PlexLibrary;
  entry: Entry | undefined;
  onChange: (next: Entry | undefined) => void;
}) {
  const mode = modeOf(entry);
  const collections = useLibraryCollections(library.key, mode !== "default");

  const setMode = (next: Mode) => {
    if (next === "default") return onChange(undefined);
    onChange({ anchor: entry?.anchor ?? "", before: next === "before" });
  };

  return (
    <div className="space-y-2 rounded-md border p-3">
      <p className="text-sm font-medium">{library.title}</p>
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label htmlFor={`row-mode-${library.key}`}>Position</Label>
          <select
            id={`row-mode-${library.key}`}
            className={selectClass + " w-52"}
            value={mode}
            onChange={(event) => setMode(event.target.value as Mode)}
          >
            <option value="default">Use the default (Settings)</option>
            <option value="after">Right after a collection…</option>
            <option value="before">Right before a collection…</option>
          </select>
        </div>
        {mode !== "default" && (
          <div className="space-y-1">
            <Label htmlFor={`row-anchor-${library.key}`}>Collection</Label>
            {collections.isError ? (
              <p className="text-sm text-destructive">
                Couldn’t load this library’s collections.
              </p>
            ) : (
              <select
                id={`row-anchor-${library.key}`}
                className={selectClass + " w-64"}
                disabled={collections.isPending}
                value={entry?.anchor ?? ""}
                onChange={(event) =>
                  onChange({
                    anchor: event.target.value,
                    before: mode === "before",
                  })
                }
              >
                <option value="" disabled>
                  {collections.isPending ? "Loading…" : "Choose a collection"}
                </option>
                {entry?.anchor &&
                  !collections.data?.some((c) => c.title === entry.anchor) && (
                    <option value={entry.anchor}>
                      {entry.anchor} (not found)
                    </option>
                  )}
                {collections.data?.map((c) => (
                  <option key={c.title} value={c.title}>
                    {c.title}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** Per-library override of where THIS row sits in the Recommended shelf. Each targeted library can
 *  inherit the global default (Settings → Row placement) or anchor to its own collection. */
export function RowShelfPlacement({
  value,
  libraryKeys,
  media,
  onChange,
}: {
  value: HubAnchorMap;
  libraryKeys: string[];
  media: CollectionInput["media"];
  onChange: (next: HubAnchorMap) => void;
}) {
  const libraries = useLibraries();

  const setLibrary = (key: string, entry: Entry | undefined) => {
    const next = { ...value };
    if (entry) next[key] = entry;
    else delete next[key];
    onChange(next);
  };

  return (
    <QueryBoundary
      query={libraries}
      skeleton={<Skeleton className="h-20 w-full" />}
    >
      {(all) => {
        const targeted = all.filter((library) =>
          targetsLibrary(library, libraryKeys, media),
        );
        if (targeted.length === 0) {
          return (
            <p className="text-sm text-muted-foreground">
              No matching libraries.
            </p>
          );
        }
        return (
          <div className="space-y-2">
            {targeted.map((library) => (
              <LibraryAnchor
                key={library.key}
                library={library}
                entry={value[library.key]}
                onChange={(entry) => setLibrary(library.key, entry)}
              />
            ))}
          </div>
        );
      }}
    </QueryBoundary>
  );
}
