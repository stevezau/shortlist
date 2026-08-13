import { useEffect, useRef } from "react";

import { QueryBoundary } from "@/components/query-boundary";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useLibraries, useLibraryCollections } from "@/lib/queries";
import type { CollectionInput, HubAnchorMap, PlexLibrary } from "@/lib/types";

const selectClass =
  "h-9 w-full rounded-md border bg-elevated px-3 text-sm focus-visible:outline-none " +
  "focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60";

type Entry = HubAnchorMap[string];
type Mode = "default" | "top" | "after" | "before";

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

/** No entry = inherit the global default; `top` = the very top; else after/before its anchor. */
function modeOf(entry: Entry | undefined): Mode {
  if (!entry) return "default";
  if (entry.top) return "top";
  return entry.before ? "before" : "after";
}

function LibraryAnchor({
  library,
  entry,
  onChange,
  rowSlug,
}: {
  library: PlexLibrary;
  entry: Entry | undefined;
  onChange: (next: Entry | undefined) => void;
  rowSlug?: string;
}) {
  const mode = modeOf(entry);
  const relative = mode === "after" || mode === "before";
  const collections = useLibraryCollections(library.key, relative, rowSlug);

  const setMode = (next: Mode) => {
    if (next === "default") return onChange(undefined);
    if (next === "top") return onChange({ top: true });
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
            <option value="default">Follow the default from Settings</option>
            <option value="top">Top of the shelf</option>
            <option value="after">Right after a collection…</option>
            <option value="before">Right before a collection…</option>
          </select>
        </div>
        {relative && (
          <div className="space-y-1">
            <Label htmlFor={`row-anchor-${library.key}`}>Collection</Label>
            {collections.isError ? (
              <p className="text-sm text-destructive-text">
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

/** Per-library placement of THIS row in the Recommended shelf. Each targeted library can inherit the
 *  global default, sit at the Top, or anchor after/before a collection. `pinnedTop` carries a legacy
 *  row-level pin over into per-library "Top" once, then `onConsumePin` lets the editor clear it. */
export function RowShelfPlacement({
  value,
  libraryKeys,
  media,
  rowSlug,
  pinnedTop = false,
  onConsumePin,
  onChange,
}: {
  value: HubAnchorMap;
  libraryKeys: string[];
  media: CollectionInput["media"];
  /** The row being edited. Only ITS own collections are dropped from the anchor list — every other
   *  Shortlist row is a legitimate anchor (issue #81). */
  rowSlug?: string;
  pinnedTop?: boolean;
  onConsumePin?: () => void;
  onChange: (next: HubAnchorMap) => void;
}) {
  const libraries = useLibraries();
  const migrated = useRef(false);

  const setLibrary = (key: string, entry: Entry | undefined) => {
    const next = { ...value };
    if (entry) next[key] = entry;
    else delete next[key];
    onChange(next);
  };

  // Legacy pin_top -> per-library Top, exactly once (only libraries without an explicit choice), the
  // moment the library list is known. Then tell the editor the pin is consumed so it clears pin_top.
  useEffect(() => {
    if (!pinnedTop || migrated.current || !libraries.data) return;
    migrated.current = true;
    const next = { ...value };
    let changed = false;
    for (const library of libraries.data) {
      if (targetsLibrary(library, libraryKeys, media) && !next[library.key]) {
        next[library.key] = { top: true };
        changed = true;
      }
    }
    if (changed) onChange(next);
    onConsumePin?.();
  }, [
    pinnedTop,
    libraries.data,
    libraryKeys,
    media,
    value,
    onChange,
    onConsumePin,
  ]);

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
                rowSlug={rowSlug}
                onChange={(entry) => setLibrary(library.key, entry)}
              />
            ))}
          </div>
        );
      }}
    </QueryBoundary>
  );
}
