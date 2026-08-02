import type { ReactNode } from "react";
import { useState } from "react";

import { AudiencePicker } from "@/components/rows/audience-picker";
import { GlobalDefaultToggle } from "@/components/rows/global-default-row";
import { LibraryPicker } from "@/components/rows/library-picker";
import { PlacementToggles } from "@/components/rows/placement-toggles";
import { PosterField } from "@/components/rows/poster-field";
import { RowScheduleField } from "@/components/rows/row-schedule-field";
import { RowEffectivenessPanel } from "@/components/rows/row-effectiveness";
import { RowPreview } from "@/components/rows/row-preview";
import { SettingsGroup } from "@/components/rows/settings-group";
import { RowShelfPlacement } from "@/components/rows/row-shelf-placement";
import {
  effectiveSources,
  RowSourcesField,
} from "@/components/rows/row-sources-field";
import { TemplateVarsHint } from "@/components/rows/template-vars-hint";
import { Segmented } from "@/components/segmented";
import { FreshnessSlider } from "@/components/settings/freshness-slider";
import { WatchedSlider } from "@/components/settings/watched-slider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { MaxSeedsField } from "@/components/max-seeds-field";
import { RecentCountField } from "@/components/recent-count-field";
import { SeedWindowField } from "@/components/seed-window-field";
import { RowSizeField } from "@/components/row-size-field";
import { apiErrorMessage } from "@/lib/api";
import { blankInput, toInput } from "@/lib/collections";
import {
  useCollectionEffectiveness,
  useLibraries,
  useSaveCollection,
  useSaveSettings,
  useSettings,
} from "@/lib/queries";
import type { RowTemplate } from "@/lib/row-templates";
import {
  freshnessGlobal,
  freshnessGlobalValue,
  freshnessSeed,
  maxSeedsGlobal,
  recentCountGlobal,
  recentCountSeed,
  watchedPctGlobal,
  watchedPctGlobalValue,
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

/** What to tell someone about the seed budget on a row whose NAME mentions one title.
 *
 * Reads the value the row is actually on, because the three cases need different things said. A row
 * already at the right number needs reassuring, not correcting; a row inheriting the global needs to
 * know its name won't match its contents; and a movies-and-TV row needs to know 1 strands half of it.
 * One static paragraph covering all three told a row sitting on 1 that it "fills itself from the
 * other 29" — describing a row the person did not have.
 */
function seedAdvice(maxSeeds: number | null, media: string): ReactNode {
  const want = namedRowSeeds(media);
  const both = media === "both";

  if (maxSeeds === want) {
    return both
      ? "This row names one title and is built from 2 watches — one film, one show. Each library gets a row named after something they actually watched."
      : "This row names one title, and that is exactly what it’s built from. Nothing to change here.";
  }

  // A movies-and-TV row set to ONE seed has a worse problem than a mismatched name: a watch is
  // either a film or a show, so one of the two libraries is left with nothing to build from and
  // never gets a row at all. Lead with that, because it is the one that loses half the row.
  if (both && maxSeeds !== null && maxSeeds < 2) {
    return (
      <>
        This row covers <strong>movies and TV</strong>, but it&rsquo;s built
        from one watch &mdash; and a watch is either a film or a show, never
        both. One of your two libraries would get nothing. Set it to{" "}
        <strong>2</strong>, or set the row to Movies only or TV only above.
      </>
    );
  }

  return (
    <>
      This row&rsquo;s name mentions <strong>one</strong> title, but{" "}
      {maxSeeds === null ? (
        <>
          it&rsquo;s blending their whole recent viewing &mdash; so it will name
          one watch and fill up with titles picked for all the others
        </>
      ) : (
        // "1 watches" — the plural has to follow the number, and this branch is reachable at any
        // value the field allows.
        <>
          it&rsquo;s built from {maxSeeds} watch{maxSeeds === 1 ? "" : "es"}
          &nbsp;&mdash; so the name only matches part of what ends up in it
        </>
      )}
      . Set it to <strong>{want}</strong>
      {both && " — one film and one show"}.
    </>
  );
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
  const libraries = useLibraries();
  const effectiveness = useCollectionEffectiveness(collection?.id ?? null);
  const ratingSource = asRatingSource(
    settings.data?.["recommendations.rating_source"],
  );
  const ratingLabel = RATING_LABELS[ratingSource];
  const [input, setInput] = useState<CollectionInput>(
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
  // Whether this row's TITLE claims a particular watch. Mirrors the engine's `_names_a_seed`, and
  // decides whether the cycle window is worth offering.
  const namesASeed = (input.name_template || input.name).includes("{top_seed}");
  // Whether the engine forces this row to a nightly cadence — which it does for a row that FOLLOWS a
  // watch, by name or by cycling. Both arms, not just `namesASeed`: an unnamed cycling row is run
  // nightly too, so showing it a freshness slider would state a cadence the row does not obey.
  const followsAWatch = namesASeed || input.seed_window > 1;
  const drawsOnSummary = [
    // "[]" means every library OF THIS ROW'S TYPE — saying "every library" on a movies row
    // contradicted the picker right below it, which ticks only the movie ones.
    input.library_keys.length === 0
      ? ((
          { movie: "every movie library", show: "every TV library" } as Record<
            string,
            string
          >
        )[input.media] ?? "every library")
      : `${input.library_keys.length} librar${input.library_keys.length === 1 ? "y" : "ies"}`,
    input.candidate_sources.length === 0
      ? "default sources"
      : `${input.candidate_sources.length} source${input.candidate_sources.length === 1 ? "" : "s"}`,
    // A followed-watch row's stored freshness is ignored by the engine, so reporting it here would
    // describe a cadence the row does not run on.
    followsAWatch
      ? "refreshes nightly"
      : input.freshness === null
        ? "default freshness"
        : input.freshness >= 1
          ? "refreshes nightly"
          : input.freshness <= 0
            ? "frozen"
            : `${Math.round(input.freshness * 100)}% fresh`,
    input.max_seeds === null
      ? null
      : `${input.max_seeds} watch${input.max_seeds === 1 ? "" : "es"}`,
    input.seed_window > 1 ? `cycling ${input.seed_window}` : null,
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
    // A PAGE, not a dialog. A modal caps at 90% of the viewport, and it was that height limit — not
    // the number of settings — that forced every group into a collapsed accordion. Which in turn hid
    // the one warning that stops a movies-and-TV row building half empty, since it lived inside a
    // section that starts closed. With the cap gone the groups can stay open, warnings can sit
    // permanently beside the setting they concern, and there is room for the preview panel that
    // turns each abstract setting into "here is what Sarah will see tonight".
    <div className="mx-auto w-full max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">
          {collection ? "Edit row" : "Add a row"}
        </h1>
        <p className="mt-1 text-muted-foreground">
          A row is a strip of “Picked for You”-style recommendations on your
          users’ Plex home screens.
        </p>
      </div>

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

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
        <div className="space-y-5">
          <SettingsGroup
            title="The basics"
            description="What this row is called, who gets one, and how it's put together."
          >
            <div className="space-y-2">
              <Label htmlFor="row-name">Name</Label>
              {collection ? (
                // A disabled box beside a button reads as a field that is broken, and gives no clue
                // why this one setting behaves unlike every other one on the page. Renaming an
                // existing row is not a form field: it rewrites the collection on Plex for every
                // person who has it, one at a time, with progress — so it gets its own screen. Say
                // that, rather than leaving a greyed-out input to imply it.
                <div className="space-y-2">
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
                      Rename…
                    </Button>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Renaming rewrites this row&rsquo;s collection on Plex for
                    everyone who has it, so it happens on its own screen where
                    you can watch it go through. Nothing else on this page is
                    touched.
                  </p>
                </div>
              ) : (
                <>
                  <Input
                    id="row-name"
                    value={input.name}
                    onChange={(e) => set({ name: e.target.value })}
                    placeholder="e.g. ✨ Hidden Gems for {user}"
                  />
                  {/* Just the list of placeholders here. What the name BECOMES is shown in the
                    preview panel, which is always on screen — printing it twice made the field's
                    own help longer without answering anything the panel didn't. */}
                  <TemplateVarsHint />
                </>
              )}
            </div>

            <div className="space-y-2">
              <Label>One row each, or one for everyone?</Label>
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
                  ? "Everyone gets their own row, built from what they have watched. Nobody sees anyone else’s."
                  : "One row, the same for everyone who can see it, built from what your users have watched between them."}
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
                      min_watchers: Math.max(
                        2,
                        Number(event.target.value) || 2,
                      ),
                    })
                  }
                  className="w-24"
                />
                <p className="text-sm text-muted-foreground">
                  Keeps one person’s viewing from ever showing up in a shared
                  row. 2 is a good default.
                </p>
                <SharedRowReachWarning
                  users={users}
                  audience={input.audience}
                  audienceUserIds={input.audience_user_ids}
                  minWatchers={input.min_watchers}
                />
              </div>
            )}
          </SettingsGroup>

          <SettingsGroup
            title="What goes in it"
            description="Which libraries this row can pick from, and what drives the picks."
            summary={drawsOnSummary}
          >
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
              description="How much of this row can be things they have already finished watching. At 0 the row is all new suggestions. Leave it on the global default to follow Settings → Finding titles."
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
                        this on also lets already-watched titles into the row,
                        so there is nothing else to set.
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
                          so one they&rsquo;re three episodes into still turns
                          up.
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

            {/* Nothing at all for a row that follows a watch. The cadence is fixed for those rows, so
                a heading and a paragraph explaining a control that isn't there is just something else
                to read past — the section summary already says "refreshes nightly". */}
            {!followsAWatch && (
              <InheritableField
                label="How often it changes"
                labelFor="row-freshness"
                description="How often this row swaps some of its titles for new ones. Leave it on the global default to follow Settings → Finding titles."
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
            )}

            {usesWebSearch && (
              <InheritableField
                label="Watches the AI web search looks up"
                description="AI web search looks up one watch at a time — “what to watch if you liked X”. This is how many of their recent watches it asks about. More gives wider results and takes more searches. It changes nothing for the other sources."
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
                  How many recent watches this row is built from. A high number
                  blends their whole recent viewing, which suits a general
                  &ldquo;Picked for you&rdquo; row. A low number makes the row
                  about one or two specific things they watched.
                </>
              }
              ariaLabel="Use the default number of watches to build from"
              inheriting={input.max_seeds === null}
              globalValue={maxSeedsGlobal(settings.data)}
              // Turning this OFF seeds the NAMED-row value (1 or 2), not the global — someone
              // reaching for this control almost always wants a row about one specific watch, and
              // the global is one switch-flip away again.
              //
              // Turning it ON also drops the cycle back to 1. The cycle control only renders for a
              // 1..2-seed row, so leaving the window set here would hide it while the engine went on
              // cycling the row AND forcing it to nightly rebuilds, with nothing in the editor to
              // explain it or undo it.
              onToggle={(on) =>
                set(
                  on
                    ? { max_seeds: null, seed_window: 1 }
                    : { max_seeds: namedRowSeeds(input.media) },
                )
              }
              // The advice has to read the CURRENT value, not assume the global. Static copy told
              // someone already sitting on 1 that their row "fills itself from the other 29", which
              // is only true while it inherits the 30-watch default — so the one hint meant to make
              // this setting clear was describing a row they didn't have.
              before={
                namesASeed && (
                  <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
                    {seedAdvice(input.max_seeds, input.media)}
                  </p>
                )
              }
            >
              <MaxSeedsField
                label=""
                value={input.max_seeds ?? 0}
                // Typing a wider budget hides the cycle control too, so it has to reset the window
                // for the same reason the inherit toggle above does — otherwise the row keeps
                // cycling with no way to see or stop it.
                onChange={(next) =>
                  set(
                    next > 2
                      ? { max_seeds: next, seed_window: 1 }
                      : { max_seeds: next },
                  )
                }
              />
            </InheritableField>

            {/* Only for a row that builds from one or two watches. Above that it is blending a whole
                history and "which watch does it follow" has no answer, so asking would be noise. */}
            {(input.max_seeds ?? 0) > 0 && (input.max_seeds ?? 0) <= 2 && (
              <div className="space-y-3 border-t pt-4">
                <p className="text-sm font-medium">Which watch it follows</p>
                <p className="text-sm text-muted-foreground">
                  Normally this row follows their most recent watch, and stays
                  on it until they finish something else. Raise this and it
                  cycles through that many of their recent watches instead
                  &mdash; a different one each day, then back round again.
                </p>
                <SeedWindowField
                  label=""
                  value={input.seed_window}
                  onChange={(next) => set({ seed_window: next })}
                />
                {input.seed_window > 1 && (
                  <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
                    Cycling rebuilds this row whenever the watch changes, so it
                    writes to Plex most nights. Set it back to 1 if you&rsquo;d
                    rather the row only changed when they actually watch
                    something new.
                  </p>
                )}
              </div>
            )}
          </SettingsGroup>

          <SettingsGroup
            title="How it's shown, and when it runs"
            description="The order titles appear in, how many there are, and when Shortlist rebuilds the row."
          >
            <div className="space-y-2">
              <Label>What order the titles appear in</Label>
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
          </SettingsGroup>

          <SettingsGroup
            title="Where it appears"
            description="Which Plex screens this row shows up on, and where in the shelf."
            summary={placementSummary}
          >
            <div className="space-y-3">
              <Label>Where it shows</Label>
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
          </SettingsGroup>

          <SettingsGroup
            title="Artwork"
            description="The picture Plex shows on the row. Optional — Plex uses its own by default."
            summary={posterSummary}
            defaultOpen={false}
          >
            <PosterField
              value={input.poster}
              onChange={(poster) => set({ poster })}
              collectionId={collection?.id ?? null}
              hasImage={collection?.poster?.has_image ?? false}
            />
          </SettingsGroup>

          <SettingsGroup
            title="Requests"
            description="Tag titles requested from this row, so Sonarr/Radarr can tell them apart. Optional."
            summary={requestSummary}
            defaultOpen={false}
          >
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
          </SettingsGroup>

          {save.isError && (
            <p role="alert" className="text-sm text-destructive">
              {apiErrorMessage(
                save.error,
                "Couldn’t save this row. Try again.",
              )}
            </p>
          )}
        </div>

        {/* Sticky so it stays beside whichever setting is being changed — the point is watching the
            outcome move as you touch things, which it cannot do if it scrolls off. */}
        <aside className="lg:sticky lg:top-6 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto lg:pr-1">
          <div className="space-y-4">
            <RowPreview
              input={input}
              users={users}
              libraries={libraries.data ?? []}
              settings={settings.data}
              followsAWatch={followsAWatch}
              globalFreshness={freshnessGlobalValue(settings.data)}
              globalWatchedPct={watchedPctGlobalValue(settings.data)}
            />
            {/* Only for a SAVED row. A row being created has no history, and a panel of dashes would
                be a worse answer than no panel. */}
            {collection && (
              <RowEffectivenessPanel
                data={effectiveness.data}
                isLoading={effectiveness.isLoading}
                rowId={collection.id}
              />
            )}
          </div>
        </aside>
      </div>

      {/* Pinned to the bottom of the viewport: on a long page the save button would otherwise be
          somewhere off-screen, and "where did Save go" is exactly the friction a dialog didn't have. */}
      <div className="sticky bottom-0 z-10 -mx-4 flex justify-end gap-2 border-t bg-background px-4 py-3 sm:-mx-6 sm:px-6">
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
      </div>
    </div>
  );
}
