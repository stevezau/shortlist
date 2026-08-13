import { useEffect, useRef } from "react";

import { QueryBoundary } from "@/components/query-boundary";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useCollections, useLibraries, useLibraryCollections } from "@/lib/queries";
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

/** A choice in the anchor dropdown, encoded so one <select> can offer both kinds.
 *
 * Two kinds, because they are addressed differently and cannot share a value space: a foreign
 * collection is anchored by TITLE, a Shortlist row by SLUG. Prefixing keeps them apart even when a
 * row and a collection are called the same thing. */
function encodeChoice(entry: Entry | undefined): string {
  if (!entry) return "";
  if (entry.row) return `row:${entry.row}`;
  return entry.anchor ? `coll:${entry.anchor}` : "";
}

function LibraryAnchor({
  library,
  entry,
  onChange,
  rowSlug,
  otherRows,
}: {
  library: PlexLibrary;
  entry: Entry | undefined;
  onChange: (next: Entry | undefined) => void;
  rowSlug?: string;
  /** Every OTHER row, unfiltered — narrowed per library below. */
  otherRows: {
    slug: string;
    name: string;
    media: CollectionInput["media"];
    libraryKeys: string[];
  }[];
}) {
  const mode = modeOf(entry);
  const relative = mode === "after" || mode === "before";
  const collections = useLibraryCollections(library.key, relative);
  const chosen = encodeChoice(entry);
  // Only rows that actually BUILD in this library. A row with nothing here has no position to be
  // relative to, so offering it would save cleanly and then be skipped every run, for good, with
  // nothing on screen saying why.
  const candidates = otherRows.filter((row) =>
    targetsLibrary(library, row.libraryKeys, row.media),
  );

  const setMode = (next: Mode) => {
    if (next === "default") return onChange(undefined);
    if (next === "top") return onChange({ top: true });
    // Keep whichever anchor is already chosen when only flipping after/before.
    onChange({
      anchor: entry?.anchor ?? "",
      row: entry?.row ?? "",
      before: next === "before",
    });
  };

  const setAnchor = (value: string) => {
    const before = mode === "before";
    if (value.startsWith("row:"))
      onChange({ row: value.slice(4), anchor: "", before });
    else onChange({ anchor: value.slice(5), row: "", before });
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
            <option value="after">Right after…</option>
            <option value="before">Right before…</option>
          </select>
        </div>
        {relative && (
          <div className="space-y-1">
            <Label htmlFor={`row-anchor-${library.key}`}>
              {mode === "before" ? "Before" : "After"}
            </Label>
            {collections.isError ? (
              <p className="text-sm text-destructive-text">
                Couldn’t load this library’s collections.
              </p>
            ) : (
              <select
                id={`row-anchor-${library.key}`}
                className={selectClass + " w-64"}
                disabled={collections.isPending}
                value={chosen}
                onChange={(event) => setAnchor(event.target.value)}
              >
                <option value="" disabled>
                  {collections.isPending ? "Loading…" : "Choose one"}
                </option>
                {/* A saved anchor that no longer exists still shows, so the setting reads truthfully
                    rather than silently appearing unset. */}
                {entry?.row && !candidates.some((r) => r.slug === entry.row) && (
                  <option value={`row:${entry.row}`}>
                    {entry.row} (row not found)
                  </option>
                )}
                {entry?.anchor &&
                  !collections.data?.some((c) => c.title === entry.anchor) && (
                    <option value={`coll:${entry.anchor}`}>
                      {entry.anchor} (not found)
                    </option>
                  )}
                {candidates.length > 0 && (
                  <optgroup label="Your Shortlist rows">
                    {candidates.map((row) => (
                      <option key={row.slug} value={`row:${row.slug}`}>
                        {row.name}
                      </option>
                    ))}
                  </optgroup>
                )}
                <optgroup label="Collections in this library">
                  {collections.data?.map((c) => (
                    <option key={c.title} value={`coll:${c.title}`}>
                      {c.title}
                    </option>
                  ))}
                </optgroup>
              </select>
            )}
          </div>
        )}
      </div>
      {relative && !chosen && (
        <p className="text-sm text-muted-foreground">
          Pick a row or collection to sit {mode === "before" ? "before" : "after"}, or nothing moves.
        </p>
      )}
      {rowSlug && entry?.row === rowSlug && (
        <p className="text-sm text-destructive-text">
          A row can’t be positioned relative to itself.
        </p>
      )}
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
  /** The row being edited — excluded from the list of rows it can be positioned against, since a row
   *  cannot follow itself (issue #81). Undefined while creating: an unsaved row has no slug yet. */
  rowSlug?: string;
  pinnedTop?: boolean;
  onConsumePin?: () => void;
  onChange: (next: HubAnchorMap) => void;
}) {
  const libraries = useLibraries();
  const rows = useCollections();
  const migrated = useRef(false);
  // Every OTHER row is a candidate anchor. Named by row rather than by collection title because a
  // per-person row is one Plex collection per person — a title names one account's copy (issue #81).
  const otherRows = (rows.data ?? [])
    .filter((row) => row.slug !== rowSlug)
    .map((row) => ({
      slug: row.slug,
      name: row.name,
      media: row.media,
      libraryKeys: row.library_keys ?? [],
    }));

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
                otherRows={otherRows}
                onChange={(entry) => setLibrary(library.key, entry)}
              />
            ))}
          </div>
        );
      }}
    </QueryBoundary>
  );
}
