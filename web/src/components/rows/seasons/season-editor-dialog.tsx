import { Fragment, useId, useMemo, useRef, useState, type RefObject } from "react";

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
import { ApiError, apiErrorMessage } from "@/lib/api";
import {
  useCreateSeason,
  useDeleteSeason,
  useSeasonNextDate,
  useSeasonPreview,
  useSeasons,
  useUpdateSeason,
} from "@/lib/queries";
import {
  blankDraft,
  draftFrom,
  draftProblems,
  emojiProblem,
  hasSource,
  nameClash,
  previewInput,
  ruleProblem,
  seasonBody,
  type SeasonDraft,
} from "@/lib/season-draft";
import { titleNoun, type SeasonRow } from "@/lib/season-verdict";
import { addDays, longDate, weekdayDate } from "@/lib/seasons";
import type { Season, SeasonPreset, SeasonPreviewInput } from "@/lib/types";
import { useDebouncedValue } from "@/lib/use-debounced-value";
import { cn } from "@/lib/utils";

import { SeasonCollectionPicker } from "./season-collection-picker";
import { SeasonGenreFields } from "./season-genre-fields";
import { SeasonPicksPicker } from "./season-picks-picker";
import { SeasonCountLine, SeasonSummary } from "./season-summary";
import { SeasonTagPicker } from "./season-tag-picker";
import { SeasonWhenFields } from "./season-when-fields";

export type SeasonEditorTarget =
  | { kind: "create" }
  | { kind: "preset"; preset: SeasonPreset }
  | { kind: "edit"; season: Season };

