import { Trash2, UserCheck, Users as UsersIcon } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { apiErrorMessage } from "@/lib/api";
import { audienceSummary, toInput } from "@/lib/collections";
import { useDeleteCollection, useSaveCollection } from "@/lib/queries";
import type { Collection, User } from "@/lib/types";
import { cn } from "@/lib/utils";

/** One row in the Rows list: its audience/size summary, an enable toggle, edit, and delete. */
export function RowCard({
  collection,
  users,
  onEdit,
}: {
  collection: Collection;
  users: User[];
  onEdit: () => void;
}) {
  const save = useSaveCollection();
  const remove = useDeleteCollection();
  const isDefault = collection.slug === "picked";
  const [confirmOpen, setConfirmOpen] = useState(false);

  return (
    <Card className={cn(!collection.enabled && "opacity-60")}>
      <CardContent className="flex flex-wrap items-center justify-between gap-4 pt-6">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{collection.name}</span>
            <Badge
              variant={collection.build === "shared" ? "warning" : "secondary"}
            >
              {collection.build === "shared" ? (
                <UsersIcon className="h-3 w-3" aria-hidden="true" />
              ) : (
                <UserCheck className="h-3 w-3" aria-hidden="true" />
              )}
              {collection.build === "shared" ? "Shared" : "Per person"}
            </Badge>
            {isDefault && <Badge variant="outline">default</Badge>}
          </div>
          <p className="text-sm text-muted-foreground">
            {audienceSummary(collection, users)} · {collection.size} titles ·{" "}
            {collection.media === "both"
              ? "movies & shows"
              : `${collection.media}s`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Switch
            checked={collection.enabled}
            onCheckedChange={(enabled) =>
              save.mutate({
                id: collection.id,
                body: { ...toInput(collection), enabled },
              })
            }
            aria-label={`Enable ${collection.name}`}
          />
          <Button variant="outline" size="sm" onClick={onEdit}>
            Edit
          </Button>
          {!isDefault && (
            <Button
              variant="ghost"
              size="sm"
              loading={remove.isPending}
              onClick={() => setConfirmOpen(true)}
              aria-label={`Delete ${collection.name}`}
            >
              {!remove.isPending && <Trash2 aria-hidden="true" />}
            </Button>
          )}
        </div>
        {remove.isError && (
          <p role="alert" className="w-full text-sm text-destructive">
            {apiErrorMessage(
              remove.error,
              "Couldn’t delete this row. Try again.",
            )}
          </p>
        )}
      </CardContent>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete “{collection.name}”?</DialogTitle>
            <DialogDescription>
              This removes the row and its Plex collections on the next run. The
              titles themselves stay in your library. This can’t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              loading={remove.isPending}
              onClick={() =>
                remove.mutate(collection.id, {
                  onSuccess: () => setConfirmOpen(false),
                })
              }
            >
              Delete row
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
