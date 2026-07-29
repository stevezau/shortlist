import { useState } from "react";

import { AudiencePicker } from "@/components/rows/audience-picker";
import { LibraryPicker } from "@/components/rows/library-picker";
import { PosterField } from "@/components/rows/poster-field";
import { RowScheduleField } from "@/components/rows/row-schedule-field";
import { RowShelfPlacement } from "@/components/rows/row-shelf-placement";
import { RowSourcesField } from "@/components/rows/row-sources-field";
import { TemplateVarsHintWithPreview } from "@/components/rows/template-vars-hint";
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
import { MaxSeedsField } from "@/components/max-seeds-field";
import { RecentCountField } from "@/components/recent-count-field";
import { RowSizeField } from "@/components/row-size-field";
import { apiErrorMessage } from "@/lib/api";
import { blankInput, toInput } from "@/lib/collections";
import { useSaveCollection } from "@/lib/queries";
import type { Collection, CollectionInput, Placement, User } from "@/lib/types";

/** Decode/encode one audience's placement as its two independent surface flags. All four
 *  combinations are representable — "off" is a real state, not a fallback to Library. */
function hasLibrary(p: Placement): boolean {
  return p === "both" || p === "library";
}

function hasHome(p: Placement): boolean {
  return p === "both" || p === "home";
}

function encode(library: boolean, home: boolean): Placement {
  if (library && home) return "both";
  if (home) return "home";
  if (library) return "library";
  return "off";
}

/** The tightest seed budget a row named after ONE title can actually use.
 *
 *  1 for a single-media row. 2 for a movies-and-TV row, because seeds are balanced across the media
 *  types present and a budget of 1 therefore yields one type only — a `both` row at 1 gathers no
 *  candidates for its other half, so that library's collection never builds. */
function namedRowSeeds(media: string): number {
  return media === "both" ? 2 : 1;
}

/** Collapsed by default — the grid should read on its own, and this is here for the "wait, who sees
 *  what?" moment. Native <details> so it is keyboard- and screen-reader-accessible with no library. */
function PlacementHelp({ isShared }: { isShared: boolean }) {
  return (
    <details className="group border-t pt-3">
      <summary className="cursor-pointer text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        How does this work?
      </summary>
      <div className="mt-3 space-y-3 text-xs text-muted-foreground">
        {isShared ? (
          <p>
            Everyone sees this <strong>same</strong> row &mdash; it&rsquo;s
            built from titles several people have watched, so there&rsquo;s
            nothing to keep apart.
          </p>
        ) : (
          <p>
            Everyone gets their <strong>own</strong> row. Plex keeps them apart
            by labelling each one and hiding everyone else&rsquo;s label from
            each person&rsquo;s share &mdash; so a friend only ever sees theirs.
          </p>
        )}
        <div className="space-y-2">
          <p>
            <strong className="text-foreground">You</strong> &mdash; the server
            owner: your own row, on your own screens.
          </p>
          <p>
            <strong className="text-foreground">Everyone else</strong> &mdash;
            people you&rsquo;ve shared with, plus Plex Home members. Plex treats
            them all the same here, and each sees only their own row.
          </p>
        </div>
        <div className="space-y-2">
          <p>
            <strong className="text-foreground">Library Recommended</strong>{" "}
            puts a row on that library&rsquo;s Recommended shelf.
          </p>
          <p>
            <strong className="text-foreground">Home screen</strong> puts it on
            a Home screen &mdash; yours in the first column, theirs in the
            second. Plex keeps those two apart, so your Home only ever shows
            your row.
          </p>
        </div>
        <p>
          Turn <strong>them all off</strong> and the row is still built and
          still private. It just doesn&rsquo;t claim a shelf &mdash;
          you&rsquo;ll find it in the library&rsquo;s Collections tab.
        </p>
        <p>
          The one thing Plex can&rsquo;t do: hide other people&rsquo;s rows from{" "}
          <strong className="text-foreground">your</strong> Recommended shelf.
          Hiding works through each person&rsquo;s share, and you don&rsquo;t
          have a share with yourself.
        </p>
      </div>
    </details>
  );
}

