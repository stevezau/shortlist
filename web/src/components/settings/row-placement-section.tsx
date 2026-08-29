import { useState } from "react";

import { QueryBoundary } from "@/components/query-boundary";
import { SaveStatus } from "@/components/save-status";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useAutosavedSettings } from "@/lib/autosave";
import { settingBool } from "@/lib/format";
import { useLibraries, useLibraryCollections } from "@/lib/queries";
import type { PlexLibrary, Settings } from "@/lib/types";

const selectClass =
  "h-9 w-full rounded-md border bg-elevated px-3 text-sm focus-visible:outline-none " +
  "focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60";

type Anchor = { anchor?: string; before?: boolean; top?: boolean };
type AnchorMap = Record<string, Anchor>;

function readAnchors(settings: Settings): AnchorMap {
  const raw = settings["rows.hub_anchor"];
  if (!raw || typeof raw !== "object") return {};
  const out: AnchorMap = {};
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!value || typeof value !== "object") continue;
    const entry = value as Anchor;
    if (entry.top) out[key] = { top: true };
    else if (typeof entry.anchor === "string" && entry.anchor)
      out[key] = { anchor: entry.anchor, before: Boolean(entry.before) };
  }
  return out;
}

/** The mode a single library is in. An entry exists the moment a non-default mode is chosen (even
 *  before a collection is picked), so an empty-anchor entry still reads as after/before, not default. */
function modeOf(
  entry: Anchor | undefined,
): "default" | "top" | "after" | "before" {
  if (!entry) return "default";
  if (entry.top) return "top";
  return entry.before ? "before" : "after";
}

