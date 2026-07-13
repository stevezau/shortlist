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

export function RowsPage() {
  const collectionsQuery = useCollections();
  const usersQuery = useUsers();
  const users = usersQuery.data ?? [];
  // null = closed; { collection } = editing (collection null = adding).
  const [editing, setEditing] = useState<{
    collection: Collection | null;
  } | null>(null);

  return (
    <div>
      <PageHeader
        icon={Rows3}
        title="Rows"
        subtitle="The curated strips Rowarr builds on your users’ Plex home screens."
        actions={
          <Button onClick={() => setEditing({ collection: null })}>
            Add a row
          </Button>
        }
      />

      <QueryBoundary
        query={collectionsQuery}
        skeleton={
          <div className="space-y-3">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-20 w-full" />
            ))}
          </div>
        }
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
        />
      )}
    </div>
  );
}
