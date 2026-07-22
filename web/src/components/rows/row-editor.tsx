import { useState } from "react";

import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { SaveStatus } from "@/components/save-status";
import { AudiencePicker } from "@/components/rows/audience-picker";
import { LibraryPicker } from "@/components/rows/library-picker";
import { PosterField } from "@/components/rows/poster-field";
import { RowScheduleField } from "@/components/rows/row-schedule-field";
import { RowShelfPlacement } from "@/components/rows/row-shelf-placement";
import { RowSourcesField } from "@/components/rows/row-sources-field";
import { Segmented } from "@/components/segmented";
import { FreshnessSlider } from "@/components/settings/freshness-slider";
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
import { RowSizeField } from "@/components/row-size-field";
import { apiErrorMessage } from "@/lib/api";
import { useAutosavedSettings } from "@/lib/autosave";
import { blankInput, toInput } from "@/lib/collections";
import { settingString } from "@/lib/format";
import { useSaveCollection, useSettings } from "@/lib/queries";
import type { Collection, CollectionInput, Settings, User } from "@/lib/types";

/**
 * The default row's curation IS the global style (Settings → Curation style), so we edit it in place
 * here, wired straight to those global settings and auto-saved — the same style, the same store, just
 * reachable from the row. Seeded synchronously from the loaded settings so opening it saves nothing.
 */
function DefaultRowCuration({ settings }: { settings: Settings }) {
  const [curation, setCuration] = useState<CurationStyleValue>({
    tone: settingString(settings, "curator.prompt_tone", "balanced"),
    guidance: settingString(settings, "curator.prompt_guidance"),
    template: settingString(settings, "curator.prompt_template"),
  });
  const save = useAutosavedSettings(curation, () => ({
    "curator.prompt_tone": curation.tone,
    "curator.prompt_guidance": curation.guidance,
    "curator.prompt_template": curation.template,
  }));
  return (
    <>
      <p className="text-sm text-muted-foreground">
        The default row uses the global style (Settings → Curation style), so
        edits here apply everywhere — and save automatically.
      </p>
      <CurationStyleFields
        value={curation}
        onChange={setCuration}
        scope="global"
      />
      <SaveStatus
        isPending={save.isPending}
        isError={save.isError}
        error={save.error}
        saved={save.saved}
        onRetry={save.retry}
      />
    </>
  );
}

/** The add/edit-a-row dialog. `collection` is null when adding. */
/**
 * A shared row is built only from titles SEVERAL people have watched, so one whose audience holds
 * fewer enabled people than its threshold can never produce anything — it just reports "skipped"
 * every run. Say so here, where it can still be fixed, rather than leaving someone to read a silent
 * skip as a broken app (issue #3).
 */