/** "A, B and C". */
function andList(names: readonly string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** What the sources section is headed, in the row's word for its titles. */
const SOURCES_HEADING = { movie: "Films", show: "Shows", both: "Titles" } as const;

/** The server's refusal of a name already taken (`api/seasons.py` `_checked`), which belongs by the name. */
const NAME_CLASH = /already a season called/;

/**
 * Create, add (from a ready-made season) or edit a season, from the Seasonal row editor (#137 D1).
 *
 * Saved for the whole server, and ticked in the row it was opened from. Beside the form, the server's
 * count of the films it finds, against that row's size and mode (D10). Nothing is saved until Save:
 * even a ready-made season opens here first (D9).
 *
 * Mounted only while open, so each opening starts from its target, not a stale draft.
 */
export function SeasonEditorDialog({
  target,
  row,
  savedRow,
  tickedHere,
  onClose,
  onCloseAutoFocus,
  onSaved,
  onDeleted,
}: {
  target: SeasonEditorTarget;
  /** The row the editor was opened from: what the count is made for, and its verdict judged by. */
  row: SeasonRow;
  /** That row as saved: left out of "Also used by", and what the server judges a delete by. Null for a
   *  row not saved yet. */
  savedRow: { id: number; seasons: readonly string[] } | null;
  /** The seasons ticked in that row's form right now. */
  tickedHere: readonly string[];
  onClose: () => void;
  /** Where focus goes once the dialog has gone (Radix's `onCloseAutoFocus`). */
  onCloseAutoFocus?: (event: Event) => void;
  onSaved: (slug: string) => void;
  onDeleted: (slug: string) => void;
}) {
  const editing = target.kind === "edit" ? target.season : null;
  const [draft, setDraft] = useState<SeasonDraft>(() =>
    target.kind === "create" ? blankDraft() : draftFrom(target.kind === "preset" ? target.preset : target.season),
  );
  const [saveError, setSaveError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const update = (patch: Partial<SeasonDraft>) => setDraft((prev) => ({ ...prev, ...patch }));
  const nameInput = useRef<HTMLInputElement>(null);

  const catalogue = useSeasons();
  const create = useCreateSeason();
  const save = useUpdateSeason();
  const saving = create.isPending || save.isPending;

  // Counted 400ms after the last change. Debounced as JSON: a fresh object every render would restart
  // the wait forever.
  const draftKey = JSON.stringify(previewInput(draft, row));
  const countedKey = useDebouncedValue(draftKey, 400);
  const counted = useMemo(() => JSON.parse(countedKey) as SeasonPreviewInput, [countedKey]);
  const sourcesNow = hasSource(draft);
  // Nothing to count before a source: the summary says so instead of judging an empty season "too few".
  const preview = useSeasonPreview(counted, {
    keepPrevious: true,
    enabled: hasSource({
      tags: counted.tags ?? [],
      genre: counted.genre ?? null,
      collections: counted.collections ?? [],
      picks: counted.picks ?? [],
    }),
  });
  const counting = preview.isFetching || draftKey !== countedKey;
  // The date comes from the rule alone, asked apart from the count, so a failed count keeps it. Only an
  // answer about the rule on screen is used: while a change waits out the debounce, it is being worked out.
  const dates = useSeasonNextDate(counted.rule);
  const ruleSettled = JSON.stringify(previewInput(draft, row).rule) === JSON.stringify(counted.rule);
  const dateAnswer = ruleSettled ? dates.data : undefined;
  const ruleError = (dateAnswer?.rule_error ?? null) || ruleProblem(draft.rule);

  const clash = nameClash(draft.name, catalogue.data ?? [], editing?.slug ?? null);
  const emojiError = emojiProblem(draft.emoji);
  const problems = draftProblems(draft, { clash, ruleError });
  const nameError = clash
    ? `There's already a season called “${clash}”.${
        target.kind === "preset" && target.preset.label !== draft.name.trim()
          ? ` Give this one another name, like “${target.preset.label}”.`
          : " Give this one another name."
      }`
    : saveError && NAME_CLASH.test(saveError)
      ? saveError
      : null;
  const footerError = saveError && !NAME_CLASH.test(saveError) ? saveError : null;

  const title =
    target.kind === "create"
      ? "Create your own season"
      : target.kind === "preset"
        ? `Add ${target.preset.label}`
        : `Edit ${target.season.name}`;
  const alreadyTicked = editing !== null && tickedHere.includes(editing.slug);
  const alsoUsedBy = (editing?.used_by ?? []).filter((row) => row.id !== savedRow?.id).map((row) => row.name);
  // Why Delete can't go ahead from here, said before anything is sent. The server judges by SAVED rows:
  // one whose only saved season this is gets a 409, however many the form here ticks.
  const deleteBlocked = !editing
    ? null
    : tickedHere.length === 1 && tickedHere[0] === editing.slug
      ? "It’s the only season ticked in this row. Tick another season for this row first, then delete it."
      : savedRow !== null &&
          savedRow.seasons.length > 0 &&
          savedRow.seasons.every((slug) => slug === editing.slug) &&
          tickedHere.some((slug) => slug !== editing.slug)
        ? "Save this row first, then delete the season. As saved, it follows only this season."
        : null;

  const nextDate = dateAnswer?.next_date ?? null;
  const nextLine = ruleError
    ? null
    : nextDate
      ? `Next: ${longDate(nextDate)} — shows from ${weekdayDate(addDays(nextDate, -draft.lead_days))}, hidden again from ${weekdayDate(addDays(nextDate, draft.after_days + 1))}`
      : ruleSettled && dates.isError
        ? "Couldn’t work out the next date. Check Shortlist is running."
        : "Working out the next date…";

  const submit = async () => {
    setSaveError(null);
    try {
      const saved = editing
        ? await save.mutateAsync({ slug: editing.slug, body: seasonBody(draft, null) })
        : await create.mutateAsync(seasonBody(draft, target.kind === "preset" ? target.preset.key : null));
      onSaved(saved.slug);
    } catch (error) {
      setSaveError(apiErrorMessage(error, "Couldn’t save the season. Check Shortlist is running, then try again."));
    }
  };

  const deleteButton = useRef<HTMLButtonElement>(null);
  const ids = {
    nameHeading: useId(),
    emoji: useId(),
    emojiError: useId(),
    name: useId(),
    nameHint: useId(),
    nameError: useId(),
    films: useId(),
    problems: useId(),
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        className="max-h-[90vh] w-[calc(100%-2rem)] max-w-[1100px] gap-6 overflow-y-auto p-4 pb-0 sm:p-6 sm:pb-0"
        // A season takes a while to put together; a stray click beside the dialog mustn't lose it.
        onInteractOutside={(event) => event.preventDefault()}
        // The name is what every season needs first; Emoji already has one.
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          nameInput.current?.focus();
        }}
        onCloseAutoFocus={onCloseAutoFocus}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>Saved for the whole server — any Seasonal row can tick it.</DialogDescription>
        </DialogHeader>

        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-start">
          <div className="min-w-0 space-y-8">
            <section aria-labelledby={ids.nameHeading} className="space-y-3">
              <h3 id={ids.nameHeading} className="text-base font-semibold">
                Name
              </h3>
              <div className="flex gap-3">
                <div className="w-20 shrink-0 space-y-1">
                  <Label htmlFor={ids.emoji}>Emoji</Label>
                  <Input
                    id={ids.emoji}
                    value={draft.emoji}
                    onChange={(event) => update({ emoji: event.target.value })}
                    aria-invalid={emojiError !== null}
                    aria-describedby={emojiError ? ids.emojiError : undefined}
                    className="text-center text-lg"
                  />
                </div>
                <div className="min-w-0 flex-1 space-y-1">
                  <Label htmlFor={ids.name}>Season name</Label>
                  <Input
                    ref={nameInput}
                    id={ids.name}
                    value={draft.name}
                    maxLength={40}
                    placeholder="e.g. Thanksgiving"
                    onChange={(event) => {
                      update({ name: event.target.value });
                      setSaveError(null);
                    }}
                    aria-invalid={nameError !== null}
                    aria-describedby={nameError ? `${ids.nameError} ${ids.nameHint}` : ids.nameHint}
                  />
                </div>
              </div>
              {emojiError && (
                <p id={ids.emojiError} role="alert" className="text-sm text-destructive-text">
                  {emojiError}
                </p>
              )}
              {nameError && (
                <p id={ids.nameError} role="alert" className="text-sm text-destructive-text">
                  {nameError}
                </p>
              )}
              <p id={ids.nameHint} className="text-sm text-muted-foreground">
                Row names show it where they say <code>{"{season}"}</code>, e.g. “
                {`${draft.emoji.trim()} ${draft.name.trim() || "Thanksgiving"} picks`.trim()}”.
              </p>
            </section>

            <SeasonWhenFields
              rule={draft.rule}
              onRule={(rule) => update({ rule })}
              leadDays={draft.lead_days}
              afterDays={draft.after_days}
              onTiming={update}
              ruleError={ruleError}
              nextLine={nextLine}
            />

            <section aria-labelledby={ids.films} className="space-y-6">
              <div className="space-y-1">
                <h3 id={ids.films} className="text-base font-semibold">
                  {SOURCES_HEADING[row.media]}
                </h3>
                <p className="text-sm text-muted-foreground">
                  {`A ${titleNoun(row.media, 1)} belongs to this season if any source below finds it. Only ${titleNoun(row.media, 2)} in your libraries are used. Mix sources freely.`}
                </p>
                {target.kind === "preset" && target.preset.note && (
                  <p className="mt-2 rounded-md bg-muted/60 p-3 text-sm">{target.preset.note}</p>
                )}
              </div>
              <SeasonTagPicker
                tags={draft.tags}
                onChange={(tags) => update({ tags })}
                perTag={preview.data?.per_tag}
                counting={counting}
              />
              <SeasonCollectionPicker
                collections={draft.collections}
                onChange={(collections) => update({ collections })}
                perCollection={preview.data?.per_collection}
                counting={counting}
                media={row.media}
              />
              <SeasonPicksPicker picks={draft.picks} onChange={(picks) => update({ picks })} />
              <SeasonGenreFields
                genre={draft.genre}
                onGenre={(genre) => update({ genre })}
                excluded={draft.excluded_genres}
                onExcluded={(excluded_genres) => update({ excluded_genres })}
              />
            </section>
          </div>

          <SeasonSummary preview={preview} row={row} hasSources={sourcesNow} alsoUsedBy={alsoUsedBy} />
        </div>

        {/* Compact on a phone: one line of count, one line of what's missing, one row of buttons — the
            footer stays in view, so every line here costs the form above it. */}
        <DialogFooter className="sticky bottom-0 -mx-4 flex-col gap-2 border-t bg-background px-4 py-2 sm:-mx-6 sm:flex-row sm:items-center sm:justify-between sm:gap-3 sm:space-x-0 sm:px-6 sm:py-3">
          <div className="min-w-0 flex-1 space-y-0.5 text-xs sm:text-sm">
            <SeasonCountLine preview={preview} row={row} hasSources={sourcesNow} className="lg:hidden" />
            {problems.length > 0 && (
              <p id={ids.problems} className="text-muted-foreground">
                {problems.map((problem, index) => (
                  <Fragment key={problem}>
                    {index > 0 && " "}
                    <span className={cn("mr-1.5 inline-block", index > 0 && "max-sm:sr-only")}>{problem}</span>
                  </Fragment>
                ))}
                {problems.length > 1 && (
                  <span aria-hidden="true" className="sm:hidden">{`+${problems.length - 1} more`}</span>
                )}
              </p>
            )}
            {footerError && (
              <p role="alert" className="text-destructive-text">
                {footerError}
              </p>
            )}
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
            {editing && (
              <Button
                ref={deleteButton}
                type="button"
                size="sm"
                variant="ghost"
                aria-label="Delete season"
                className="text-destructive-text hover:text-destructive-text sm:h-9 sm:px-4 sm:text-sm"
                onClick={() => setConfirmingDelete(true)}
              >
                <span className="sm:hidden">Delete</span>
                <span className="max-sm:hidden">Delete season</span>
              </Button>
            )}
            <Button type="button" size="sm" variant="outline" className="sm:h-9 sm:px-4 sm:text-sm" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              className="sm:h-9 sm:px-4 sm:text-sm"
              disabled={problems.length > 0}
              loading={saving}
              aria-describedby={problems.length > 0 ? ids.problems : undefined}
              onClick={() => void submit()}
            >
              {alreadyTicked ? "Save changes" : "Save and add to this row"}
            </Button>
          </div>
        </DialogFooter>

        {editing && confirmingDelete && (
          <DeleteSeasonDialog
            season={editing}
            blocked={deleteBlocked}
            returnFocusTo={deleteButton}
            onCancel={() => setConfirmingDelete(false)}
            onDeleted={() => onDeleted(editing.slug)}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

/**
 * "Delete “Thanksgiving”?" (#137 D12): deleting unticks the season in every row, in one go.
 *
 * Refused before anything is sent when this row's form or its saved state stands in the way (`blocked`),
 * and by the server while the season is any row's only season (409). Either way it says so first, under
 * "Can't delete … yet", and offers nothing to press that would only be refused.
 */
function DeleteSeasonDialog({
  season,
  blocked,
  returnFocusTo,
  onCancel,
  onDeleted,
}: {
  season: Season;
  /** Why it can't be deleted from here (nothing is sent); null when it can. */
  blocked: string | null;
  /** The Delete button that opened this, given focus back on Cancel: this dialog has no Radix trigger. */
  returnFocusTo: RefObject<HTMLButtonElement | null>;
  onCancel: () => void;
  onDeleted: () => void;
}) {
  const remove = useDeleteSeason();
  const rows = season.used_by.map((row) => row.name);
  const refused = remove.error instanceof ApiError && remove.error.status === 409 ? remove.error.message : null;
  const failed =
    remove.error && !refused
      ? apiErrorMessage(remove.error, "Couldn’t delete the season. Check Shortlist is running, then try again.")
      : null;
  const reason = blocked ?? refused;
  const title = reason
    ? `Can't delete “${season.name}” yet`
    : rows.length > 0
      ? `Remove “${season.name}” from ${andList(rows)} and delete it?`
      : `Delete “${season.name}”?`;
  const consequence =
    rows.length === 1
      ? "That row keeps its other seasons. The season is deleted for the whole server, and can't be brought back."
      : rows.length > 1
        ? "Those rows keep their other seasons. The season is deleted for the whole server, and can't be brought back."
        : "It's deleted for the whole server, and can't be brought back.";

  return (
    <Dialog open onOpenChange={(open) => !open && onCancel()}>
      <DialogContent
        className="w-[calc(100%-2rem)]"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          if (returnFocusTo.current?.isConnected) returnFocusTo.current.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle className="leading-snug">{title}</DialogTitle>
          <DialogDescription>{reason ?? consequence}</DialogDescription>
        </DialogHeader>
        {failed && (
          <p role="alert" className="rounded-md border border-destructive/40 p-3 text-sm">
            {failed}
          </p>
        )}
        <DialogFooter className="gap-2">
          <Button type="button" variant="outline" onClick={onCancel}>
            {reason ? "Close" : "Cancel"}
          </Button>
          {!blocked && (
            <Button
              type="button"
              variant="destructive"
              disabled={refused !== null}
              loading={remove.isPending}
              onClick={() => remove.mutate(season.slug, { onSuccess: onDeleted })}
            >
              Delete season
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
