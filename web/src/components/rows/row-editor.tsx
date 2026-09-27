import { ListChecks, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { RowRequestSettings } from "@/components/rows/row-request-settings";
import { AudiencePicker } from "@/components/rows/audience-picker";
import { InheritableField } from "@/components/rows/inheritable-field";
import { LibraryPicker } from "@/components/rows/library-picker";
import { PlacementToggles } from "@/components/rows/placement-toggles";
import { PosterField } from "@/components/rows/poster-field";
import { RowContentsFields } from "@/components/rows/row-contents-fields";
import { RowKindChangeDialog } from "@/components/rows/row-kind-change-dialog";
import { RowKindPicker } from "@/components/rows/row-kind-picker";
import { RowKindSettings, TakeTurns } from "@/components/rows/row-kind-settings";
import { RowPlexCard } from "@/components/rows/row-plex-card";
import {
  RowDescriptionField,
  RowSortPrefixField,
} from "@/components/rows/row-plex-details-field";
import { RowScheduleField } from "@/components/rows/row-schedule-field";
import { RowShowDaysField } from "@/components/rows/row-show-days-field";
import { showDaysSummary } from "@/lib/show-days";
import { RowDestructiveActions } from "@/components/rows/row-destructive-actions";
import { RowEnableToggle } from "@/components/rows/row-enable-toggle";
import { RowEffectivenessPanel } from "@/components/rows/row-effectiveness";
import { RowPreview } from "@/components/rows/row-preview";
import { RowRunAction } from "@/components/rows/row-run-action";
import { SettingsGroup } from "@/components/rows/settings-group";
import { RowShelfPlacement } from "@/components/rows/row-shelf-placement";
import { effectiveSources } from "@/components/rows/row-sources-field";
import { TemplateVarsHint } from "@/components/rows/template-vars-hint";
import { Segmented } from "@/components/segmented";
import { RefreshDaysField } from "@/components/settings/refresh-days-field";
import { IdleHoldField } from "@/components/settings/idle-hold-field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RowSizeField } from "@/components/row-size-field";
import { apiErrorMessage } from "@/lib/api";
import { blankInput, hasUnsavedChanges, toInput } from "@/lib/collections";
import { settingString } from "@/lib/format";
import {
  useCollectionEffectiveness,
  useCollections,
  useLibraries,
  useSaveCollection,
  useSaveSettings,
  useSeasons,
  useSettings,
} from "@/lib/queries";
import { requestReadiness, requestsSummary } from "@/lib/requests";
import { useStickyTop } from "@/lib/use-sticky-top";
import {
  applyRowKind,
  BASELINE_FIELDS,
  baselineTakes,
  DEFAULT_ROW_NAME_SETTINGS,
  describeKindChange,
  FILL_META,
  followsAWatch as rowFollowsAWatch,
  hiddenButRead,
  KIND_GROUP,
  KIND_META,
  kindBaseline as baselineOf,
  kindDisabledReason,
  kindSwitchBase,
  normalizeKindResult,
  renameAt,
  renameAtNote,
  renameProblem,
  rowKindOf,
  SEED_NAME_IN_SETTINGS,
  visibleSettings,
  type RowKind,
  type RowKindChoice,
  type RowKindContext,
} from "@/lib/row-kinds";
import type { RowTemplate } from "@/lib/row-templates";
import {
  idleHoldGlobal,
  idleHoldSeed,
  maxSeedsSeed,
  refreshDaysGlobal,
  refreshDaysGlobalValue,
  refreshDaysSeed,
} from "@/lib/row-globals";
import {
  asRatingSource,
  RATING_LABELS,
  RATING_SOURCES,
} from "@/lib/rating-sources";
import type { Collection, CollectionInput, User } from "@/lib/types";

/** The global `row.name_template` every install ships with, until Settings says otherwise. */
const DEFAULT_ROW_NAME = "✨ {library_name} Picked for You";

/** What each pick order actually does, in the row editor's voice: says what happens, not what it is.
 *
 *  "Shuffled" and "Taking turns" name their cost out loud. They are the two orders that rewrite the
 *  collection on Plex on nights when nothing about the row has changed — the other four ride along
 *  with a refresh the row was doing anyway, so they cost nothing extra. */
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
    case "new_first":
      // Says "when the row refreshes" out loud because the commonest disappointment here is setting
      // this on a row at the default cadence and seeing nothing move for a week.
      return "Whatever is new goes to the front, the rest follow in match order. Only moves on the nights the row refreshes.";
    case "rotate":
      return "Everything keeps its place in the list, but the front moves along by one title a day, so each pick gets a turn there. Writes to Plex on days the row is otherwise unchanged.";
    default:
      return "Strongest suggestions first — how well each title matches what they watch.";
  }
}

/** How many people the row reaches as the engine counts them (enabled, not paused, in its audience);
 *  null while the roster loads, so no warning fires on a guess. */
