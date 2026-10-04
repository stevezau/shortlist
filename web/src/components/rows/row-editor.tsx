import { ListChecks, Lock } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { PageHeader } from "@/components/page-header";
import { AiPromptsSection } from "@/components/rows/ai-prompts-section";
import { AiRowSection } from "@/components/rows/ai-row-section";
import { AiTryIt } from "@/components/rows/ai-try-it";
import { RowRequestSettings } from "@/components/rows/row-request-settings";
import { AudiencePicker } from "@/components/rows/audience-picker";
import { InheritableField } from "@/components/rows/inheritable-field";
import { LibraryPicker } from "@/components/rows/library-picker";
import { PlacementToggles } from "@/components/rows/placement-toggles";
import { PosterField } from "@/components/rows/poster-field";
import { RowAudienceTable } from "@/components/rows/row-audience-table";
import { RowContentsFields } from "@/components/rows/row-contents-fields";
import { draftChanges } from "@/components/rows/row-draft-diff";
import { mediaLabel, rowLibraries, rowReach } from "@/components/rows/row-facts";
import { RowKindChangeDialog } from "@/components/rows/row-kind-change-dialog";
import { RowBuildPicker, RowKindPicker } from "@/components/rows/row-kind-picker";
import { RowLiveStrip } from "@/components/rows/row-live-strip";
import { RowName } from "@/components/rows/row-name";
import { RowKindSettings, TakeTurns } from "@/components/rows/row-kind-settings";
import { RowPlexCard } from "@/components/rows/row-plex-card";
import {
  RowDescriptionField,
  RowSortPrefixField,
} from "@/components/rows/row-plex-details-field";
import { RowRenameDialog } from "@/components/rows/row-rename-dialog";
import { RowSaveBar } from "@/components/rows/row-save-bar";
import { RowScheduleField } from "@/components/rows/row-schedule-field";
import { RowShowDaysField } from "@/components/rows/row-show-days-field";
import { RowDestructiveActions } from "@/components/rows/row-destructive-actions";
import { RowSectionNavigation, type RowSection } from "@/components/rows/row-section-navigation";
import { RowShelfPlacement } from "@/components/rows/row-shelf-placement";
import { effectiveSources } from "@/components/rows/row-sources-field";
import { TemplateVarsHint } from "@/components/rows/template-vars-hint";
import { Segmented } from "@/components/segmented";
import { RefreshDaysField } from "@/components/settings/refresh-days-field";
import { IdleHoldField } from "@/components/settings/idle-hold-field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { RowSizeField } from "@/components/row-size-field";
import { apiErrorMessage } from "@/lib/api";
import { blankInput, hasUnsavedChanges, toInput } from "@/lib/collections";
import { describeCron } from "@/lib/cron";
import { settingString } from "@/lib/format";
import {
  useCollectionEffectiveness,
  useLibraries,
  usePrivacyStatus,
  useSaveCollection,
  useSaveSettings,
  useSchedule,
  useSeasons,
  useSettings,
} from "@/lib/queries";
import { requestReadiness } from "@/lib/requests";
import {
  AI_KIND_META,
  aiRowSettings,
  applyRowKind,
  BASELINE_FIELDS,
  baselineTakes,
  DEFAULT_ROW_NAME_SETTINGS,
  describeKindChange,
  FILL_META,
  followsAWatch as rowFollowsAWatch,
  hiddenButRead,
  isAiRow,
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
  withoutHiddenInstructions,
  type RowKind,
  type RowKindChoice,
  type RowKindContext,
  type RowFill,
} from "@/lib/row-kinds";
import { sentenceCaseHighlights, type RowTemplate } from "@/lib/row-templates";
import { selectsNothing, toSaveBody, useSaveTheme, type PendingTheme } from "@/lib/themes";
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

function kindTitle(choice: RowKindChoice): string {
  return choice.kind === "seasonal"
    ? `${KIND_META.seasonal.title} · ${FILL_META[choice.fill].title}`
    : KIND_META[choice.kind].title;
}

/** One section of the editor: a heading the jump list links to, a line on what it decides, and
 *  its settings in a single card (never a card inside a card). */
