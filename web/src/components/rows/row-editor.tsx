import type { ReactNode } from "react";
import { useState } from "react";

import { AudiencePicker } from "@/components/rows/audience-picker";
import { GlobalDefaultToggle } from "@/components/rows/global-default-row";
import { LibraryPicker } from "@/components/rows/library-picker";
import { PlacementToggles } from "@/components/rows/placement-toggles";
import { PosterField } from "@/components/rows/poster-field";
import { RowScheduleField } from "@/components/rows/row-schedule-field";
import { RowSection } from "@/components/rows/row-section";
import { RowShelfPlacement } from "@/components/rows/row-shelf-placement";
import {
  effectiveSources,
  RowSourcesField,
} from "@/components/rows/row-sources-field";
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
import {
  useSaveCollection,
  useSaveSettings,
  useSettings,
} from "@/lib/queries";
import type { RowTemplate } from "@/lib/row-templates";
import {
  freshnessGlobal,
  freshnessSeed,
  maxSeedsGlobal,
  recentCountGlobal,
  recentCountSeed,
  watchedPctGlobal,
  watchedPctSeed,
} from "@/lib/row-globals";
import {
  asRatingSource,
  RATING_LABELS,
  RATING_SOURCES,
} from "@/lib/rating-sources";
import type { Collection, CollectionInput, User } from "@/lib/types";

/** The tightest seed budget a row named after ONE title can actually use.
 *
 *  1 for a single-media row. 2 for a movies-and-TV row, because seeds are balanced across the media
 *  types present and a budget of 1 therefore yields one type only — a `both` row at 1 gathers no
 *  candidates for its other half, so that library's collection never builds. */
function namedRowSeeds(media: string): number {
  return media === "both" ? 2 : 1;
}

/** What each pick order actually does, in the row editor's voice: says what happens, not what it is.
 *
 *  "Shuffled" names its cost out loud. It is the only order that rewrites the collection on Plex on
 *  nights when nothing about the row has changed — the other three ride along with a refresh the row
 *  was doing anyway, so they cost nothing extra. */
function pickOrderHelp(
  order: CollectionInput["pick_order"],
  ratingLabel: string,
): string {
  switch (order) {
    case "rating":
      // Names the service the server is actually configured for: "Highest rated" alone leaves the
      // owner guessing whose score they get, and the answer is a setting they may not have visited.
      return `Highest ${ratingLabel} score first, whatever the match.`;
    case "newest":
      return "Most recently released first.";
    case "shuffle":
      return "A different order every day, from the same titles. The only order that writes to Plex on days the row is otherwise unchanged.";
    default:
      return "Strongest suggestions first — how well each title matches what they watch.";
  }
}

/**
 * One "leave on the global default, or override it here" field.
 *
 * The same shape used four times in this dialog (already-watched cap, freshness, recent-watches,
 * seed count): a label, a description, the `GlobalDefaultToggle`, and the field itself once the row
 * overrides it. Each call site now states only what's different — its copy and its control — instead
 * of repeating the toggle/conditional wiring.
 */
