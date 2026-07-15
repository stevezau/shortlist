import { useState } from "react";

import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { AudiencePicker } from "@/components/rows/audience-picker";
import { LibraryPicker } from "@/components/rows/library-picker";
import { RowSourcesField } from "@/components/rows/row-sources-field";
import { Segmented } from "@/components/segmented";
import { WatchedSlider } from "@/components/settings/watched-slider";
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
import { Switch } from "@/components/ui/switch";
import { apiErrorMessage } from "@/lib/api";
import { blankInput, toInput } from "@/lib/collections";
import { ROW_SIZES } from "@/lib/constants";
import { useSaveCollection } from "@/lib/queries";
import type { Collection, CollectionInput, User } from "@/lib/types";

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
              onChange={(build) =>
                // Shared rows never request missing titles, so a request tag on one is inert —
                // clear it when switching so no orphaned value lingers hidden in the row.
                set({
                  build,
                  ...(build === "shared" ? { request_tag: "" } : {}),
                })
              }
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

          <LibraryPicker
            libraryKeys={input.library_keys}
            onChange={(next) => set(next)}
          />

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

          <RowSourcesField
            value={input.candidate_sources}
            onChange={(candidate_sources) => set({ candidate_sources })}
          />

          <div className="space-y-3 border-t pt-4">
            <Label htmlFor="row-watched-pct">Already-watched titles</Label>
            <p className="text-sm text-muted-foreground">
              How much of this row may be things a person has already finished.
              Leave on the global default to follow Settings → Recommendations.
            </p>
            <div className="flex items-center justify-between gap-4">
              <span className="text-sm">Use the global default</span>
              <Switch
                checked={input.watched_pct === null}
                onCheckedChange={(on) => set({ watched_pct: on ? null : 0 })}
                aria-label="Use the global already-watched default"
              />
            </div>
            {input.watched_pct !== null && (
              <WatchedSlider
                id="row-watched-pct"
                value={Math.round(input.watched_pct * 100)}
                onChange={(pct) => set({ watched_pct: pct / 100 })}
              />
            )}
          </div>

          <div className="space-y-2 border-t pt-4">
            <Label>Curation style</Label>
            {isDefault ? (
              // The server discards this row's stored recipe and curates it with the global one
              // (ContextBuilder._build_rows), so offering the fields here would save a dead setting.
              <p className="text-sm text-muted-foreground">
                The default row&rsquo;s style always comes from Settings →
                Curation style, so it stays in sync everywhere. Change it there.
                Every other row can have its own.
              </p>
            ) : (
              <>
                <p className="text-sm text-muted-foreground">
                  How the AI picks and writes up this row. Leave a field on
                  Inherit/blank to follow Settings → Curation style; set one to
                  give this row a voice of its own.
                </p>
                <CurationStyleFields
                  allowInherit
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
              </>
            )}
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
