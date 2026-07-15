import { useMutation } from "@tanstack/react-query";
import { Eraser, Trash2, UserCheck, Users as UsersIcon } from "lucide-react";
import { useState } from "react";

import { MutationAlert } from "@/components/mutation-alert";
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
import { api, apiErrorMessage } from "@/lib/api";
import { audienceSummary, rowOverrides, toInput } from "@/lib/collections";
import { DEFAULT_ROW_SLUG } from "@/lib/constants";
import { settingString } from "@/lib/format";
import {
  useDeleteCollection,
  useLibraries,
  useSaveCollection,
  useSettings,
} from "@/lib/queries";
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
  const settings = useSettings();
  const libraries = useLibraries();
  const isDefault = collection.slug === DEFAULT_ROW_SLUG;
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  // A dry-run first (what WOULD be removed), then the real removal on confirm.
  const preview = useMutation({
    mutationFn: () => api.cleanupCollection(collection.id, true),
  });
  const cleanup = useMutation({
    mutationFn: () => api.cleanupCollection(collection.id, false),
  });
  // null until the library list actually arrives — a half-loaded card must not label a row's
  // libraries with raw Plex section keys, which mean nothing to the owner.
  const overrides = rowOverrides(
    collection,
    libraries.isSuccess ? libraries.data : null,
  );

  // The default row's size is delivered from Settings → Defaults, not its own column (which the
  // backend ignores). Show the effective value so the card can't advertise a size no user gets.
  const globalSize = Number(settingString(settings.data ?? {}, "row.size"));
  const effectiveSize =
    isDefault && Number.isFinite(globalSize) && globalSize > 0
      ? globalSize
      : collection.size;

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
            {audienceSummary(collection, users)} · {effectiveSize} titles ·{" "}
            {collection.media === "both"
              ? "movies & shows"
              : `${collection.media}s`}
          </p>
          {overrides.length > 0 && (
            <div className="flex flex-wrap gap-1 pt-0.5">
              {overrides.map((part) => (
                <Badge key={part} variant="outline" className="font-normal">
                  {part}
                </Badge>
              ))}
            </div>
          )}
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
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              cleanup.reset();
              preview.reset();
              setCleanupOpen(true);
              preview.mutate();
            }}
            aria-label={`Remove ${collection.name} from Plex`}
            title="Remove from Plex"
          >
            <Eraser aria-hidden="true" />
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
        {/* The Switch mirrors the saved row, so a rejected save just snaps it back — silently
            reverting is exactly what a click that never landed looks like. */}
        {save.isError && (
          <MutationAlert
            className="w-full"
            error={save.error}
            lead={
              collection.enabled
                ? "This row is still on."
                : "This row is still off."
            }
            fallback="Couldn’t change this row. Try again."
            onRetry={() => {
              const last = save.variables;
              if (last) save.mutate(last);
            }}
          />
        )}

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

      <Dialog
        open={cleanupOpen}
        onOpenChange={(open) => {
          setCleanupOpen(open);
          if (!open) {
            preview.reset();
            cleanup.reset();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove “{collection.name}” from Plex?</DialogTitle>
            <DialogDescription>
              Deletes this row’s collections from Plex for everyone who has it.
              The titles stay in your library and the row’s settings here are
              kept — it’ll be rebuilt on the next run unless you also turn it
              off or delete it.
            </DialogDescription>
          </DialogHeader>
          {preview.isPending && (
            <p className="text-sm text-muted-foreground">Checking Plex…</p>
          )}
          {preview.isSuccess && !cleanup.isSuccess && (
            <p className="text-sm">
              {preview.data.removed.length === 0
                ? "Nothing to remove — this row has no collections on Plex right now."
                : `This will remove ${preview.data.removed.length} collection${
                    preview.data.removed.length === 1 ? "" : "s"
                  } from Plex.`}
            </p>
          )}
          {cleanup.isSuccess && (
            <p role="status" className="text-sm text-success">
              {cleanup.data.message}
            </p>
          )}
          {(preview.isError || cleanup.isError) && (
            <p role="alert" className="text-sm text-destructive">
              {apiErrorMessage(
                preview.error ?? cleanup.error,
                "Couldn’t reach Plex. Try again.",
              )}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setCleanupOpen(false)}>
              {cleanup.isSuccess ? "Close" : "Cancel"}
            </Button>
            {!cleanup.isSuccess && (
              <Button
                variant="destructive"
                loading={cleanup.isPending}
                disabled={
                  preview.isPending ||
                  (preview.isSuccess && preview.data.removed.length === 0)
                }
                onClick={() => cleanup.mutate()}
              >
                Remove from Plex
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