function InheritableField({
  label,
  labelFor,
  description,
  ariaLabel,
  inheriting,
  globalValue,
  onToggle,
  before,
  after,
  children,
}: {
  label: string;
  /** Set only when the field it labels has a matching `id` — some of these fields (RecentCountField,
   *  MaxSeedsField) already wire their own internal `<Label>`, so this heading stays a plain string. */
  labelFor?: string;
  description: ReactNode;
  ariaLabel: string;
  inheriting: boolean;
  globalValue: string | null;
  onToggle: (usesGlobal: boolean) => void;
  /** Extra content between the description and the toggle (the {top_seed} warning). */
  before?: ReactNode;
  /** Extra content after the field, shown regardless of inheriting (the rewatch/unstarted switches). */
  after?: ReactNode;
  /** The control shown once the row overrides the global. */
  children: ReactNode;
}) {
  return (
    <div className="space-y-3 border-t pt-4">
      {labelFor ? (
        <Label htmlFor={labelFor}>{label}</Label>
      ) : (
        <p className="text-sm font-medium">{label}</p>
      )}
      <p className="text-sm text-muted-foreground">{description}</p>
      {before}
      <GlobalDefaultToggle
        ariaLabel={ariaLabel}
        inheriting={inheriting}
        globalValue={globalValue}
        settingsHash="recommendations"
        onChange={onToggle}
      />
      {!inheriting && children}
      {after}
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
  template = null,
  users,
  onClose,
  onRename,
}: {
  collection: Collection | null;
  /** Seeds a NEW row's fields. Never set when editing an existing row, and every field it fills
   *  stays editable — a template is a starting point, not a mode. */
  template?: RowTemplate | null;
  users: User[];
  onClose: () => void;
  onRename?: () => void;
}) {
  const save = useSaveCollection();
  const saveSettings = useSaveSettings();
  // Read-only here: the editor never writes settings, it only names the globals a row inherits.
  const settings = useSettings();
  const ratingSource = asRatingSource(
    settings.data?.["recommendations.rating_source"],
  );
  const ratingLabel = RATING_LABELS[ratingSource];  const [input, setInput] = useState<CollectionInput>(
    collection
      ? toInput(collection)
      : { ...blankInput(), ...(template?.values ?? {}) },
  );
  const isDefault = collection?.slug === "picked";

  const set = (patch: Partial<CollectionInput>) =>
    setInput((prev) => ({ ...prev, ...patch }));

  // "Watches the AI searches from" caps ONE source's lookups. On a row that doesn't use AI web
  // search it changes nothing, so showing it invites someone to tune a setting with no effect —
  // and it sat directly beneath "Watches to build from", which is the row-wide one, making the two
  // read as rival answers to the same question.
  const usesWebSearch = effectiveSources(
    input.candidate_sources,
    settings.data,
  ).includes("llm_web");

  // What each folded section says about itself while closed. A disclosure that hides both its
  // controls AND what they are currently set to is worse than the flat list it replaced — these are
  // what let someone skip a section rather than open it to find out they didn't need it.
  const posterSummary =
    (
      {
        "": "Plex’s own artwork",
        upload: "Uploaded image",
        text: "Generated from text",
        ai: "AI image",
        generate: "AI image",
      } as Record<string, string>
    )[input.poster.mode] ?? "Plex’s own artwork";
  const drawsOnSummary = [
    // "[]" means every library OF THIS ROW'S TYPE — saying "every library" on a movies row
    // contradicted the picker right below it, which ticks only the movie ones.
    input.library_keys.length === 0
      ? ({ movie: "every movie library", show: "every TV library" } as Record<
          string,
          string
        >)[input.media] ?? "every library"
      : `${input.library_keys.length} librar${input.library_keys.length === 1 ? "y" : "ies"}`,
    input.candidate_sources.length === 0
      ? "default sources"
      : `${input.candidate_sources.length} source${input.candidate_sources.length === 1 ? "" : "s"}`,
    input.freshness === null
      ? "default freshness"
      : input.freshness >= 1
        ? "refreshes nightly"
        : input.freshness <= 0
          ? "frozen"
          : `${Math.round(input.freshness * 100)}% fresh`,
    input.max_seeds === null ? null : `${input.max_seeds} watch${input.max_seeds === 1 ? "" : "es"}`,
  ]
    .filter(Boolean)
    .join(" · ");
  const placementSummary = (() => {
    const mine = input.placement === "off" ? 0 : 1;
    const theirs = input.placement_friends === "off" ? 0 : 1;
    if (mine && theirs) return "You and everyone else";
    if (theirs) return "Everyone else only";
    if (mine) return "You only";
    return "Hidden from every shelf";
  })();
  const requestSummary = input.request_tag
    ? `Tagged “${input.request_tag}”`
    : "No tag";

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

        {/* Purely informational — nothing about the template is stored on the row, and every
            field it filled is editable below. It's here so a prefilled form doesn't read as
            settings that appeared from nowhere. */}
        {template && !collection && (
          <p className="rounded-md bg-muted/60 px-3 py-2 text-sm text-muted-foreground">
            Started from{" "}
            <strong className="text-foreground">
              {template.emoji} {template.title}
            </strong>
            {/* Several template titles end in an ellipsis ("Because you watched…"), which the
                sentence stop then doubled into "…." — so the separator is a dash, not a full stop. */}
            {" — change anything you like: "}
            {template.highlights.join(", ").toLowerCase()}.
          </p>
        )}

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

          <div className="space-y-2">
            <Label>Order</Label>
            <Segmented
              value={input.pick_order}
              onChange={(pick_order) => set({ pick_order })}
              ariaLabel="How the titles in this row are ordered"
              options={[
                { value: "best", label: "Best match" },
                { value: "rating", label: "Highest rated" },
                { value: "newest", label: "Newest" },
                { value: "shuffle", label: "Shuffled" },
              ]}
            />
            <p className="text-sm text-muted-foreground">
              {pickOrderHelp(input.pick_order, ratingLabel)}
            </p>
            {/* The score to sort on is chosen HERE, not in Settings. "Highest rated" raises the
                question "rated by whom?" at exactly this moment, and answering it by sending someone
                to another screen is how the setting stayed undiscovered. It is still one server-wide
                value, so the note says so rather than implying it is per-row. */}
            {input.pick_order === "rating" && (
              <div className="space-y-1.5 rounded-md border bg-muted/30 p-3">
                <Label htmlFor="row-rating-source">Rated by</Label>
                <select
                  id="row-rating-source"
                  value={ratingSource}
                  onChange={(e) =>
                    saveSettings.mutate({
                      "recommendations.rating_source": asRatingSource(
                        e.target.value,
                      ),
                    })
                  }
                  disabled={saveSettings.isPending}
                  className="h-9 w-56 rounded-md border bg-background px-3 text-sm"
                >
                  {RATING_SOURCES.map((source) => (
                    <option key={source} value={source}>
                      {RATING_LABELS[source]}
                    </option>
                  ))}
                </select>
                <p className="text-xs text-muted-foreground">
                  {ratingSource === "tmdb"
                    ? "TMDB needs no setup. IMDb, Trakt, Rotten Tomatoes and Metacritic come from MDBList and need its API key in Settings → Requests."
                    : `Scores come from MDBList — without its API key in Settings → Requests, ${ratingLabel} rows quietly fall back to TMDB.`}{" "}
                  Shared by every row ordered by rating.
                </p>
              </div>
            )}
          </div>

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

          <RowSection
            title="Artwork"
            summary={posterSummary}
          >
          <PosterField
            value={input.poster}
            onChange={(poster) => set({ poster })}
            collectionId={collection?.id ?? null}
            hasImage={collection?.poster?.has_image ?? false}
          />
          </RowSection>

          <RowSection title="What it draws on" summary={drawsOnSummary}>

          <LibraryPicker
            libraryKeys={input.library_keys}
            media={input.media}
            onChange={(next) =>
              set({
                ...next,
                // `media` is DERIVED from the libraries picked, so a row can stop being shows-only
                // without anyone touching the flag. Its control is hidden then, and the API refuses
                // the combination — which would be a save failing with no visible cause.
                ...(next.media !== "show" ? { unstarted_only: false } : {}),
              })
            }
          />

          <RowSourcesField
            value={input.candidate_sources}
            onChange={(candidate_sources) => set({ candidate_sources })}
          />

          <InheritableField
            label="Already-watched titles"
            labelFor="row-watched-pct"
            description="How much of this row may be things a person has already finished. Leave on the global default to follow Settings → Finding titles."
            ariaLabel="Use the global already-watched default"
            inheriting={input.watched_pct === null}
            globalValue={watchedPctGlobal(settings.data)}
            onToggle={(on) =>
              set({ watched_pct: on ? null : watchedPctSeed(settings.data) })
            }
            after={
              <>
                {/* The percentage above is a CEILING — it permits finished titles, it never prefers
                    them, so on a library with plenty of unwatched candidates even 100% yields an
                    unwatched row. This switch is what actually makes a rewatch shelf, which is why
                    it is named after the row someone wants rather than after its effect on the
                    setting above: "lead with things they've seen" could only be understood by
                    someone who had already understood the ceiling. */}
                <div className="flex items-start justify-between gap-4 rounded-md border p-3">
                  <div className="space-y-1">
                    <Label htmlFor="row-rewatch">
                      Make this a &ldquo;watch it again&rdquo; row
                    </Label>
                    <p className="text-sm text-muted-foreground">
                      Films and shows they&rsquo;ve already finished lead the
                      row, and new suggestions fill whatever is left. Turning
                      this on also lets already-watched titles into the row, so
                      there is nothing else to set.
                    </p>
                  </div>
                  <Switch
                    id="row-rewatch"
                    aria-label="Make this a watch it again row"
                    checked={input.rewatch}
                    onCheckedChange={(rewatch) =>
                      set({
                        rewatch,
                        // A rewatch row needs finished titles in its pool at all, so lift a 0% cap
                        // off the global default in the same click — otherwise the switch silently
                        // does nothing.
                        ...(rewatch && input.watched_pct === 0
                          ? { watched_pct: 1 }
                          : {}),
                        // Mutually exclusive: the two ask for opposite things, and the API refuses
                        // the pair. Clearing it here means the owner never meets that error.
                        ...(rewatch ? { unstarted_only: false } : {}),
                      })
                    }
                  />
                </div>

                {/* Shown for shows only, and cleared when the row stops being a shows row: an
                    invisible setting the API then refuses is a save that fails for no visible
                    reason. */}
                {input.media === "show" && (
                  <div className="flex items-start justify-between gap-4 rounded-md border p-3">
                    <div className="space-y-1">
                      <Label htmlFor="row-unstarted">
                        Only series they haven&rsquo;t started
                      </Label>
                      <p className="text-sm text-muted-foreground">
                        Drops any show they&rsquo;ve watched even one episode
                        of. Normally only <em>finished</em> shows are skipped,
                        so one they&rsquo;re three episodes into still turns up.
                      </p>
                    </div>
                    <Switch
                      id="row-unstarted"
                      aria-label="Only series they have not started"
                      checked={input.unstarted_only}
                      onCheckedChange={(unstarted_only) =>
                        set({
                          unstarted_only,
                          ...(unstarted_only ? { rewatch: false } : {}),
                        })
                      }
                    />
                  </div>
                )}
              </>
            }
          >
            <WatchedSlider
              id="row-watched-pct"
              value={Math.round((input.watched_pct ?? 0) * 100)}
              onChange={(pct) => set({ watched_pct: pct / 100 })}
            />
          </InheritableField>

          <InheritableField
            label="Freshness"
            labelFor="row-freshness"
            description="How often this row swaps in new titles — which titles it holds, not the sequence they appear in (that’s Order, below). Leave on the global default to follow Settings → Finding titles."
            ariaLabel="Use the global freshness default"
            inheriting={input.freshness === null}
            globalValue={freshnessGlobal(settings.data)}
            onToggle={(on) =>
              set({ freshness: on ? null : freshnessSeed(settings.data) })
            }
          >
            <FreshnessSlider
              id="row-freshness"
              value={Math.round((input.freshness ?? 0) * 100)}
              onChange={(pct) => set({ freshness: pct / 100 })}
            />
          </InheritableField>

          {usesWebSearch && (
          <InheritableField
            label="Watches the AI searches from"
            description="How many of a person’s most recent watches the AI web-search source looks up for this row (one cached search each). Only affects rows using AI web search. Leave on the global default to follow Settings → Finding titles."
            ariaLabel="Use the global recent-watches default"
            inheriting={input.recent_count === null}
            globalValue={recentCountGlobal(settings.data)}
            onToggle={(on) =>
              set({
                recent_count: on ? null : recentCountSeed(settings.data),
              })
            }
          >
            <RecentCountField
              label=""
              value={input.recent_count ?? 0}
              onChange={(next) => set({ recent_count: next })}
            />
          </InheritableField>
          )}

          <InheritableField
            label="Watches to build from"
            description={
              <>
                How many of a person&rsquo;s recent watches this row is built
                from. The global default blends their whole recent history,
                which is right for a general &ldquo;Picked for you&rdquo; row. A
                small number makes the row about one or two specific things they
                watched.
              </>
            }
            ariaLabel="Use the default number of watches to build from"
            inheriting={input.max_seeds === null}
            globalValue={maxSeedsGlobal(settings.data)}
            // Turning this OFF seeds the NAMED-row value (1 or 2), not the global — someone
            // reaching for this control almost always wants a row about one specific watch, and
            // the global is one switch-flip away again.
            onToggle={(on) =>
              set({ max_seeds: on ? null : namedRowSeeds(input.media) })
            }
            before={
              (input.name_template || input.name).includes("{top_seed}") && (
                <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
                  This row is named{" "}
                  <span className="font-mono">
                    &ldquo;{input.name_template || input.name}&rdquo;
                  </span>
                  , so it names one title. Set this to{" "}
                  <strong>{namedRowSeeds(input.media)}</strong> and the row
                  really is what those watches led to &mdash; otherwise it names
                  one watch and fills itself from the other 29.
                  {input.media === "both" && (
                    <>
                      {" "}
                      This row covers <strong>movies and TV</strong>, and a
                      single watch is one or the other &mdash; so 1 would leave
                      the other half empty. Use 2 to seed both, or set this row
                      to Movies only or TV only above.
                    </>
                  )}
                </p>
              )
            }
          >
            <MaxSeedsField
              label=""
              value={input.max_seeds ?? 0}
              onChange={(next) => set({ max_seeds: next })}
            />
          </InheritableField>
          </RowSection>

          <RowSection title="Where it appears" summary={placementSummary}>

          <div className="space-y-3">
            <Label>Where it shows</Label>
            <p className="text-sm text-muted-foreground">
              Which Plex screens this row appears on — matches Plex&rsquo;s
              collection visibility toggles.
            </p>
            <PlacementToggles
              placement={input.placement}
              placementFriends={input.placement_friends}
              isShared={input.build === "shared"}
              users={users}
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
          </RowSection>

          <RowSection title="Requests" summary={requestSummary}>
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
          </RowSection>
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