function SharedRowReachWarning({
  users,
  audience,
  audienceUserIds,
  minWatchers,
}: {
  users: User[];
  audience: "everyone" | "subset";
  audienceUserIds: number[];
  minWatchers: number;
}) {
  // Unknown user list (still loading) — say nothing rather than cry wolf.
  if (users.length === 0) return null;
  // The engine's audience is enabled AND not paused (a paused user is dropped before any row is
  // built), so counting only `enabled` would stay silent on a row that genuinely cannot build.
  const reach = users.filter(
    (user) =>
      user.enabled &&
      !user.prefs?.paused &&
      (audience === "everyone" || audienceUserIds.includes(user.id)),
  ).length;
  if (reach >= minWatchers) return null;
  return (
    <p
      role="status"
      className="rounded-md border border-warning/40 bg-warning/5 p-3 text-sm"
    >
      This row can’t build yet: it needs {minWatchers} people with viewing in
      common, but{" "}
      {reach === 0
        ? "nobody in its audience is active in runs"
        : `only ${reach} of them ${reach === 1 ? "is" : "are"} active in runs`}{" "}
      (enabled and not paused). Add more people to the audience, or make this a
      per-person row so each of them gets their own.
    </p>
  );
}

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
  const settings = useSettings();
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

  const submit = () => {
    // Keep 'Top' entries and real anchors; drop a half-set library (mode chosen, no collection yet) so
    // it inherits the global default rather than being POSTed as an empty anchor (which the API rejects).
    const hub_anchor = Object.fromEntries(
      Object.entries(input.hub_anchor).filter(
        ([, entry]) => entry.top || (entry.anchor ?? "").trim(),
      ),
    );
    save.mutate(
      { id: collection?.id ?? null, body: { ...input, hub_anchor } },
      { onSuccess: onClose },
    );
  };

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
            {isDefault ? (
              <p className="text-sm text-muted-foreground">
                The default row’s name and size follow Settings → Defaults, so
                they stay in sync everywhere. Change them there.
              </p>
            ) : (
              <p className="text-sm text-muted-foreground">
                Use <span className="font-mono">{"{library_name}"}</span> for
                the library’s name,{" "}
                <span className="font-mono">{"{user}"}</span> for each person’s
                name, or <span className="font-mono">{"{top_seed}"}</span> for
                their top watched title.
              </p>
            )}
          </div>

          <PosterField
            value={input.poster}
            onChange={(poster) => set({ poster })}
            collectionId={collection?.id ?? null}
            hasImage={collection?.poster?.has_image ?? false}
          />

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

          <RowScheduleField
            value={input.schedule}
            onChange={(schedule) => set({ schedule })}
          />

          {!isDefault && (
            <RowSizeField
              value={input.size}
              onChange={(size) => set({ size })}
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
              <SharedRowReachWarning
                users={users}
                audience={input.audience}
                audienceUserIds={input.audience_user_ids}
                minWatchers={input.min_watchers}
              />
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

          <div className="space-y-3 border-t pt-4">
            <Label htmlFor="row-freshness">Freshness</Label>
            <p className="text-sm text-muted-foreground">
              How much this row changes day to day. Leave on the global default
              to follow Settings → Recommendations.
            </p>
            <div className="flex items-center justify-between gap-4">
              <span className="text-sm">Use the global default</span>
              <Switch
                checked={input.freshness === null}
                onCheckedChange={(on) => set({ freshness: on ? null : 0 })}
                aria-label="Use the global freshness default"
              />
            </div>
            {input.freshness !== null && (
              <FreshnessSlider
                id="row-freshness"
                value={Math.round(input.freshness * 100)}
                onChange={(pct) => set({ freshness: pct / 100 })}
              />
            )}
          </div>

          <div className="space-y-3 border-t pt-4">
            <Label htmlFor="row-recent-count">Recent watches to search</Label>
            <p className="text-sm text-muted-foreground">
              How many of a person&rsquo;s most recent watches the AI web-search
              source looks up for this row (one cached search each). Only
              affects rows using AI web search. Leave on the global default to
              follow Settings → Recommendations.
            </p>
            <div className="flex items-center justify-between gap-4">
              <span className="text-sm">Use the global default</span>
              <Switch
                checked={input.recent_count === null}
                onCheckedChange={(on) => set({ recent_count: on ? null : 10 })}
                aria-label="Use the global recent-watches default"
              />
            </div>
            {input.recent_count !== null && (
              <Input
                id="row-recent-count"
                type="number"
                min={1}
                max={25}
                value={input.recent_count}
                onChange={(e) =>
                  set({
                    recent_count: Math.max(
                      1,
                      Math.min(25, Number(e.target.value) || 1),
                    ),
                  })
                }
                className="w-28"
              />
            )}
          </div>

          <div className="space-y-3 border-t pt-4">
            <Label>Where it shows</Label>
            <p className="text-sm text-muted-foreground">
              Which Plex screens this row appears on once it&rsquo;s built.
            </p>
            <Segmented
              value={input.placement}
              onChange={(placement) =>
                set({ placement: placement as CollectionInput["placement"] })
              }
              ariaLabel="Where the row shows"
              options={[
                { value: "both", label: "Home & Library" },
                { value: "home", label: "Home only" },
                { value: "library", label: "Library only" },
              ]}
            />
            <div className="space-y-2 pt-2">
              <span className="text-sm font-medium">
                Position in the Recommended shelf
              </span>
              <p className="text-sm text-muted-foreground">
                Where this row lands. Each library can inherit the global
                default (Settings → Row placement), sit at the{" "}
                <strong>Top</strong>, or anchor right after/before one of your
                collections.
              </p>
              <RowShelfPlacement
                value={input.hub_anchor}
                libraryKeys={input.library_keys}
                media={input.media}
                pinnedTop={input.pin_top}
                onConsumePin={() => set({ pin_top: false })}
                onChange={(hub_anchor) => set({ hub_anchor })}
              />
            </div>
          </div>

          <div className="space-y-2 border-t pt-4">
            <Label>Curation style</Label>
            {isDefault ? (
              // The default row is curated with the GLOBAL recipe (ContextBuilder._build_rows discards
              // any stored on the row), so edit that global recipe in place here — auto-saved to the
              // same settings Settings → Curation style writes, once they've loaded.
              settings.data && <DefaultRowCuration settings={settings.data} />
            ) : (
              <CurationStyleFields
                allowInherit
                scope="row"
                shared={input.build === "shared"}
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