function audienceSize(input: CollectionInput, users: User[]): number | null {
  if (users.length === 0) return null;
  return users.filter(
    (user) =>
      user.enabled &&
      !user.prefs?.paused &&
      (input.audience === "everyone" ||
        input.audience_user_ids.includes(user.id)),
  ).length;
}

function kindTitle(choice: RowKindChoice): string {
  return choice.kind === "seasonal"
    ? `${KIND_META.seasonal.title} · ${FILL_META[choice.fill].title}`
    : KIND_META[choice.kind].title;
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
  /** Hands a name to the rename screen, which owns the Plex work: the typed-but-unapplied name from
   *  Rename…, or — with `saved` — the one a kind switch proposed, which the save already carried, so
   *  the screen only streams the rename from the title the collections still carry. */
  onRename?: (proposedName: string, saved?: { oldTemplate: string }) => void;
}) {
  const save = useSaveCollection();
  const saveSettings = useSaveSettings();
  // Read-only here: the editor never writes settings, it only names the globals a row inherits.
  const settings = useSettings();
  const libraries = useLibraries();
  // The summary scrolls with the page (no scrollbar of its own) and stays in view: see useStickyTop.
  // 80 clears the sticky Save bar at the bottom of the page (61px) with room to spare.
  const [summaryRef, summaryTop] = useStickyTop<HTMLElement>(24, 80);
  // Every row's name by slug, so the summary names a row this one sits beside.
  const collections = useCollections();
  const rowNames = Object.fromEntries(
    (collections.data ?? []).map((row) => [row.slug, row.name_template || row.name]),
  );
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

  // The header's on/off switch saves straight away, so what it saved is the saved row's `enabled` from
  // then on — and the form's, or Save would send back the value the page opened with and undo it.
  // Everything that asks whether the row is on (the notes, where a rename lands, the preview) reads it
  // from here.
  const [savedEnabled, setSavedEnabled] = useState<boolean | null>(null);
  const savedRow =
    collection && savedEnabled !== null
      ? { ...collection, enabled: savedEnabled }
      : collection;
  const saved = savedRow ? toInput(savedRow) : undefined;
  // The switch and page Save each PATCH the whole row, so one mustn't start while the other is in
  // flight: a Save sent before the switch's change landed would carry the old `enabled` back.
  const [enableSaving, setEnableSaving] = useState(false);

  // What every kind switch starts from (`kindSwitchBase`): the kind fields and name as loaded or
  // prefilled, plus the owner's hand edits the baseline's own kind shows (`baselineTakes`) — never a
  // switch's. So switching back undoes a switch, and a switch doesn't build on the one before.
  const [kindBaseline, setKindBaseline] = useState<Partial<CollectionInput>>(() =>
    baselineOf(input),
  );

  // Held apart from `input` on purpose — see the Name field. The saved value is the TEMPLATE, since
  // that is what a rename rewrites; `name` is only its rendered form.
  const savedName = collection?.name_template || collection?.name || "";
  const [renameDraft, setRenameDraft] = useState(savedName);
  const renamePending = renameDraft.trim() !== savedName.trim();
  // Drives only the note beside Run — Save is never gated on it, because a form that refuses to
  // save what it thinks is unchanged is unfixable when the comparison is the thing that is wrong.
  const unsaved = hasUnsavedChanges(input, savedRow);

  // A kind switch on a saved row waits here for the confirm dialog; the name it asked for waits in
  // `pendingRename` until Save sends it (the Name box edits it meanwhile), with the name the switch
  // was confirmed under and the placeholders the new name can't carry.
  const [pendingKind, setPendingKind] = useState<RowKindChoice | null>(null);
  const [pendingRename, setPendingRename] = useState<{
    name: string;
    confirmed: string;
    mustNotUse: string[];
  } | null>(null);
  const pendingProblem = pendingRename
    ? renameProblem(
        pendingRename.name,
        pendingRename.mustNotUse,
        pendingRename.confirmed,
      )
    : null;

  // The default row's title is the global template, which follows no season; the server refuses it,
  // so its catalogue is never needed.
  const seasonCatalogue = useSeasons(!isDefault);
  const kindCtx: RowKindContext = {
    isDefault,
    globalMaxSeeds: maxSeedsSeed(settings.data),
    defaultRowName: settingString(
      settings.data ?? {},
      "row.name_template",
      DEFAULT_ROW_NAME,
    ),
    globalSources: effectiveSources([], settings.data),
    seasonCatalogue: (seasonCatalogue.data ?? []).map((season) => season.slug),
    pausedAll: settings.data?.["paused_all"] === true,
  };
  // What the row will be once a pending rename lands. The kind is read from this, or a {top_seed}
  // row switched away from Because you watched would read straight back as one until the rename. It
  // carries the name the switch was confirmed under: a name typed in the Name box since is checked
  // (`pendingProblem`) and refused at Save, but never changes the kind on screen.
  const draft: CollectionInput = pendingRename
    ? {
        ...input,
        name: pendingRename.confirmed,
        name_template: pendingRename.confirmed,
      }
    : input;

  // Every control writes through here. A kind switch doesn't: it calls `setInput` itself, so the
  // baseline only takes what the owner changed by hand in a setting its own kind shows. A patch is one
  // edit, taken whole or not at all, so a side effect goes with the setting that made it: a blend
  // chosen under Based on stops taking turns, and that belongs to Because you watched, not to a Watch
  // it again row the owner switched from.
  const set = (patch: Partial<CollectionInput>) => {
    setInput((prev) => ({ ...prev, ...patch }));
    const baselineRow = { ...input, ...kindBaseline };
    const fields = BASELINE_FIELDS.filter((field) => field in patch);
    const taken =
      fields.length > 0 &&
      fields.every((field) =>
        baselineTakes(field, draft, baselineRow, kindCtx),
      );
    if (taken) {
      setKindBaseline((prev) => ({
        ...prev,
        ...Object.fromEntries(fields.map((field) => [field, patch[field]])),
      }));
    }
  };
  const current = rowKindOf(draft, kindCtx);
  const shown = visibleSettings(draft, kindCtx);
  const hidden = hiddenButRead(draft, kindCtx);
  // Whether the engine forces this row to a nightly cadence — which it does for a row that FOLLOWS
  // a watch, by name or by cycling.
  const followsAWatch = rowFollowsAWatch(draft, kindCtx);
  const isSeasonal = input.seasons.length > 0;
  const chosenSeasons = (seasonCatalogue.data ?? []).filter((season) =>
    input.seasons.includes(season.slug),
  );
  const readiness = requestReadiness(settings.data);
  const requestsEnabled = settings.data?.["requests.enabled"] === true;

  const describeSwitch = (choice: RowKindChoice, renameTo: string | null = null) =>
    describeKindChange(draft, choice, kindCtx, {
      baseline: kindBaseline,
      saved,
      renameTo,
    });
  const switched = (choice: RowKindChoice) =>
    normalizeKindResult(
      applyRowKind(
        kindSwitchBase(draft, kindBaseline, choice, kindCtx),
        choice,
        kindCtx,
      ),
    );

  const applyKind = (choice: RowKindChoice, renameTo: string | null) => {
    // The switch starts from the baseline's name, so a saved row keeps its saved one in the form.
    setInput(switched(choice));
    // Exactly this switch's rename, or none: an earlier switch's rename never outlives it.
    setPendingRename(
      renameTo && renameTo.trim() !== savedName.trim()
        ? {
            name: renameTo,
            confirmed: renameTo.trim(),
            mustNotUse: describeSwitch(choice).rename?.mustNotUse ?? [],
          }
        : null,
    );
  };

  const requestKind = (choice: RowKindChoice) => {
    const same =
      choice.kind === current.kind &&
      (choice.kind !== "seasonal" || choice.fill === current.fill);
    if (same) return;
    if (collection) {
      setPendingKind(choice);
      return;
    }
    // A new row has no rename screen to wait for — its name is part of this form — so a name the
    // new kind can't keep is replaced here, or the row would read straight back as its old kind (or,
    // with the season in it, be refused).
    const rename = describeSwitch(choice).rename;
    const next = switched(choice);
    setInput(
      rename?.required
        ? {
            ...next,
            name: rename.proposed,
            ...(next.name_template ? { name_template: rename.proposed } : {}),
          }
        : next,
    );
  };

  // Seasonal is filled the way the row on screen is: the owner makes what they see seasonal.
  const pickKind = (kind: RowKind) =>
    requestKind({ kind, fill: kind === "seasonal" ? current.fill : kind });

  // What each folded section says about itself while closed. A disclosure that hides both its
  // controls AND what they are currently set to is worse than the flat list it replaced — these are
  // what let someone skip a section rather than open it to find out they didn't need it.
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
    input.max_seeds === null
      ? null
      : `${input.max_seeds} watch${input.max_seeds === 1 ? "" : "es"}`,
    input.seed_window > 1 ? `cycling ${input.seed_window}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const updatesSummary = [
    input.schedule.trim() ? "On its own schedule" : "Only when you run it",
    // A followed-watch row's stored cadence is ignored by the engine, so reporting it here would
    // describe a cadence the row does not run on.
    followsAWatch
      ? "rebuilds nightly"
      : input.refresh_days === null
        ? "default cadence"
        : input.refresh_days === 1
          ? "rebuilds nightly"
          : input.refresh_days <= 0
            ? "frozen"
            : `rebuilds every ${input.refresh_days} days`,
  ].join(" · ");
  const placementSummary = (() => {
    const mine = input.placement === "off" ? 0 : 1;
    const theirs = input.placement_friends === "off" ? 0 : 1;
    if (mine && theirs) return "You and everyone else";
    if (theirs) return "Everyone else only";
    if (mine) return "You only";
    return "Hidden from every shelf";
  })();
  // The same words as the preview's Requests line: what this row will ask for.
  const requestSummary = requestsSummary(draft, settings.data, {
    shared: !shown.has("requests"),
  });

  const submit = () => {
    // Keep 'Top', 'off', and real anchors — a row slug or a collection title. Drop a half-set library
    // (mode chosen, nothing picked yet) so it falls back to the default rather than being POSTed as an
    // empty anchor, which the API rejects. Also drop the empty twin of whichever kind was chosen: the
    // API refuses a body carrying both, since the engine reads `row` first and would silently ignore
    // the other.
    //
    // 'off' has to survive the filter even though it names no anchor: it is a real choice ("never
    // position this row"), and dropping it would silently restore the default, which is the top.
    const hub_anchor = Object.fromEntries(
      Object.entries(input.hub_anchor)
        .filter(
          ([, entry]) =>
            entry.enabled === false ||
            entry.top ||
            (entry.row ?? "").trim() ||
            (entry.anchor ?? "").trim(),
        )
        .map(([key, entry]) => {
          if (entry.enabled === false) return [key, { enabled: false }];
          if (entry.top) return [key, { top: true }];
          if ((entry.row ?? "").trim())
            return [key, { row: entry.row, before: Boolean(entry.before) }];
          return [key, { anchor: entry.anchor, before: Boolean(entry.before) }];
        }),
    );
    // A name a kind switch asked for (confirmed in its dialog, maybe edited in the Name box since —
    // never one merely typed there without a switch) is saved in this same PATCH: the switch may be
    // refused under the old name (a season name on a row that no longer follows seasons). Where it
    // reaches Plex is `renameAt`: only the rename screen's case navigates there, streaming from the
    // title the collections still carry. A build flip deletes them at save, so that PATCH renames
    // nothing itself; every other case defers the Plex rename. A failed save leaves the rename
    // pending, so it can be fixed in the Name box and saved again.
    const renameTo = saved && pendingRename ? pendingRename.name.trim() : null;
    const at = saved && renameTo ? renameAt(saved, input, renameTo) : null;
    const oldTemplate = savedName;
    save.mutate(
      {
        id: collection?.id ?? null,
        body: {
          ...input,
          hub_anchor,
          ...(renameTo
            ? {
                name: renameTo,
                name_template: renameTo,
                defer_rename: at !== "rebuild",
              }
            : {}),
        },
      },
      {
        onSuccess: () => {
          onClose();
          if (renameTo && at === "rename_screen") {
            onRename?.(renameTo, { oldTemplate });
          }
        },
      },
    );
  };

  return (
    // A PAGE, not a dialog. A modal caps at 90% of the viewport, and it was that height limit — not
    // the number of settings — that forced every group into a collapsed accordion. Which in turn hid
    // the one warning that stops a movies-and-TV row building half empty, since it lived inside a
    // section that starts closed. With the cap gone the groups can stay open, warnings can sit
    // permanently beside the setting they concern, and there is room for the preview panel that
    // turns each abstract setting into "here is what Sarah will see tonight".
    <div className="w-full space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <div>
          <h1 className="text-2xl font-semibold">
            {collection ? "Edit row" : "Add a row"}
          </h1>
        </div>

        {/* What you reach for repeatedly while tuning a row: turn it on or off, rebuild it, and look
            at what the last rebuild did. The toggle used to be on the Rows card alone — this page's
            own copy sent you back there for it, which is a page change to do the one reversible
            thing to the row you already have open.
            Removing and deleting are still NOT up here. They reach into other people's Plex and are
            not undone by Cancel, so they stay fenced off at the bottom rather than sitting one pixel
            from "rebuild this row" — but fenced off had become invisible, so the link below says
            they exist and takes you to them. */}
        {collection && (
          <div className="flex flex-wrap items-center justify-end gap-2">
            <div data-setting="enabled" className="contents">
              <RowEnableToggle
                collection={savedRow ?? collection}
                showLabel
                disabled={save.isPending}
                onSaving={setEnableSaving}
                onSaved={(enabled) => {
                  setSavedEnabled(enabled);
                  setInput((prev) => ({ ...prev, enabled }));
                }}
              />
            </div>
            <RowRunAction collection={collection} variant="outline" />
            <Button asChild variant="outline" size="sm">
              <Link to={`/runs?row=${encodeURIComponent(collection.slug)}`}>
                <ListChecks aria-hidden="true" />
                Runs
              </Link>
            </Button>
            {/* Reads as the destructive control it points at — the Rows card's own "Remove or
                delete" is the same red with the same icon, and a plain grey label here did not look
                like a button at all, let alone one that ends in deleting a row. */}
            <Button
              variant="outline"
              size="sm"
              className="border-destructive/40 text-destructive-text hover:bg-destructive/10 hover:text-destructive-text"
              onClick={() =>
                document
                  .getElementById("remove-this-row")
                  ?.scrollIntoView({ behavior: "smooth", block: "center" })
              }
            >
              <Trash2 aria-hidden="true" />
              Remove or delete
            </Button>
          </div>
        )}
      </div>

      {/* Run rebuilds the row AS SAVED, which is a trap next to a form you have been editing —
          nothing about the button says the changes on screen won't be in it. */}
      {collection && unsaved && (
        <p className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-foreground">
          You have unsaved changes. Running now rebuilds this row as it was last
          saved — press <strong>Save changes</strong> first if you want these
          settings in it.
        </p>
      )}

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

      {/* Across the top, like the dashboard's Impact strip — not down in the sidebar.
          It lived at the BOTTOM of the sticky sidebar, which has its own scroller, so "is this row
          working?" was hidden inside a second scroll area most people never noticed: the page looked
          finished while the numbers sat below the fold of a column they had no reason to scroll.
          It is also the one thing here that is not a setting — it is the outcome the settings are
          for — so it belongs above them rather than beside them.
          Saved rows only: a row being created has no history, and a strip of dashes answers nothing. */}
      {collection && (
        <RowEffectivenessPanel
          data={effectiveness.data}
          isLoading={effectiveness.isLoading}
          isError={effectiveness.isError}
          onRetry={() => effectiveness.refetch()}
          rowSlug={collection.slug}
        />
      )}

      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
        {/* `min-w-0`, because a grid item's default `min-width: auto` resolves to its MIN-CONTENT
            width. The `minmax(0,1fr)` above only covers `lg` and up; below it the single implicit
            column took its floor from the widest unbreakable thing inside — measured at 380px in a
            320px viewport, so the whole page scrolled sideways and every heading and paragraph on
            it ran past the right edge. Same fix, and the same reason, as the dashboard's cards. */}
        <div className="min-w-0 space-y-5">
          {/* Names the left column, so the page reads as three labelled parts — how it is doing,
              what you can change, what that will produce — instead of an unlabelled wall of cards
              with a panel floating beside it. */}
          <h2 className="text-base font-semibold">Row settings</h2>
          <SettingsGroup
            title="How it looks on Plex"
            description="The name, description and picture people see."
          >
            {/* Everything someone SEES of the row, together and first. The description and poster used
                to be folded groups near the bottom, three groups away from the name they describe. */}
            <div className="grid gap-6 sm:grid-cols-[minmax(0,1fr)_11rem]">
              <div className="min-w-0 space-y-4">
                <div data-setting="name" className="space-y-2">
                  <Label htmlFor="row-name">Name</Label>
                  {collection ? (
                    // Type here, but this is NOT part of the form: `renameDraft` is deliberately held
                    // apart from `input`, so Save can never carry a new name. Saving the name without
                    // renaming on Plex would leave the two disagreeing, with nothing on screen saying
                    // so. Applying it rewrites the collection for every person who has the row, one at
                    // a time with progress, which is why the work itself stays on its own screen.
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        {/* While a kind switch's rename is pending, this box IS that rename: Save
                            sends it, so it can be fixed here after a refused save. */}
                        <Input
                          id="row-name"
                          value={pendingRename ? pendingRename.name : renameDraft}
                          onChange={(e) =>
                            pendingRename
                              ? setPendingRename({
                                  ...pendingRename,
                                  name: e.target.value,
                                })
                              : setRenameDraft(e.target.value)
                          }
                          className="min-w-0 flex-1"
                        />
                        <Button
                          type="button"
                          variant={renamePending ? "default" : "outline"}
                          size="sm"
                          // Only once the name actually differs from the saved one. Enabled on an
                          // unchanged name it offered to rewrite every collection on Plex, for every
                          // person, to the name they already had — minutes of writes for no change.
                          disabled={
                            enableSaving ||
                            pendingRename !== null ||
                            !renamePending ||
                            !renameDraft.trim()
                          }
                          onClick={() => {
                            onClose();
                            onRename?.(renameDraft.trim());
                          }}
                        >
                          Rename…
                        </Button>
                      </div>
                      {/* The same placeholder list a new row's name box shows. Renaming is where a
                          name is actually typed for an existing row, and without it the box read as
                          plain text — nothing said {user} or {library_name} would work here. */}
                      <TemplateVarsHint seasonal={isSeasonal} />
                      {pendingRename ? (
                        <>
                          {pendingProblem && (
                            <p
                              role="alert"
                              className="text-sm text-destructive-text"
                            >
                              {pendingProblem}
                            </p>
                          )}
                          <p className="text-sm text-warning">
                            To match its new kind, saving renames the row to
                            this.{" "}
                            {saved &&
                              renameAtNote(
                                renameAt(
                                  saved,
                                  input,
                                  pendingRename.name.trim(),
                                ),
                                kindCtx,
                              )}
                          </p>
                        </>
                      ) : renamePending ? (
                        <p role="status" className="text-sm text-warning">
                          Not applied yet &mdash; press <strong>Rename</strong> to
                          change it on Plex. Saving this page won&rsquo;t.
                        </p>
                      ) : (
                        <p className="text-sm text-muted-foreground">
                          Renaming rewrites this row on Plex for everyone who has
                          it, so it gets its own screen. Nothing else here is
                          touched.
                        </p>
                      )}
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
                      <TemplateVarsHint seasonal={isSeasonal} />
                    </>
                  )}
                </div>

                <div data-setting="description">
                  <RowDescriptionField
                    value={input.description}
                    onChange={(description) => set({ description })}
                  />
                </div>
                <div data-setting="poster">
                  <PosterField
                    value={input.poster}
                    onChange={(poster) => set({ poster })}
                    collectionId={collection?.id ?? null}
                    hasImage={collection?.poster?.has_image ?? false}
                    seasonal={isSeasonal}
                  />
                </div>
              </div>
              <RowPlexCard
                // The name as typed: the card previews what the row will be called.
                input={
                  pendingRename
                    ? {
                        ...draft,
                        name: pendingRename.name.trim(),
                        name_template: pendingRename.name.trim(),
                      }
                    : draft
                }
                collectionId={collection?.id ?? null}
                hasImage={collection?.poster?.has_image ?? false}
                sampleSeason={chosenSeasons[0]}
              />
            </div>
          </SettingsGroup>

          {/* Directly under what people see: the kind decides every setting below it (design §3). */}
          <SettingsGroup
            title={KIND_GROUP.title}
            description={KIND_GROUP.description}
            summary={kindTitle(current)}
          >
            <div data-setting="kind">
              <RowKindPicker
                value={current.kind}
                onChange={pickKind}
                // The row's own kind is never disabled, even before the season list loads.
                disabledReason={(kind) => {
                  if (kind === current.kind) return null;
                  const reason = kindDisabledReason(kind, draft, kindCtx);
                  if (reason !== SEED_NAME_IN_SETTINGS) return reason;
                  return (
                    <>
                      {reason}{" "}
                      <Link
                        to={DEFAULT_ROW_NAME_SETTINGS}
                        className="not-italic underline underline-offset-2 hover:text-foreground"
                      >
                        Settings › Row defaults
                      </Link>
                    </>
                  );
                }}
              />
            </div>
            <RowKindSettings
              choice={current}
              onChooseFill={(fill) => requestKind({ kind: "seasonal", fill })}
              input={draft}
              set={set}
              ctx={kindCtx}
              shown={shown}
              hidden={hidden}
              settings={settings.data}
              users={users}
              // Only while the form still matches what is saved: the status describes the SAVED row.
              seasonStatus={
                collection &&
                JSON.stringify(collection.seasons ?? []) ===
                  JSON.stringify(input.seasons) &&
                collection.season_lead_days === input.season_lead_days &&
                collection.season_after_days === input.season_after_days
                  ? (collection.season_status ?? null)
                  : null
              }
            />
          </SettingsGroup>

          <SettingsGroup
            title="Who gets it"
            description="Which people get this row."
          >
            <div data-setting="audience">
              <AudiencePicker
                audience={input.audience}
                audienceUserIds={input.audience_user_ids}
                users={users}
                onChange={set}
              />
            </div>
          </SettingsGroup>

          <SettingsGroup
            title="What goes in it"
            description="Where titles come from, how many, and in what order."
            summary={drawsOnSummary}
          >
            <div data-setting="libraries">
              <LibraryPicker
                libraryKeys={input.library_keys}
                media={input.media}
                onChange={(next) =>
                  set({
                    ...next,
                    // `media` is DERIVED from the libraries picked, so a row can narrow to movies-only
                    // without anyone touching the flag. Its control is hidden then, and the API refuses
                    // that one combination — a save failing with no visible cause.
                    //
                    // `=== "movie"`, matching the API exactly. It used to clear on anything that wasn't
                    // shows-only, which silently switched the flag off when a row widened to "films and
                    // shows" — a combination both the API and the engine accept.
                    ...(next.media === "movie" ? { unstarted_only: false } : {}),
                  })
                }
              />
            </div>

            {shown.has("size") && (
              <div data-setting="size" className="border-t pt-4">
                <RowSizeField
                  value={input.size}
                  onChange={(size) => set({ size })}
                />
              </div>
            )}
            <div data-setting="pick_order" className="space-y-2 border-t pt-4">
              <Label>What order the titles appear in</Label>
              <Segmented
                value={input.pick_order}
                onChange={(pick_order) => set({ pick_order })}
                ariaLabel="How the titles in this row are ordered"
                options={[
                  { value: "best", label: "Best match" },
                  { value: "rating", label: "Highest rated" },
                  // "Newest released", not "Newest": it sits two chips from "Just added", and the
                  // two mean different things — when a film came out, vs when it joined this row.
                  { value: "newest", label: "Newest released" },
                  { value: "shuffle", label: "Shuffled" },
                  { value: "new_first", label: "Just added" },
                  { value: "rotate", label: "Taking turns" },
                ]}
              />
              <p className="text-sm text-muted-foreground">
                {pickOrderHelp(input.pick_order, ratingLabel)}
              </p>
              {/* The score to sort on is chosen HERE, not in Settings. "Highest rated" raises the
                question "rated by whom?" at exactly this moment, and answering it by sending someone
                to another screen is how the setting stayed undiscovered. It is still one server-wide
                value, so the note says so rather than implying it is per-row. */}
              {shown.has("rated_by") && (
                <div
                  data-setting="rated_by"
                  className="space-y-1.5 rounded-md border bg-muted/30 p-3"
                >
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
                      ? "TMDB scores need no setup. IMDb, Trakt, Rotten Tomatoes and Metacritic all come from MDBList, a free service that fetches every site’s score in one lookup — add its key under Settings → Connections."
                      : `Scores come from MDBList, a free service that fetches every site’s score in one lookup. Add its key under Settings → Connections, or ${ratingLabel} rows quietly fall back to TMDB.`}{" "}
                    Shared by every row and by requests: changing it here
                    changes it everywhere.
                  </p>
                </div>
              )}
            </div>

            <RowContentsFields
              input={draft}
              set={set}
              shown={shown}
              settings={settings.data}
              fill={current.fill}
              takeTurns={
                current.fill === "again" && (
                  <TakeTurns
                    fill="again"
                    input={draft}
                    set={set}
                    ctx={kindCtx}
                    shown={shown}
                    hidden={hidden}
                    settings={settings.data}
                  />
                )
              }
            />
          </SettingsGroup>

          <SettingsGroup
            title="When it updates"
            description="When Shortlist rebuilds the row, and how often its titles change."
            summary={updatesSummary}
          >
            <div data-setting="schedule">
              <RowScheduleField
                value={input.schedule}
                onChange={(schedule) => set({ schedule })}
              />
            </div>

            {/* A row that follows a watch has its cadence forced to nightly by the engine
                (`effective_refresh_days`), so there is nothing to choose — one line says so. A
                shared row rebuilds every run regardless and gets nothing at all. */}
            {shown.has("refresh_days") ? (
              <InheritableField
                setting="refresh_days"
                label="How often it changes"
                labelFor="row-refresh-days"
                description="How often this row swaps some of its titles for new ones."
                ariaLabel="Use the global rebuild cadence"
                inheriting={input.refresh_days === null}
                globalValue={refreshDaysGlobal(settings.data)}
                onToggle={(on) =>
                  set({
                    refresh_days: on ? null : refreshDaysSeed(settings.data),
                  })
                }
              >
                <RefreshDaysField
                  id="row-refresh-days"
                  value={input.refresh_days ?? 0}
                  onChange={(days) => set({ refresh_days: days })}
                />
              </InheritableField>
            ) : (
              followsAWatch &&
              current.fill !== "popular" && (
                <p className="border-t pt-4 text-sm text-muted-foreground">
                  Changes every night: a row that follows a watch always picks
                  new titles nightly, whatever the global default says.
                </p>
              )
            )}

            {/* Matches `effective_idle_hold_days` exactly: the engine refuses to hold a row that
                CYCLES its seed (the rotation is driven by the cadence, not by watches, so holding
                stops the feature rather than delaying it), but it does hold a `{top_seed}` row —
                only ADDING a watch moves its seed forward, which an idle person by definition has
                not done. An editor that hides a control the engine is still applying is the bug the
                cadence field shipped once already (issue #57). A shared row has no single owner
                whose watching could be idle. */}
            {shown.has("idle_hold_days") && (
              <InheritableField
                setting="idle_hold_days"
                label="Hold when they aren't watching"
                labelFor="row-idle-hold-days"
                description="How long this row waits when the person it belongs to hasn't watched anything since it was built."
                ariaLabel="Use the global hold for inactive viewers"
                inheriting={input.idle_hold_days === null}
                globalValue={idleHoldGlobal(settings.data)}
                onToggle={(on) =>
                  set({
                    idle_hold_days: on ? null : idleHoldSeed(settings.data),
                  })
                }
              >
                <IdleHoldField
                  id="row-idle-hold-days"
                  value={input.idle_hold_days ?? 0}
                  // The EFFECTIVE cadence, not the stored one. `effective_refresh_days` forces 1
                  // for a row that follows a watch, and that forcing is exactly why a hold works
                  // there: an 8-day hold on a nightly row is a real 7-night hold.
                  cadence={
                    followsAWatch
                      ? 1
                      : (input.refresh_days ??
                        refreshDaysGlobalValue(settings.data) ??
                        undefined)
                  }
                  onChange={(days) => set({ idle_hold_days: days })}
                />
              </InheritableField>
            )}
          </SettingsGroup>

          <SettingsGroup
            title="Where and when people see it"
            description="Which Plex screens it shows on, where it sits, and on which days."
            summary={`${placementSummary} · ${showDaysSummary(input.show_days)}`}
          >
            <div className="space-y-3">
              <div data-setting="placement" className="space-y-3">
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
              </div>
              <div data-setting="hub_anchor" className="space-y-2 pt-2">
                <span className="text-sm font-medium">
                  Position in the Recommended shelf
                </span>
                <p className="text-sm text-muted-foreground">
                  Where this row sits on the shelf: the default from Settings →
                  Row placement, the <strong>Top</strong>, or beside one of your
                  own collections.
                </p>
                <RowShelfPlacement
                  value={input.hub_anchor}
                  libraryKeys={input.library_keys}
                  media={input.media}
                  rowSlug={collection?.slug}
                  pinnedTop={input.pin_top}
                  onConsumePin={() => set({ pin_top: false })}
                  onChange={(hub_anchor) => set({ hub_anchor })}
                />
              </div>
            </div>
            <div data-setting="show_days" className="border-t pt-4">
              <RowShowDaysField
                value={input.show_days}
                onChange={(show_days) => set({ show_days })}
              />
            </div>
            <div data-setting="sort_title_prefix">
              <RowSortPrefixField
                value={input.sort_title_prefix}
                rowName={input.name_template || input.name}
                media={input.media}
                onChange={(sort_title_prefix) => set({ sort_title_prefix })}
              />
            </div>
          </SettingsGroup>

          <SettingsGroup
            title="Requests"
            description="What this row asks Sonarr and Radarr for when a pick isn't on the server yet, and where those titles land."
            summary={requestSummary}
            defaultOpen={false}
          >
            {shown.has("requests") ? (
              <div data-setting="requests" className="space-y-4">
                <RowRequestSettings
                  input={input}
                  set={set}
                  settings={settings.data}
                  requestsEnabled={requestsEnabled}
                  target={readiness.target}
                  radarrReady={readiness.radarrReady}
                  sonarrReady={readiness.sonarrReady}
                  media={input.media}
                  audienceSize={audienceSize(input, users)}
                />
              </div>
            ) : (
              // A shared row is built from titles people have already WATCHED, which are by
              // definition on the server, so it can never surface a missing title to request.
              <p className="text-sm text-muted-foreground">
                A shared row never asks for missing titles: everything in it is
                something people here have already watched.
              </p>
            )}
          </SettingsGroup>

          {save.isError && (
            <p role="alert" className="text-sm text-destructive-text">
              {apiErrorMessage(
                save.error,
                "Couldn’t save this row. Try again.",
              )}
            </p>
          )}

          {/* Last, and fenced off: these two reach into Plex, and neither is undone by Cancel. A row
              being created has nothing to remove yet. */}
          {collection && (
            <div className="space-y-3 rounded-lg border border-destructive/30 p-5">
              <div className="space-y-1">
                <h2
                  id="remove-this-row"
                  className="text-base font-semibold text-destructive-text"
                >
                  Remove this row
                </h2>
                <p className="text-sm text-muted-foreground">
                  Taking it off Plex keeps its settings here, so the next run
                  builds it again. Deleting it doesn&rsquo;t. Either way the
                  titles stay in your library. To stop it coming back, use the
                  on/off switch at the top of this page instead.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <RowDestructiveActions
                  collection={collection}
                  onDeleted={onClose}
                />
              </div>
            </div>
          )}
        </div>

        {/* Sticky so it stays beside whichever setting is being changed — the point is watching the
            outcome move as you touch things, which it cannot do if it scrolls off.
            The max-height + scroll is a safety valve for a very tall preview on a short window, not
            a place to put content: anything parked below the fold here is effectively invisible,
            which is exactly why the effectiveness panel moved to the top of the page. */}
        <aside ref={summaryRef} style={{ top: summaryTop }} className="min-w-0 space-y-5 lg:sticky">
          {/* Outside the card, matching "Row settings" opposite, so the two columns start level. */}
          <div>
            <h2 className="text-base font-semibold">What this row will do</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Updates as you change things. Nothing is saved until you press
              save.
            </p>
          </div>
          <RowPreview
            input={draft}
            ctx={kindCtx}
            // The header's switch saves straight away, so the saved row says whether it is on.
            enabled={saved?.enabled ?? input.enabled}
            users={users}
            libraries={libraries.data ?? []}
            settings={settings.data}
            seasons={chosenSeasons}
            rowNames={rowNames}
          />
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
          disabled={
            !input.name.trim() || pendingProblem !== null || enableSaving
          }
        >
          {collection ? "Save changes" : "Add row"}
        </Button>
      </div>

      {pendingKind && (
        <RowKindChangeDialog
          describe={(renameTo) => describeSwitch(pendingKind, renameTo)}
          onCancel={() => setPendingKind(null)}
          onConfirm={(renameTo) => {
            applyKind(pendingKind, renameTo);
            setPendingKind(null);
          }}
        />
      )}
    </div>
  );
}
