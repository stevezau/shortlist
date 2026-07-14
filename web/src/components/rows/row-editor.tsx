import { useState } from "react";

import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { AudiencePicker } from "@/components/rows/audience-picker";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
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
import { apiErrorMessage } from "@/lib/api";
import { blankInput, toInput } from "@/lib/collections";
import { ROW_SIZES } from "@/lib/constants";
import { useSaveCollection } from "@/lib/queries";
import type { Collection, CollectionInput, User } from "@/lib/types";

const MEDIA: { value: CollectionInput["media"]; label: string }[] = [
  { value: "both", label: "Movies & Shows" },
  { value: "movie", label: "Movies only" },
  { value: "show", label: "Shows only" },
];

/** The add/edit-a-row dialog. `collection` is null when adding. */
export function RowEditor({
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
              disabled={isDefault}
              onChange={(event) => set({ name: event.target.value })}
            />
            {isDefault && (
              <p className="text-sm text-muted-foreground">
                The default row’s name and size follow Settings → Defaults, so
                they stay in sync everywhere. Change them there.
              </p>
            )}
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

          <AudiencePicker
            audience={input.audience}
            audienceUserIds={input.audience_user_ids}
            users={users}
            onChange={set}
          />

          <div className="grid gap-5 sm:grid-cols-2">
            {!isDefault && (
              <Segmented
                legend="Row size"
                value={String(input.size)}
                onChange={(size) => set({ size: Number(size) })}
                options={ROW_SIZES.map((size) => ({
                  value: String(size),
                  label: String(size),
                }))}
              />
            )}
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

          {input.build !== "shared" && (
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="row-request-tag">Request tag (optional)</Label>
              <Input
                id="row-request-tag"
                value={input.request_tag}
                onChange={(event) => set({ request_tag: event.target.value })}
                placeholder="e.g. picked-for-family"
                maxLength={64}
                className="max-w-xs"
              />
              <p className="text-sm text-muted-foreground">
                When Requests are on, titles asked for anyone in this row’s
                audience get this tag in Sonarr/Radarr — on top of your global
                tag and each person’s own tag. Leave blank for none.
              </p>
            </div>
          )}
        </div>

        {save.isError && (
          <p role="alert" className="text-sm text-destructive">
            {apiErrorMessage(save.error, "Couldn’t save this row. Try again.")}
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
