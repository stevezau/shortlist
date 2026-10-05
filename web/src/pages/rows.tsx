import { Plus, Rows3 } from "lucide-react";
import { useRef, useState } from "react";
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
 * ("ON PLEX IT READS ✨ Movies Picked for You — Example only…"); this is that example, once, as a
 * note under the page header, and only when a row on screen actually has a chip in it.
 */
function RowNameChipLegend({ rows }: { rows: Collection[] }) {
  if (!rows.some((row) => hasRowNameToken(row.name))) return null;
  // Hedged, because this renders for ANY of the three tokens but can only show one example: a row
  // named "🎯 Because you watched {top_seed}" on a library called "4K Films" would otherwise be
  // told it reads "✨ Movies Picked for You", which is true of neither half.
  return (
    <p className="rounded-md border bg-elevated px-3 py-2 text-sm text-muted-foreground">
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
  );
}

export function RowsPage() {
  const collectionsQuery = useCollections();
  const usersQuery = useUsers();
  const navigate = useNavigate();
  // Adding goes through the gallery first — a blank 17-field form only ever helped someone who
  // already knew what they wanted to build.
  const templateTrigger = useRef<HTMLButtonElement | null>(null);
  const [pickingTemplate, setPickingTemplate] = useState(false);

  return (
    <div>
      <PageHeader
        title="Rows"
        subtitle="The strips Shortlist builds on your users’ Plex home screens."
        actions={
          <Button
            onClick={(event) => { templateTrigger.current = event.currentTarget; setPickingTemplate(true); }}
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
                    <Button variant="outline" onClick={(event) => { templateTrigger.current = event.currentTarget; setPickingTemplate(true); }}>
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

            <RowTemplateGallery
              open={pickingTemplate}
              onClose={() => setPickingTemplate(false)}
              onReturnFocus={() => templateTrigger.current?.focus()}
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