/** One library's placement control: a mode select plus, when anchored, a collection dropdown. */
function LibraryPlacement({
  library,
  entry,
  onChange,
}: {
  library: PlexLibrary;
  entry: Anchor | undefined;
  onChange: (next: Anchor | undefined) => void;
}) {
  const mode = modeOf(entry);
  const relative = mode === "after" || mode === "before";
  const collections = useLibraryCollections(library.key, relative);
  // Only collections actually on a Plex shelf can anchor anything — one promoted nowhere
  // has no position to sit after, and following it buries the row (issue #106). Off-shelf ones are
  // shown unselectable rather than hidden, so a saved anchor never silently disappears.
  const onShelf = (collections.data ?? []).filter((c) => c.on_shelf);
  const offShelf = (collections.data ?? []).filter((c) => !c.on_shelf);
  const anchorOffShelf = offShelf.some((c) => c.title === entry?.anchor);

  const setMode = (next: "default" | "top" | "after" | "before") => {
    if (next === "default") return onChange(undefined);
    if (next === "top") return onChange({ top: true });
    // Keep the chosen collection when only flipping after/before; else start unset.
    onChange({ anchor: entry?.anchor ?? "", before: next === "before" });
  };

  return (
    <div className="space-y-2 rounded-md border p-3">
      <p className="font-medium">{library.title}</p>
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label htmlFor={`mode-${library.key}`}>Place Shortlist rows</Label>
          <select
            id={`mode-${library.key}`}
            className={selectClass + " w-48"}
            value={mode}
            onChange={(event) =>
              setMode(
                event.target.value as "default" | "top" | "after" | "before",
              )
            }
          >
            <option value="default">Wherever Plex puts them</option>
            <option value="top">Top of the shelf</option>
            <option value="after">Right after a collection…</option>
            <option value="before">Right before a collection…</option>
          </select>
        </div>

        {relative && (
          <div className="space-y-1">
            <Label htmlFor={`anchor-${library.key}`}>Collection</Label>
            {collections.isError ? (
              <p className="text-sm text-destructive-text">
                Couldn’t load this library’s collections.
              </p>
            ) : (
              <select
                id={`anchor-${library.key}`}
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
                {/* A previously-saved anchor that no longer exists still shows, so the setting reads truthfully. */}
                {entry?.anchor &&
                  !collections.data?.some((c) => c.title === entry.anchor) && (
                    <option value={entry.anchor}>
                      {entry.anchor} (not found)
                    </option>
                  )}
                {onShelf.length > 0 && (
                  <optgroup label="On a Plex shelf">
                    {onShelf.map((c) => (
                      <option key={c.title} value={c.title}>
                        {c.title}
                      </option>
                    ))}
                  </optgroup>
                )}
                {offShelf.length > 0 && (
                  <optgroup label="Not on a Plex shelf — can’t be used">
                    {offShelf.map((c) => (
                      <option key={c.title} value={c.title} disabled>
                        {c.title}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            )}
          </div>
        )}
      </div>
      {relative && !entry?.anchor && (
        <p className="text-sm text-muted-foreground">
          Pick a collection to anchor to, or nothing changes.
        </p>
      )}
      {anchorOffShelf && (
        <p className="text-sm text-destructive-text">
          “{entry?.anchor}” isn’t on any of this library’s Plex shelves, so there’s
          no position to anchor to. Turn it on in Plex (the library’s Manage
          Recommendations screen) or choose something else — until then these
          rows stay where they are.
        </p>
      )}
    </div>
  );
}

/** Where Shortlist's rows land in each library's Plex "Recommended" shelf — anchored to a collection
 *  you choose, re-applied every run so a co-managing tool (Kometa) can't bury them again. */
export function RowPlacementSection({ settings }: { settings: Settings }) {
  const librariesQuery = useLibraries();
  const [anchors, setAnchors] = useState<AnchorMap>(() =>
    readAnchors(settings),
  );
  const [manageOrder, setManageOrder] = useState<boolean>(() =>
    settingBool(settings, "rows.manage_shelf_order", true),
  );

  // Only anchors with a real collection are persisted — a half-set library (mode chosen, no
  // collection yet) is dropped so the engine never tries to anchor to "".
  const persistable = Object.fromEntries(
    Object.entries(anchors).filter(([, a]) => a.top || (a.anchor ?? "").trim()),
  );

  const save = useAutosavedSettings({ anchors, manageOrder }, () => ({
    "rows.hub_anchor": persistable,
    "rows.manage_shelf_order": manageOrder,
  }));

  const update = (key: string, next: Anchor | undefined) =>
    setAnchors((current) => {
      const copy = { ...current };
      if (next) copy[key] = next;
      else delete copy[key];
      return copy;
    });

  return (
    <section aria-labelledby="placement-heading" className="space-y-3">
      <h2 id="placement-heading" className="text-lg font-semibold">
        Row placement
      </h2>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-1">
              <p className="font-medium">
                Let Shortlist order the Recommended shelf
              </p>
              <p className="text-sm text-muted-foreground">
                Turn it <strong className="text-foreground">off</strong> if
                another tool (Kometa, Agregarr) orders that shelf, and Shortlist
                will leave it alone. Rows are still built, delivered and kept
                private either way.
              </p>
              {/* The same advice is in the "something is reordering your shelf" notification, but
                  that one only fires while this switch is ON — and turning it off is the fix it
                  recommends. Owners who take that advice would never hear this otherwise. */}
              <p className="text-sm text-muted-foreground">
                If it’s <strong className="text-foreground">Agregarr</strong>,
                check which one you run. The original is no longer actively
                released, and reordering a shelf on it re-promotes collections
                with Plex’s defaults — which puts other people’s rows on{" "}
                <em>your</em> Home, the one place no share filter can cover.
                Shortlist clears that on every run, so it’s a gap between runs
                rather than something permanent. The maintained fork at{" "}
                <a
                  href="https://github.com/bitr8/agregarr-dev"
                  target="_blank"
                  rel="noreferrer"
                  className="underline underline-offset-2 hover:text-foreground"
                >
                  bitr8/agregarr-dev
                </a>{" "}
                (Docker: <code>bitr8/agregarr</code>) fixes it at the source and
                is a drop-in swap.
              </p>
            </div>
            <Switch
              checked={manageOrder}
              onCheckedChange={setManageOrder}
              aria-label="Let Shortlist order the Recommended shelf"
            />
          </div>

          {!manageOrder ? (
            <p className="rounded-md border border-dashed bg-muted/30 p-3 text-sm text-muted-foreground">
              Shelf ordering is off — Shortlist won’t touch the Recommended
              shelf order. Turn it on to position your rows.
            </p>
          ) : (
            <>
              <p className="border-t pt-4 text-sm text-muted-foreground">
                Where Shortlist’s rows sit within each library’s{" "}
                <em>Recommended</em> shelf. By default Plex drops new rows at
                the end, so pin a row just after — or before — a collection you
                choose, and it stays there on every run.
              </p>

              <QueryBoundary
                query={librariesQuery}
                skeleton={<Skeleton className="h-24 w-full" />}
              >
                {(libraries) =>
                  libraries.length === 0 ? (
                    <p className="text-sm text-muted-foreground">
                      No Plex libraries found.
                    </p>
                  ) : (
                    <div className="space-y-3">
                      {libraries.map((library) => (
                        <LibraryPlacement
                          key={library.key}
                          library={library}
                          entry={anchors[library.key]}
                          onChange={(next) => update(library.key, next)}
                        />
                      ))}
                    </div>
                  )
                }
              </QueryBoundary>
            </>
          )}

          <SaveStatus
            isPending={save.isPending}
            isError={save.isError}
            error={save.error}
            saved={save.saved}
            onRetry={save.retry}
          />
        </CardContent>
      </Card>
    </section>
  );
}
