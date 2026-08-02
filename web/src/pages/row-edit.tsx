import { useNavigate, useParams, useSearchParams } from "react-router";

import { BackLink } from "@/components/back-link";
import { QueryBoundary } from "@/components/query-boundary";
import { RowEditor } from "@/components/rows/row-editor";
import { Skeleton } from "@/components/ui/skeleton";
import { findRowTemplate } from "@/lib/row-templates";
import { useCollections, useUsers } from "@/lib/queries";

/**
 * The add/edit-a-row screen.
 *
 * `/rows/new?template=<id>` to add, `/rows/:id` to edit. A page rather than the dialog it used to
 * be: a modal is capped at 90% of the viewport, and that cap — not the number of settings — is what
 * forced every group of settings into a collapsed accordion, which in turn hid the warnings that
 * only matter before you save. A page also gives the outcome preview somewhere to live, and makes a
 * single setting linkable from anywhere else in the app.
 */
export function RowEditPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const collections = useCollections();
  const users = useUsers();

  const isNew = id === undefined;
  const template = isNew
    ? (findRowTemplate(params.get("template") ?? "") ?? null)
    : null;

  return (
    <div className="space-y-4">
      <BackLink to="/rows" label="Rows" />
      {/* Only the collections list gates rendering. The users list feeds the audience picker and the
          reach warning, and an empty one degrades to "no audience detail" rather than blocking the
          editor — someone adding their first row has no users synced yet. */}
      <QueryBoundary
        query={collections}
        skeleton={<Skeleton className="h-96 w-full" />}
      >
        {(rows) => {
          const collection = isNew
            ? null
            : (rows.find((row) => row.id === Number(id)) ?? null);
          if (!isNew && !collection) {
            return (
              <p className="text-muted-foreground">
                That row no longer exists. It may have been deleted.
              </p>
            );
          }
          return (
            <RowEditor
              collection={collection}
              template={template}
              users={users.data ?? []}
              onClose={() => navigate("/rows")}
              onRename={(proposedName) =>
                collection &&
                navigate(`/rows/${collection.id}/rename`, {
                  // Only the proposed name — deliberately NOT `oldTemplate`, which is what makes
                  // that screen auto-start. Arriving from here still asks before touching Plex.
                  state: { proposedName },
                })
              }
            />
          );
        }}
      </QueryBoundary>
    </div>
  );
}