/** "a", "a and b", "a, b and c" — for reading a placement back as a sentence. */
function joinPhrases(parts: string[]): string {
  if (parts.length <= 1) return parts[0] ?? "";
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

function surfaces(home: boolean, library: boolean, whose: string): string {
  return joinPhrases(
    [
      home ? `${whose} Home screen` : "",
      library ? `${whose} Recommended shelf` : "",
    ].filter(Boolean),
  );
}

/**
 * The toggles restated as the outcome they produce. Without this the grid only describes Plex's
 * flags, leaving "what did I just do to this row" to be inferred from four switches and an essay.
 */
function placementSummary(
  ownerLibrary: boolean,
  ownerHome: boolean,
  friendsLibrary: boolean,
  friendsHome: boolean,
  isShared: boolean,
): string {
  if (isShared) {
    const where: string[] = [];
    if (ownerHome && friendsHome) where.push("everyone’s Home screen");
    else if (ownerHome) where.push("your Home screen");
    else if (friendsHome) where.push("everyone else’s Home screen");
    if (ownerLibrary || friendsLibrary) where.push("the Recommended shelf");
    return `This row shows on ${joinPhrases(where)}.`;
  }

  const yours = surfaces(ownerHome, ownerLibrary, "your");
  const theirs = surfaces(friendsHome, friendsLibrary, "their");
  const first = yours
    ? `Your row shows on ${yours}.`
    : "Your row doesn’t claim a shelf.";
  const second = theirs
    ? `Everyone else’s row shows on ${theirs}.`
    : "Everyone else’s row doesn’t claim a shelf.";
  return `${first} ${second}`;
}

/**
 * "Where it shows" as a surface x audience grid. The two columns are real, not cosmetic: every
 * person gets their OWN Plex collection, so each of Plex's three flags is set per collection —
 * `promotedToRecommended` on the owner's vs on everyone else's, plus the two Home flags Plex
 * already splits by audience (`promotedToOwnHome` is owner-only; `promotedToSharedHome` covers
 * shared AND managed users — https://support.plex.tv/articles/manage-recommendations/).
 *
 * A SHARED row is the exception: one collection for everyone, so there is only one
 * `promotedToRecommended` to set and the Recommended pair collapses to a single control.
 */
function PlacementToggles({
  placement,
  placementFriends,
  isShared,
  onChange,
}: {
  placement: Placement;
  placementFriends: Placement;
  isShared: boolean;
  onChange: (placement: Placement, placementFriends: Placement) => void;
}) {
  const ownerLibrary = hasLibrary(placement);
  const ownerHome = hasHome(placement);
  const friendsLibrary = hasLibrary(placementFriends);
  const friendsHome = hasHome(placementFriends);
  const allOff = placement === "off" && placementFriends === "off";

  // A shared row is ONE Plex collection, so its single `promotedToRecommended` flag cannot be split
  // by audience the way the two Home flags can — the engine ORs the pair (RowSpec.show_library).
  // Collapse it into one control rather than drawing two switches that silently move together.
  const sharedLibrary = ownerLibrary || friendsLibrary;
  const setSharedLibrary = (v: boolean) =>
    onChange(encode(v, ownerHome), encode(v, friendsHome));

  const cell = (
    checked: boolean,
    label: string,
    onToggle: (v: boolean) => void,
  ) => (
    <div className="flex justify-center">
      <Switch aria-label={label} checked={checked} onCheckedChange={onToggle} />
    </div>
  );

  return (
    <div className="space-y-3 rounded-md border p-4">
      <div className="grid grid-cols-[1fr_auto_auto] items-center gap-x-6 gap-y-3">
        <span />
        <p className="text-center text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          You
        </p>
        <p className="text-center text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Everyone else
        </p>

        <p className="text-sm">Library Recommended</p>
        {isShared ? (
          <div className="col-span-2 flex justify-center">
            <Switch
              aria-label="Library Recommended"
              checked={sharedLibrary}
              onCheckedChange={setSharedLibrary}
            />
          </div>
        ) : (
          <>
            {cell(ownerLibrary, "Owner Library Recommended", (v) =>
              onChange(encode(v, ownerHome), placementFriends),
            )}
            {cell(friendsLibrary, "Friends Library Recommended", (v) =>
              onChange(placement, encode(v, friendsHome)),
            )}
          </>
        )}

        <p className="text-sm">Home screen</p>
        {cell(ownerHome, "Owner Home", (v) =>
          onChange(encode(ownerLibrary, v), placementFriends),
        )}
        {cell(friendsHome, "Friends' Home", (v) =>
          onChange(placement, encode(friendsLibrary, v)),
        )}
      </div>

      {isShared && (
        <p className="text-xs text-muted-foreground">
          Everyone shares this one row, so its Recommended shelf setting applies
          to all of you at once.
        </p>
      )}

      {!allOff && (
        <p className="rounded-md bg-muted/50 p-3 text-sm">
          {placementSummary(
            ownerLibrary,
            ownerHome,
            friendsLibrary,
            friendsHome,
            isShared,
          )}
        </p>
      )}

      {friendsLibrary && !isShared && (
        <p className="rounded-md border border-dashed bg-muted/30 p-3 text-xs text-muted-foreground">
          {ownerLibrary
            ? "Everyone else’s rows show on your Recommended shelf too."
            : "Your row is off this shelf, but everyone else’s rows still show on your Recommended shelf."}{" "}
          You own the server, so there&rsquo;s no share filter to hide them
          behind. Turn off{" "}
          <strong className="text-foreground">
            Everyone else &rarr; Library Recommended
          </strong>{" "}
          to clear them from it.
        </p>
      )}

      {allOff && (
        <p className="rounded-md border border-dashed bg-muted/30 p-3 text-xs text-muted-foreground">
          This row won&rsquo;t appear on any Home screen or Recommended shelf.
          It&rsquo;s still built and kept private &mdash; you&rsquo;ll find it
          under the library&rsquo;s Collections tab.
        </p>
      )}

      <PlacementHelp isShared={isShared} />
    </div>
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
  onRename,
}: {
  collection: Collection | null;
  users: User[];
  onClose: () => void;
  onRename?: () => void;
}) {
  const save = useSaveCollection();
  const [input, setInput] = useState<CollectionInput>(
    collection ? toInput(collection) : blankInput(),
  );
  const isDefault = collection?.slug === "picked";

  const set = (patch: Partial<CollectionInput>) =>
    setInput((prev) => ({ ...prev, ...patch }));

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
            {collection ? (
              <div className="flex items-center gap-2">
                <Input
                  id="row-name"
                  value={input.name || "Picked for You"}
                  disabled
                  className="flex-1 opacity-70"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    onClose();
                    onRename?.();
                  }}
                >
                  Rename
                </Button>
              </div>
            ) : (
              <>
                <Input
                  id="row-name"
                  value={input.name}
                  onChange={(e) => set({ name: e.target.value })}
                  placeholder="e.g. ✨ Hidden Gems for {user}"
                />
                <TemplateVarsHintWithPreview template={input.name} />
              </>
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
            <p className="text-sm font-medium">Recent watches to search</p>
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
              <RecentCountField
                value={input.recent_count}
                onChange={(next) => set({ recent_count: next })}
              />
            )}
          </div>

          <div className="space-y-3 border-t pt-4">
            <p className="text-sm font-medium">Watches to build from</p>
            <p className="text-sm text-muted-foreground">
              How many of a person&rsquo;s recent watches this row is built
              from. The default (30) blends their whole recent history, which is
              right for a general &ldquo;Picked for you&rdquo; row. A small
              number makes the row about one or two specific things they
              watched.
            </p>
            {input.name_template.includes("{top_seed}") && (
              <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
                This row is named{" "}
                <span className="font-mono">
                  &ldquo;{input.name_template}&rdquo;
                </span>
                , so it names one title. Set this to{" "}
                <strong>{namedRowSeeds(input.media)}</strong> and the row really
                is what those watches led to &mdash; otherwise it names one
                watch and fills itself from the other 29.
                {input.media === "both" && (
                  <>
                    {" "}
                    This row covers <strong>movies and TV</strong>, and a single
                    watch is one or the other &mdash; so 1 would leave the other
                    half empty. Use 2 to seed both, or set this row to Movies
                    only or TV only above.
                  </>
                )}
              </p>
            )}
            <div className="flex items-center justify-between gap-4">
              <span className="text-sm">Use the default (30)</span>
              <Switch
                checked={input.max_seeds === null}
                onCheckedChange={(on) =>
                  set({ max_seeds: on ? null : namedRowSeeds(input.media) })
                }
                aria-label="Use the default number of watches to build from"
              />
            </div>
            {input.max_seeds !== null && (
              <MaxSeedsField
                value={input.max_seeds}
                onChange={(next) => set({ max_seeds: next })}
              />
            )}
          </div>

          <div className="space-y-3 border-t pt-4">
            <Label>Where it shows</Label>
            <p className="text-sm text-muted-foreground">
              Which Plex screens this row appears on — matches Plex&rsquo;s
              collection visibility toggles.
            </p>
            <PlacementToggles
              placement={input.placement}
              placementFriends={input.placement_friends}
              isShared={input.build === "shared"}
              onChange={(placement, placementFriends) =>
                set({ placement, placement_friends: placementFriends })
              }
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
