import { Rows3, Trash2, UserCheck, Users as UsersIcon } from "lucide-react";
import { useState } from "react";

import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { UserAvatar } from "@/components/user-avatar";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ApiError } from "@/lib/api";
import {
  useCollections,
  useDeleteCollection,
  useSaveCollection,
  useUsers,
} from "@/lib/queries";
import type { Collection, CollectionInput, User } from "@/lib/types";
import { cn } from "@/lib/utils";

const ROW_SIZES = [10, 15, 20];
const MEDIA: { value: CollectionInput["media"]; label: string }[] = [
  { value: "both", label: "Movies & Shows" },
  { value: "movie", label: "Movies only" },
  { value: "show", label: "Shows only" },
];

function blankInput(): CollectionInput {
  return {
    name: "",
    build: "per_person",
    audience: "everyone",
    audience_user_ids: [],
    enabled: true,
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    min_watchers: 2,
    prompt: { tone: "balanced", guidance: "", template: "" },
  };
}

function toInput(collection: Collection): CollectionInput {
  return {
    name: collection.name,
    build: collection.build,
    audience: collection.audience,
    audience_user_ids: collection.audience_user_ids,
    enabled: collection.enabled,
    size: collection.size,
    media: collection.media,
    sort_order: collection.sort_order,
    name_template: collection.name_template,
    min_watchers: collection.min_watchers,
    prompt: {
      tone: collection.prompt.tone ?? "balanced",
      guidance: collection.prompt.guidance ?? "",
      template: collection.prompt.template ?? "",
    },
  };
}

function audienceSummary(collection: Collection, users: User[]): string {
  if (collection.audience === "everyone") return "Everyone";
  const names = collection.audience_user_ids
    .map((id) => users.find((u) => u.id === id)?.username)
    .filter(Boolean);
  if (names.length === 0) return "No one yet";
  return names.length <= 2
    ? names.join(" & ")
    : `${names.slice(0, 2).join(", ")} +${names.length - 2}`;
}

function RowEditor({
  collection,
  users,
  onClose,
}: {
  collection: Collection | null;
  users: User[];
  onClose: () => void;
}) {
  const save = useSaveCollection();
  const [input, setInput] = useState<CollectionInput>(
    collection ? toInput(collection) : blankInput(),
  );
  const isDefault = collection?.slug === "picked";

  const set = (patch: Partial<CollectionInput>) =>
    setInput((prev) => ({ ...prev, ...patch }));

  const curation: CurationStyleValue = {
    tone: input.prompt.tone,
    guidance: input.prompt.guidance,
    template: input.prompt.template,
  };

  const submit = () =>
    save.mutate(
      { id: collection?.id ?? null, body: input },
      { onSuccess: onClose },
    );

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{collection ? "Edit row" : "Add a row"}</DialogTitle>
          <DialogDescription>
            A row is a strip of “Picked for You”-style recommendations on your
            users’ Plex home screens.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5 py-2">
          <div className="space-y-2">
            <Label htmlFor="row-name">Name</Label>
            <Input
              id="row-name"
              value={input.name}
              placeholder="e.g. Hidden Gems"
              onChange={(event) => set({ name: event.target.value })}
            />
          </div>

          <div className="space-y-2">
            <Label>Built how?</Label>
            <Segmented
              value={input.build}
              onChange={(build) => set({ build })}
              options={[
                { value: "per_person", label: "Per person" },
                { value: "shared", label: "Shared" },
              ]}
            />
            <p className="text-sm text-muted-foreground">
              {input.build === "per_person"
                ? "Each chosen person gets their own version, from their own viewing."
                : "One version built from everyone’s viewing, the same for whoever can see it."}
            </p>
          </div>

          <div className="space-y-2">
            <Label>Who gets it?</Label>
            <Segmented
              value={input.audience}
              onChange={(audience) => set({ audience })}
              options={[
                { value: "everyone", label: "Everyone" },
                { value: "subset", label: "Choose people" },
              ]}
            />
            {input.audience === "subset" && (
              <div className="mt-2 space-y-1 rounded-lg border bg-elevated p-2">
                {users.length === 0 && (
                  <p className="p-2 text-sm text-muted-foreground">
                    No users yet.
                  </p>
                )}
                {users.map((user) => {
                  const on = input.audience_user_ids.includes(user.id);
                  return (
                    <label
                      key={user.id}
                      className="flex cursor-pointer items-center justify-between rounded-md px-2 py-1.5 hover:bg-accent"
                    >
                      <span className="flex items-center gap-2 text-sm">
                        <UserAvatar name={user.username} size="sm" />
                        {user.username}
                      </span>
                      <Switch
                        checked={on}
                        onCheckedChange={(checked) =>
                          set({
                            audience_user_ids: checked
                              ? [...input.audience_user_ids, user.id]
                              : input.audience_user_ids.filter(
                                  (id) => id !== user.id,
                                ),
                          })
                        }
                        aria-label={user.username}
                      />
                    </label>
                  );
                })}
              </div>
            )}
          </div>

          <div className="grid gap-5 sm:grid-cols-2">
            <Segmented
              legend="Row size"
              value={String(input.size)}
              onChange={(size) => set({ size: Number(size) })}
              options={ROW_SIZES.map((size) => ({
                value: String(size),
                label: String(size),
              }))}
            />
            <Segmented
              legend="Libraries"
              value={input.media}
              onChange={(media) => set({ media })}
              options={MEDIA}
            />
          </div>

          {input.build === "shared" && (
            <div className="space-y-2">
              <Label htmlFor="min-watchers">
                Only show titles at least this many people watched
              </Label>
              <Input
                id="min-watchers"
                type="number"
                min={2}
                max={50}
                value={input.min_watchers}
                onChange={(event) =>
                  set({
                    min_watchers: Math.max(2, Number(event.target.value) || 2),
                  })
                }
                className="w-24"
              />
              <p className="text-sm text-muted-foreground">
                Keeps one person’s viewing from ever showing up in a shared row.
                2 is a good default.
              </p>
            </div>
          )}

          <div className="space-y-2 border-t pt-4">
            <Label>Curation style</Label>
            <p className="text-sm text-muted-foreground">
              How the AI writes this row.{" "}
              {isDefault &&
                "The default row also uses the global setting under Settings."}
            </p>
            <CurationStyleFields
              value={curation}
              onChange={(next) =>
                set({
                  prompt: {
                    tone: next.tone,
                    guidance: next.guidance,
                    template: next.template,
                  },
                })
              }
            />
          </div>
        </div>

        {save.isError && (
          <p role="alert" className="text-sm text-destructive">
            {save.error instanceof ApiError
              ? save.error.message
              : "Couldn’t save this row. Try again."}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={submit}
            loading={save.isPending}
            disabled={!input.name.trim()}
          >
            {collection ? "Save changes" : "Add row"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RowCard({
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
            {remove.error instanceof ApiError
              ? remove.error.message
              : "Couldn’t delete this row. Try again."}
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
