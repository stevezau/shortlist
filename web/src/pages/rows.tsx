import { Rows3 } from "lucide-react";
import { useState } from "react";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { RowCard } from "@/components/rows/row-card";
import { RowEditor } from "@/components/rows/row-editor";
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

export function RowsPage() {
  const collectionsQuery = useCollections();
  const usersQuery = useUsers();
  // null = closed; { collection } = editing (collection null = adding).
  const [editing, setEditing] = useState<{
    collection: Collection | null;
  } | null>(null);
  // When set, the matching RowCard opens its rename dialog on mount.
  const [renameTarget, setRenameTarget] = useState<number | null>(null);

  return (
    <div>
      <PageHeader
        icon={Rows3}
        title="Rows"
        subtitle="The curated strips Shortlist builds on your users’ Plex home screens. Each row picks its own recommendation sources, AI style, libraries, size and audience."
        actions={
          <Button
            onClick={() => setEditing({ collection: null })}
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
                    <Button onClick={() => setEditing({ collection: null })}>
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
                      onEdit={() => setEditing({ collection })}
                      openRename={renameTarget === collection.id}
                      onRenameOpened={() => setRenameTarget(null)}
                    />
                  ))}
                </div>
              )}
            </QueryBoundary>

            {editing && (
              <RowEditor
                collection={editing.collection}
                users={users}
                onClose={() => setEditing(null)}
                onRename={() => {
                  const id = editing.collection?.id;
                  setEditing(null);
                  if (id) setRenameTarget(id);
                }}
              />
            )}
          </>
        )}
      </QueryBoundary>
    </div>
  );
}