function EditorSection({
  id,
  title,
  description,
  panelId,
  danger = false,
  children,
}: {
  id: string;
  title: string;
  description: string;
  /** An id for the card itself, so an older link to it still lands. */
  panelId?: string;
  danger?: boolean;
  children: ReactNode;
}) {
  return (
    <section id={id} aria-labelledby={`${id}-heading`} className="scroll-mt-32 space-y-3 lg:scroll-mt-6">
      <div className="space-y-1">
        <h2 id={`${id}-heading`} tabIndex={-1} className="text-base font-semibold focus:outline-none">
          {title}
        </h2>
        <p className="max-w-prose text-sm text-muted-foreground">{description}</p>
      </div>
      <div
        id={panelId}
        className={
          danger
            ? "scroll-mt-32 rounded-xl border border-destructive/40 bg-card shadow-elevated lg:scroll-mt-6"
            : "space-y-4 rounded-xl border bg-card p-5 shadow-elevated"
        }
      >
        {children}
      </div>
    </section>
  );
}

/** "today at 02:30", "tomorrow at 02:30", "Sun 5 Oct at 02:30": a run time in the reader's own clock. */
function runTime(iso: string, now: Date = new Date()): string {
  const when = new Date(iso);
  const time = when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  if (when.toDateString() === now.toDateString()) return `today at ${time}`;
  if (when.toDateString() === tomorrow.toDateString()) return `tomorrow at ${time}`;
  return `${when.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} at ${time}`;
}

/** When the row next runs. The scheduler's own answer when the schedule on screen is the saved one;
 *  otherwise the cron in words, since nothing is scheduled for a schedule that isn't saved yet. */
function nextRunWords(cron: string, nextRun: string | null): string {
  if (!cron.trim()) return "It only runs when you press Run now.";
  if (nextRun && !Number.isNaN(Date.parse(nextRun))) return `It runs ${runTime(nextRun)}.`;
  const words = describeCron(cron);
  return words ? `It runs ${words.charAt(0).toLowerCase()}${words.slice(1)}, Shortlist's clock.` : "It runs on its own schedule.";
}

/** How often the titles change, from the same cadence the engine uses (`effective_refresh_days`). */
function titlesChangeWords({
  followsAWatch,
  hasCadence,
  cadence,
  inheriting,
}: {
  followsAWatch: boolean;
  hasCadence: boolean;
  cadence: number | null;
  inheriting: boolean;
}): string {
  if (followsAWatch) return "Its titles change every night, because it follows their latest watch.";
  if (!hasCadence) return "Its titles are picked again every time it runs.";
  const from = inheriting ? " (the global default)" : "";
  if (cadence === null) return "Its titles change as often as the global default says.";
  if (cadence <= 0) return `Its titles never change once it is built${from}.`;
  if (cadence === 1) return `Its titles change every night${from}.`;
  return `Its titles change on a cycle of ${cadence} days${from}.`;
}

