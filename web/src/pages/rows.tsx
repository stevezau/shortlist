import { Plus, Rows3, X } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { hasRowNameToken, RowCard } from "@/components/rows/row-card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCollections, useUsers } from "@/lib/queries";
import type { Collection } from "@/lib/types";

function RowsSkeleton() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 3 }, (_, i) => (
        <Skeleton key={i} className="h-20 w-full" />
      ))}
    </div>
  );
}

const CHIP_LEGEND_KEY = "shortlist.rows.chip-legend-dismissed";

function chipLegendDismissed(): boolean {
  try {
    return localStorage.getItem(CHIP_LEGEND_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * What the little grey chip inside a row's name is.
 *
 * A row name is a template, and the card marks each `{placeholder}` as a chip rather than printing
 * the braces — but nothing said what a chip WAS, so "✨ [library name] Picked for You" read as a
 * stray tag someone had attached to the row. The row editor answers this with a worked example
 * ("ON PLEX IT READS ✨ Movies Picked for You — Example only…"); this is that example, once, as a
 * note under the page header, and only when a row on screen actually has a chip in it.
 */
function RowNameChipLegend({ rows }: { rows: Collection[] }) {
  // Shown until it is dismissed once: a permanent card above the list explained the same thing on
  // every visit. Storage can be blocked, in which case it simply shows each time.
  const [dismissed, setDismissed] = useState(chipLegendDismissed);
  if (dismissed || !rows.some((row) => hasRowNameToken(row.name))) return null;
  const dismiss = () => {
    setDismissed(true);
    try {
      localStorage.setItem(CHIP_LEGEND_KEY, "1");
    } catch {
      // Nothing to remember it in; it stays dismissed for this visit.
    }
  };
  // Hedged, because this renders for ANY of the three tokens but can only show one example: a row
  // named "🎯 Because you watched {top_seed}" on a library called "4K Films" would otherwise be
  // told it reads "✨ Movies Picked for You", which is true of neither half.
  return (
    <div className="flex items-start gap-3 rounded-md border bg-elevated px-3 py-2">
      <p className="min-w-0 flex-1 text-sm text-muted-foreground">
        Grey chips like{" "}
        <span className="rounded bg-muted px-1 py-0.5 font-normal">
          library name
        </span>{" "}
        are placeholders, filled in when Shortlist builds the row. For example,
        ✨{" "}
        <span className="rounded bg-muted px-1 py-0.5 font-normal">
          library name
        </span>{" "}
        Picked for You shows on Plex as{" "}
        <span className="text-foreground">✨ Movies Picked for You</span>. A
        person&rsquo;s name or a recent watch fills in the same way.
      </p>
      <Button type="button" variant="ghost" size="sm" onClick={dismiss} aria-label="Dismiss this note">
        <X aria-hidden="true" />
        Got it
      </Button>
    </div>
  );
}

export function RowsPage() {
  const collectionsQuery = useCollections();
  const usersQuery = useUsers();
  const navigate = useNavigate();
  // Adding starts on its own page: pick a kind, name it, choose who gets it.
  const addRow = () => navigate("/rows/new");

  return (
    <div>
      <PageHeader
        title="Rows"
        subtitle="The strips Shortlist builds on your users’ Plex home screens."
        actions={
          <Button
            onClick={addRow}
            // Without the user list, the editor's audience picker would offer nobody to choose —
            // and an owner could save "chosen people: none" believing they'd picked everyone.
            disabled={!usersQuery.isSuccess}
          >
            <Plus aria-hidden="true" />
            Add a row
          </Button>
        }
      />

      {/* Every row's audience is a statement about PEOPLE. A failed users query used to collapse to
          `[] `, which turned "Sarah & Mike" into "No one yet" on a row that really does reach them.
          Nothing here renders until we actually know who the users are. */}
      <QueryBoundary query={usersQuery} skeleton={<RowsSkeleton />}>
        {(users) => (
          <>
            <QueryBoundary
              query={collectionsQuery}
              skeleton={<RowsSkeleton />}
              isEmpty={(rows) => rows.length === 0}
              empty={
                <EmptyState
                  icon={Rows3}
                  title="No rows yet"
                  hint="Add a row to start building recommendations. The default “Picked for You” usually seeds itself."
                  action={
                    // Outline: the header's "Add a row" is already this screen's one primary.
                    <Button variant="outline" onClick={addRow}>
                      Add a row
                    </Button>
                  }
                />
              }
            >
              {(rows) => (
                <div className="space-y-3">
                  <RowNameChipLegend rows={rows} />
                  {rows.map((collection) => (
                    <RowCard
                      key={collection.id}
                      collection={collection}
                      users={users}
                      onEdit={() => navigate(`/rows/${collection.id}`)}
                    />
                  ))}
                </div>
              )}
            </QueryBoundary>
          </>
        )}
      </QueryBoundary>
    </div>
  );
}
