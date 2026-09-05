import { Rows3 } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { hasRowNameToken, RowCard } from "@/components/rows/row-card";
import { RowTemplateGallery } from "@/components/rows/row-template-gallery";
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

/**
 * What the little grey chip inside a row's name is.
 *
 * A row name is a template, and the card marks each `{placeholder}` as a chip rather than printing
 * the braces — but nothing said what a chip WAS, so "✨ [library name] Picked for You" read as a
 * stray tag someone had attached to the row. The row editor answers this with a worked example
 * ("ON PLEX IT READS ✨ Movies Picked for You — Example only…"); this is that example, once, under
 * the list, and only when a row on screen actually has a chip in it.
 */
function RowNameChipLegend({ rows }: { rows: Collection[] }) {
  if (!rows.some((row) => hasRowNameToken(row.name))) return null;
  return (
    <p className="px-1 pt-1 text-xs text-muted-foreground">
      A chip like{" "}
      <span className="rounded bg-muted px-1 py-0.5 font-normal">
        library name
      </span>{" "}
      is a placeholder, filled in when the row is built &mdash; so on Plex that
      row reads{" "}
      <span className="text-foreground">✨ Movies Picked for You</span>.
    </p>
  );
}

export function RowsPage() {
  const collectionsQuery = useCollections();
  const usersQuery = useUsers();
  const navigate = useNavigate();
  // Adding goes through the gallery first — a blank 17-field form only ever helped someone who
  // already knew what they wanted to build.
  const [pickingTemplate, setPickingTemplate] = useState(false);
  // When set, the matching RowCard opens its rename dialog on mount.

  return (
    <div>
      <PageHeader
        icon={Rows3}
        title="Rows"
        subtitle="The strips Shortlist builds on your users’ Plex home screens."
        actions={
          <Button
            onClick={() => setPickingTemplate(true)}
            // Without the user list, the editor's audience picker would offer nobody to choose —
            // and an owner could save "chosen people: none" believing they'd picked everyone.
            disabled={!usersQuery.isSuccess}
          >
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
                    <Button onClick={() => setPickingTemplate(true)}>
                      Add a row
                    </Button>
                  }
                />
              }
            >
              {(rows) => (
                <div className="space-y-3">
                  {rows.map((collection) => (
                    <RowCard
                      key={collection.id}
                      collection={collection}
                      users={users}
                      onEdit={() => navigate(`/rows/${collection.id}`)}
                    />
                  ))}
                  <RowNameChipLegend rows={rows} />
                </div>
              )}
            </QueryBoundary>

            <RowTemplateGallery
              open={pickingTemplate}
              onClose={() => setPickingTemplate(false)}
              onPick={(template) => {
                setPickingTemplate(false);
                // null = "start from scratch" — the gallery's last tile.
                navigate(
                  template ? `/rows/new?template=${template.id}` : "/rows/new",
                );
              }}
            />
          </>
        )}
      </QueryBoundary>
    </div>
  );
}