export function RowEditor({
  collection,
  template = null,
  users,
  audienceState = "ready",
  onRetryAudience,
  onClose,
  onRename,
}: {
  collection: Collection | null;
  /** Seeds a NEW row's fields. Never set when editing an existing row, and every field it fills
   *  stays editable — a template is a starting point, not a mode. */
  template?: RowTemplate | null;
  users: User[];
  audienceState?: "loading" | "error" | "ready";
  onRetryAudience?: () => void;
  onClose: () => void;
  /** Hands a name to the rename screen, which owns the Plex work: the name typed in Rename on
   *  Plex…, or — with `saved` — the one a kind switch proposed, which the save already carried, so
   *  the screen only streams the rename from the title the collections still carry. */
  onRename?: (proposedName: string, saved?: { oldTemplate: string }) => void;
}) {
  const save = useSaveCollection();
  const saveSettings = useSaveSettings();
  // Read-only here: the editor never writes settings, it only names the globals a row inherits.
  const settings = useSettings();
  const libraries = useLibraries();
  const effectiveness = useCollectionEffectiveness(collection?.id ?? null);
  // Shared with the nav rail's Privacy dot (same query key), so it costs no extra plex.tv read.
  const privacy = usePrivacyStatus();
  const schedule = useSchedule();
  const ratingSource = asRatingSource(
    settings.data?.["recommendations.rating_source"],
  );
  const ratingLabel = RATING_LABELS[ratingSource];
  const [input, setInput] = useState<CollectionInput>(
    collection
      ? toInput(collection)
      : { ...blankInput(), ...(template?.values ?? {}) },
  );
  // An AI row (#138) is decided when the editor opens and never changes: a saved row follows a theme,
  // and a new one starts from the Describe a row template. It has no theme until its list is built.
  const [aiRow] = useState(() => isAiRow(input) || template?.kind === "ai");
  // The list built or edited here and not saved yet; it is saved, as a theme, with the row.
  const [pendingTheme, setPendingTheme] = useState<PendingTheme | null>(null);
  // Tokens this edit's AI calls have spent. Charged to the row and its theme when the list is saved.
  const [tokensSpent, setTokensSpent] = useState(0);
  // A new row's theme is saved before the row, so a row save that fails can't make a second theme.
  const [createdThemeId, setCreatedThemeId] = useState<number | null>(null);
  const saveTheme = useSaveTheme();
  // Bumped by Discard, to remount the fields: several hold state of their own seeded from the value
  // they were first given (the schedule's mode, the poster's upload), which a reset `input` alone
  // would leave showing the discarded edit.
  const [draftVersion, setDraftVersion] = useState(0);
  const isDefault = collection?.slug === "picked";

  // Live on Plex's on/off switch saves straight away, so what it saved is the saved row's `enabled`
  // from then on — and the form's, or Save would send back the value the page opened with and undo
  // it. Everything that asks whether the row is on (the notes, where a rename lands) reads it here.
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
  const lastPersonalFill = useRef<Exclude<RowFill, "popular"> | null>(null);

  // Held apart from `input` on purpose — see the Name field. The saved value is the TEMPLATE, since
  // that is what a rename rewrites; `name` is only its rendered form.
  const savedName = collection?.name_template || collection?.name || "";
  const [renameDraft, setRenameDraft] = useState(savedName);
  const [renameOpen, setRenameOpen] = useState(false);
  const [discardForRename, setDiscardForRename] = useState(false);
  const renamePending = renameDraft.trim() !== savedName.trim();
  // Whether a rename would throw away settings edits (it leaves the page). Save is never gated on
  // it, because a form that refuses to save what it thinks is unchanged is unfixable when the
  // comparison is the thing that is wrong.
  const unsaved = hasUnsavedChanges(input, savedRow) || pendingTheme !== null;

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
    builtinSeasons: (seasonCatalogue.data ?? []).filter((season) => season.builtin).map((season) => season.slug),
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
  // An AI row's titles come from its theme, so it shows its own short list of settings.
  const shown = aiRow ? aiRowSettings() : visibleSettings(draft, kindCtx);
  const hidden = aiRow ? [] : hiddenButRead(draft, kindCtx);
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
    if (current.fill !== "popular" && choice.fill === "popular") lastPersonalFill.current = current.fill;
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
    if (current.fill !== "popular" && choice.fill === "popular") lastPersonalFill.current = current.fill;
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

  // Seasonal is filled the way the row on screen is: the owner makes what they see seasonal. A
  // requests row can't be (the API refuses the pair), so from one Seasonal starts as Picked for You.
  const pickKind = (kind: RowKind) =>
    requestKind({
      kind,
      fill: kind !== "seasonal" ? kind : current.fill === "requests" ? "picked" : current.fill,
    });

  const pickBuild = (build: CollectionInput["build"]) => {
    if (build === input.build) return;
    const remembered = lastPersonalFill.current ?? "picked";
    const fill = build === "shared" ? "popular" : current.kind === "seasonal" && remembered === "requests" ? "picked" : remembered;
    requestKind({ kind: current.kind === "seasonal" ? "seasonal" : fill, fill });
  };

  const disabledKindReason = (kind: RowKind) => {
    if (kind === current.kind) return null;
    const reason = kindDisabledReason(kind, draft, kindCtx);
    if (reason !== SEED_NAME_IN_SETTINGS) return reason;
    return <>{reason}{" "}<Link to={DEFAULT_ROW_NAME_SETTINGS} className="not-italic underline underline-offset-2 hover:text-foreground">Settings › Row defaults</Link></>;
  };

  // "[]" means every library OF THIS ROW'S TYPE. Saying "every library" on a movies row
  // contradicted the picker right beside it, which ticks only the movie ones.
  const everyLibraryOfType =
    input.media === "movie" ? "every movie library" : input.media === "show" ? "every TV library" : "every library";
  const landsIn = libraries.data ? rowLibraries(input, libraries.data) : null;
  const reach = rowReach(input, users);
  const savedReach = savedRow ? rowReach(savedRow, users) : null;
  const changes = [
    ...draftChanges(input, savedRow, pendingRename ? { name: pendingRename.name, from: savedName } : null),
    ...(pendingTheme
      ? [{ label: "Its list of titles", to: pendingTheme.origin === "ai" ? "a new AI-written list" : "your edited list" }]
      : []),
  ];
  // A row with no list has nothing to pick from, and the API refuses a theme that selects nothing.
  const aiBlocked = !aiRow
    ? null
    : pendingTheme && selectsNothing(pendingTheme.draft)
      ? "Add at least one tag, genre or title to the list before saving."
      : !collection && !pendingTheme
        ? "Build the row’s list before adding it."
        : null;
  const scheduleGroup = collection
    ? schedule.data?.rows.find((group) => group.rows.some((row) => row.id === collection.id))
    : undefined;
  const nextRun =
    collection && input.schedule.trim() === (collection.schedule ?? "").trim()
      ? (scheduleGroup?.next_run ?? null)
      : null;
  const effectiveCadence = input.refresh_days ?? refreshDaysGlobalValue(settings.data);

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
    const saveRow = (themeId: number | null) => save.mutate(
      {
        id: collection?.id ?? null,
        body: {
          ...withoutHiddenInstructions(input, kindCtx),
          theme_id: themeId,
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

    if (!pendingTheme) {
      saveRow(input.theme_id);
      return;
    }
    // The theme first: the row can only follow one that exists. Its tokens are charged once, here.
    const themeId = collection?.theme_id ?? createdThemeId;
    saveTheme.mutate(
      {
        id: themeId,
        body: toSaveBody(pendingTheme, { tokens: tokensSpent, collectionId: collection?.id ?? null }),
      },
      {
        onSuccess: (theme) => {
          // The list stays on screen until the row is saved too: if that fails, the retry replaces this
          // theme instead of making another, and spends nothing more.
          setCreatedThemeId(theme.id);
          setTokensSpent(0);
          saveRow(theme.id);
        },
      },
    );
  };

  // Back to the row as saved, the way the page first opened it. The on/off switch's change is part
  // of the saved row by now, so it survives.
  const discard = () => {
    const back = savedRow ? toInput(savedRow) : input;
    setInput(back);
    setKindBaseline(baselineOf(back));
    lastPersonalFill.current = null;
    // Tokens stay counted: a discarded list was still paid for, and the next theme save charges it.
    setPendingTheme(null);
    setPendingKind(null);
    setPendingRename(null);
    setDraftVersion((version) => version + 1);
  };

  const openRename = () => {
    setRenameDraft(savedName);
    setRenameOpen(true);
  };
  const startRename = () => {
    setRenameOpen(false);
    if (unsaved) {
      setDiscardForRename(true);
      return;
    }
    onClose();
    onRename?.(renameDraft.trim());
  };

  const sections: RowSection[] = [
    { id: "name-and-look", label: "Name & look" },
    { id: "who-gets-it", label: "Who gets it" },
    { id: "what-goes-in", label: "What goes in" },
    ...(aiRow
      ? [
          { id: "try-it", label: "Try it" },
          { id: "ai-prompts", label: "AI prompts" },
        ]
      : []),
    { id: "schedule", label: "Schedule" },
    { id: "placement", label: "Placement" },
    // A Your requests row never searches, so it has nothing to ask for and nothing to set here, and
    // an empty section would only be somewhere to be wrong.
    ...(input.requests_row ? [] : [{ id: "requests", label: "Requests" }]),
    // A row being created has nothing on Plex to remove yet.
    ...(collection ? [{ id: "danger-zone", label: "Danger zone" }] : []),
  ];

  const subtitle = savedRow
    ? [
        savedRow.build === "shared" ? "Shared" : "Per person",
        savedRow.audience === "everyone"
          ? `everyone${savedReach === null ? "" : ` (${savedReach})`}`
          : `chosen people${savedReach === null ? "" : ` (${savedReach})`}`,
        mediaLabel(savedRow.media),
        ...(isDefault ? ["the default row"] : []),
      ].join(" · ")
    : "Nothing reaches Plex until you add it.";

  return (
    <div className="w-full space-y-6">
      <PageHeader
        // The name as a template, its placeholders drawn as chips: there is no single rendered name,
        // because each person and each library fills it differently.
        title={collection ? <RowName name={savedName} className="" /> : "Add a row"}
        subtitle={subtitle}
        className="mb-0"
        actions={
          collection && (
            <Button asChild variant="ghost" size="sm">
              <Link to={`/runs?row=${encodeURIComponent(collection.slug)}`}>
                <ListChecks aria-hidden="true" />
                Runs that built it
              </Link>
            </Button>
          )
        }
      />

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
          {sentenceCaseHighlights(template.highlights).join(", ")}.
        </p>
      )}

      {savedRow && (
        <RowLiveStrip
          collection={savedRow}
          enableDisabled={save.isPending}
          onEnableSaving={setEnableSaving}
          onEnableSaved={(enabled) => {
            setSavedEnabled(enabled);
            setInput((prev) => ({ ...prev, enabled }));
          }}
          renameDisabled={enableSaving || pendingRename !== null}
          onRename={openRename}
          unsaved={changes.length > 0}
          history={effectiveness.data}
          historyState={effectiveness.isError ? "error" : effectiveness.isPending ? "loading" : "ready"}
          onRetryHistory={() => {
            void effectiveness.refetch();
          }}
        />
      )}

      {/* A block, not a grid, below `lg`: the jump list is sticky, and a sticky element only sticks
          inside its parent — a grid row of its own would be exactly its height and let it scroll
          away. From `lg` the grid's single row spans the whole form, so it sticks there too. */}
      <div key={draftVersion} className="lg:grid lg:grid-cols-[11rem_minmax(0,1fr)] lg:items-start lg:gap-8">
        <RowSectionNavigation sections={sections} />

        {/* `min-w-0`: a grid item's default `min-width: auto` resolves to its min-content width,
            and the widest unbreakable thing inside once pushed a 320px page 60px sideways. */}
        <div className="mt-6 min-w-0 space-y-10 lg:mt-0">
          <EditorSection
            id="name-and-look"
            title="Name & look"
            description="The name, description and artwork people see on their Plex Home."
          >
            <div data-setting="name" className="space-y-2">
              {collection && !pendingRename ? (
                // Not an input: Save never carries a new name. Saving it without renaming on Plex
                // would leave the database and the server disagreeing, with nothing on screen
                // saying so — so a rename goes through Live on Plex, which owns the Plex work.
                <>
                  <p className="text-sm font-medium">Name</p>
                  <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-md border bg-elevated px-3 py-2">
                    <RowName name={savedName} className="min-w-0 font-medium [overflow-wrap:anywhere]" />
                    <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      <Lock aria-hidden="true" className="size-3.5" />
                      Changed with Rename on Plex
                    </span>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    A rename rewrites this row on Plex for everyone who has it, so it
                    happens at once from Live on Plex, not on Save.
                  </p>
                </>
              ) : collection && pendingRename ? (
                // While a kind switch's rename is pending, this box IS that rename: Save sends it,
                // so it can be fixed here after a refused save.
                <>
                  <Label htmlFor="row-name">Name</Label>
                  <Input
                    id="row-name"
                    value={pendingRename.name}
                    onChange={(e) => setPendingRename({ ...pendingRename, name: e.target.value })}
                  />
                  <TemplateVarsHint seasonal={isSeasonal} themed={aiRow} />
                  {pendingProblem && (
                    <p role="alert" className="text-sm text-destructive-text">
                      {pendingProblem}
                    </p>
                  )}
                  <p className="text-sm text-warning">
                    To match its new kind, saving renames the row to this.{" "}
                    {saved && renameAtNote(renameAt(saved, input, pendingRename.name.trim()), kindCtx)}
                  </p>
                </>
              ) : (
                <>
                  <Label htmlFor="row-name">Name</Label>
                  <Input
                    id="row-name"
                    value={input.name}
                    onChange={(e) => set({ name: e.target.value })}
                    placeholder="e.g. ✨ Hidden Gems for {user}"
                  />
                  {/* Just the placeholders. What the name BECOMES is the Plex card below. */}
                  <TemplateVarsHint seasonal={isSeasonal} themed={aiRow} />
                </>
              )}
            </div>

            <div data-setting="description" className="border-t pt-4">
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
            <div className="border-t pt-4">
              <RowPlexCard
                compact
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
                sampleTheme={
                  pendingTheme
                    ? { name: pendingTheme.draft.name, emoji: pendingTheme.draft.emoji ?? "" }
                    : undefined
                }
              />
            </div>
          </EditorSection>

          <EditorSection
            id="who-gets-it"
            title="Who gets it"
            description={
              input.build === "shared"
                ? "One copy, shared by everyone in its audience. Every account outside it has hide rules that exclude it before it reaches their Home."
                : "Each person gets their own copy, built from their own watching. Every other account's hide rules exclude it before it reaches anyone's Home."
            }
          >
            <div data-setting="audience">
              {audienceState === "error" ? (
                <div role="alert" className="space-y-2 text-sm">
                  <p className="text-destructive-text">Couldn’t load the audience. Your saved audience is unchanged.</p>
                  <Button type="button" variant="outline" size="sm" onClick={onRetryAudience}>
                    Retry audience
                  </Button>
                </div>
              ) : audienceState === "loading" ? (
                <p role="status" className="text-sm text-muted-foreground">
                  Loading people…
                </p>
              ) : (
                <AudiencePicker
                  audience={input.audience}
                  audienceUserIds={input.audience_user_ids}
                  users={users}
                  onChange={set}
                >
                  <RowAudienceTable
                    input={input}
                    users={users}
                    libraries={landsIn}
                    accounts={privacy.data?.accounts ?? []}
                    privacy={privacy.isError ? "error" : privacy.isPending ? "loading" : "ready"}
                    onSelect={(id, checked) => set({
                      audience: "subset",
                      audience_user_ids: checked ? [...input.audience_user_ids, id] : input.audience_user_ids.filter((selected) => selected !== id),
                    })}
                  />
                </AudiencePicker>
              )}
            </div>
          </EditorSection>

          <EditorSection
            id="what-goes-in"
            title="What goes in"
            description="What kind of row it is, where its titles come from, how many, and in what order."
          >
            {/* First: the kind decides every setting after it (design §3). Folded to its one line,
                because the six kinds with their descriptions are a page of their own. */}
            {aiRow ? (
              <>
                {/* Fixed: an AI row can't be switched to another kind, and no other kind can become one,
                    because it needs a list first (`isAiRow`). */}
                <div data-setting="kind">
                  <span className="block font-medium">Row type: {AI_KIND_META.title}</span>
                  <span className="block text-sm text-muted-foreground">{AI_KIND_META.description}</span>
                </div>
                <div className="space-y-4 border-t pt-4">
                  <AiRowSection
                    input={draft}
                    collection={collection}
                    pending={pendingTheme}
                    tokensSpent={tokensSpent}
                    onPending={setPendingTheme}
                    onSpent={(tokens) => setTokensSpent((total) => total + tokens)}
                  />
                </div>
              </>
            ) : (
              <>
            <RowBuildPicker value={input.build} onChange={pickBuild} sharedDisabledReason={disabledKindReason("popular")} seasonal={isSeasonal} />
            <details data-setting="kind" className="group">
              <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-3 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
                <span className="min-w-0">
                  <span className="block font-medium">Row type: {kindTitle(current)}</span>
                  <span className="block text-sm text-muted-foreground">{KIND_META[current.kind].description}</span>
                </span>
                <span className="shrink-0 whitespace-nowrap rounded-md border border-border-strong bg-elevated px-3 py-1.5 text-xs font-medium hover:bg-raised">
                  Change row type…
                </span>
              </summary>
              <div className="pt-4">
                <RowKindPicker
                  value={current.kind}
                  build={input.build}
                  onChange={pickKind}
                  // The row's own kind is never disabled, even before the season list loads.
                  disabledReason={disabledKindReason}
                />
              </div>
            </details>

            <div className="space-y-4 border-t pt-4">
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
                savedRow={collection ? { id: collection.id, seasons: collection.seasons ?? [] } : null}
                // Only while the form still matches what is saved: the status describes the SAVED row.
                seasonStatus={
                  collection &&
                  JSON.stringify(collection.seasons ?? []) === JSON.stringify(input.seasons) &&
                  collection.season_lead_days === input.season_lead_days &&
                  collection.season_after_days === input.season_after_days
                    ? (collection.season_status ?? null)
                    : null
                }
              />
            </div>
              </>
            )}

            <div data-setting="libraries" className="space-y-2 border-t pt-4">
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
              {input.library_keys.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  Builds in {everyLibraryOfType}, including any you add to Plex later.
                </p>
              )}
            </div>

            {shown.has("size") && (
              <div data-setting="size" className="border-t pt-4">
                <RowSizeField
                  value={input.size}
                  onChange={(size) => set({ size })}
                />
              </div>
            )}
            {shown.has("pick_order") && (
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
                  <div data-setting="rated_by" className="space-y-1.5 rounded-md bg-elevated p-3">
                    <Label htmlFor="row-rating-source">Rated by · global setting</Label>
                    <select
                      id="row-rating-source"
                      value={ratingSource}
                      onChange={(e) =>
                        saveSettings.mutate({
                          "recommendations.rating_source": asRatingSource(e.target.value),
                        })
                      }
                      disabled={saveSettings.isPending}
                      className="h-9 w-56 max-w-full rounded-md border bg-background px-3 text-sm"
                    >
                      {RATING_SOURCES.map((source) => (
                        <option key={source} value={source}>
                          {RATING_LABELS[source]}
                        </option>
                      ))}
                    </select>
                    {saveSettings.isPending && <p role="status" className="text-xs text-muted-foreground">Saving global rating source…</p>}
                    {saveSettings.isSuccess && <p role="status" className="text-xs text-success">Global rating source saved.</p>}
                    {saveSettings.isError && <p role="alert" className="text-xs text-destructive-text">{apiErrorMessage(saveSettings.error, "Couldn’t save the global rating source. Try again.")}</p>}
                    <p className="text-xs text-muted-foreground">
                      {ratingSource === "tmdb"
                        ? "TMDB scores need no setup. IMDb, Trakt, Rotten Tomatoes and Metacritic all come from MDBList, a free service that fetches every site’s score in one lookup — add its key under Settings → Connections."
                        : `Scores come from MDBList, a free service that fetches every site’s score in one lookup. Add its key under Settings → Connections, or ${ratingLabel} rows quietly fall back to TMDB.`}{" "}
                      Shared by every row and by requests. Changes save immediately, separately from Save changes below.
                      {" "}<Link to={DEFAULT_ROW_NAME_SETTINGS} className="underline underline-offset-2">Global row defaults</Link>
                    </p>
                  </div>
                )}
              </div>
            )}

            <RowContentsFields
              input={draft}
              set={set}
              shown={shown}
              settings={settings.data}
              fill={aiRow ? "picked" : current.fill}
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
          </EditorSection>

          {aiRow && (
            <>
              <EditorSection
                id="try-it"
                title="Try it"
                description="Run this row for one person to see what it would pick, and why. Nothing is written to Plex."
              >
                <AiTryIt collection={collection} users={users} unsaved={changes.length > 0} />
              </EditorSection>

              <EditorSection
                id="ai-prompts"
                title="AI prompts"
                description="What the AI is told when it builds or changes this row's list. The guidance is yours to change; the mechanics aren't."
              >
                <AiPromptsSection input={draft} set={set} />
              </EditorSection>
            </>
          )}

          <EditorSection
            id="schedule"
            title="Schedule"
            description="When Shortlist runs this row, and how often its titles change. Every row runs on its own schedule."
          >
            <div data-setting="schedule">
              <RowScheduleField
                value={input.schedule}
                onChange={(cron) => set({ schedule: cron })}
              />
            </div>

            {/* A row that follows a watch has its cadence forced to nightly by the engine
                (`effective_refresh_days`), so there is nothing to choose — one line says so. A
                shared row is picked again on every run and gets nothing at all. */}
            {shown.has("refresh_days") ? (
              <InheritableField
                setting="refresh_days"
                label="Titles refresh every…"
                labelFor="row-refresh-days"
                description="How often this row swaps some of its titles for new ones."
                ariaLabel="Use the global refresh cadence"
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

            <p className="rounded-md bg-elevated px-3 py-2 text-sm text-muted-foreground">
              <b className="font-semibold text-foreground">Next:</b>{" "}
              {nextRunWords(input.schedule, nextRun)}{" "}
              {titlesChangeWords({
                followsAWatch: followsAWatch && current.fill !== "popular",
                hasCadence: shown.has("refresh_days"),
                cadence: effectiveCadence,
                inheriting: input.refresh_days === null,
              })}
            </p>
          </EditorSection>

          <EditorSection
            id="placement"
            title="Placement"
            description="Which Plex screens it shows on, where it sits, and on which days."
          >
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
            <div data-setting="hub_anchor" className="space-y-2 border-t pt-4">
              <span className="text-sm font-medium">Position in the Recommended shelf</span>
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
          </EditorSection>

          {!input.requests_row && (
            <EditorSection
              id="requests"
              title="Requests"
              description="What this row asks Sonarr and Radarr for when a pick isn't on the server yet, and where those titles land."
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
                    audienceSize={reach}
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
            </EditorSection>
          )}

          {/* Last, and fenced off: these reach into other people's Plex, and Discard doesn't undo
              them. `remove-this-row` keeps the Rows list's link, and old bookmarks, landing here. */}
          {collection && (
            <EditorSection
              id="danger-zone"
              title="Danger zone"
              description="To take it off Plex but keep its settings, switch it off in Live on Plex instead."
              panelId="remove-this-row"
              danger
            >
              <RowDestructiveActions collection={collection} reach={savedReach} onDeleted={onClose} />
            </EditorSection>
          )}

          {save.isError && (
            <p role="alert" className="text-sm text-destructive-text">
              {apiErrorMessage(save.error, "Couldn’t save this row. Try again.")}
            </p>
          )}
          {saveTheme.isError && (
            <p role="alert" className="text-sm text-destructive-text">
              {apiErrorMessage(saveTheme.error, "Couldn’t save this row’s list. Try again.")}
            </p>
          )}
          {aiBlocked && (
            <p role="status" className="text-sm text-warning">
              {aiBlocked}
            </p>
          )}
        </div>
      </div>

      <RowSaveBar
        changes={changes}
        isNew={!collection}
        saving={save.isPending || saveTheme.isPending}
        saveDisabled={!input.name.trim() || pendingProblem !== null || enableSaving || aiBlocked !== null}
        onSave={submit}
        onDiscard={discard}
        onCancel={onClose}
      />

      <RowRenameDialog
        open={renameOpen}
        onOpenChange={setRenameOpen}
        value={renameDraft}
        onChange={setRenameDraft}
        changed={renamePending}
        seasonal={isSeasonal}
        onConfirm={startRename}
      />

      <Dialog open={discardForRename} onOpenChange={setDiscardForRename}>
        <DialogContent>
          <DialogHeader><DialogTitle>Discard unsaved settings and rename?</DialogTitle><DialogDescription>Rename uses the saved row settings. Your other unsaved changes will be discarded. Keep editing to save those settings first.</DialogDescription></DialogHeader>
          <DialogFooter><Button variant="outline" onClick={() => setDiscardForRename(false)}>Keep editing</Button><Button onClick={() => { setDiscardForRename(false); onClose(); onRename?.(renameDraft.trim()); }}>Discard settings and rename</Button></DialogFooter>
        </DialogContent>
      </Dialog>

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
