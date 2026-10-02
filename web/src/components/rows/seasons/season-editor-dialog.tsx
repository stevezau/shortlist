import { useId, useMemo, useRef, useState } from "react";

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
import { useCreateSeason, useDeleteSeason, useSeasonPreview, useSeasons, useUpdateSeason } from "@/lib/queries";
import {
  blankDraft,
  draftFrom,
  draftProblems,
  emojiProblem,
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

import { SeasonCollectionPicker } from "./season-collection-picker";
import { SeasonGenreFields } from "./season-genre-fields";
import { SeasonPicksPicker } from "./season-picks-picker";
import { SeasonSummary } from "./season-summary";
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
  const preview = useSeasonPreview(counted, { keepPrevious: true });
  const counting = preview.isFetching || draftKey !== countedKey;
  // What the count says about the date, only while it is about the date on screen.
  const countedRule =
    preview.data && JSON.stringify(preview.data.draft.rule) === JSON.stringify(previewInput(draft, row).rule)
      ? preview.data
      : null;
  const ruleError = (countedRule?.rule_error ?? null) || ruleProblem(draft.rule);

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

  const nextDate = countedRule?.next_date ?? null;
  const nextLine = ruleError
    ? null
    : nextDate
      ? `Next: ${longDate(nextDate)} — shows from ${weekdayDate(addDays(nextDate, -draft.lead_days))}, hidden again from ${weekdayDate(addDays(nextDate, draft.after_days + 1))}`
      : preview.isError
        ? null
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
                  Films
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

          <SeasonSummary preview={preview} row={row} alsoUsedBy={alsoUsedBy} />
        </div>

        <DialogFooter className="sticky bottom-0 -mx-4 flex-col gap-3 border-t bg-background px-4 py-3 sm:-mx-6 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between sm:space-x-0 sm:px-6">
          <div className="min-w-0 flex-1 space-y-1 text-sm">
            {problems.length > 0 && (
              <p id={ids.problems} className="text-muted-foreground">
                {problems.map((problem) => (
                  <span key={problem} className="mr-1.5 inline-block">
                    {problem}
                  </span>
                ))}
              </p>
            )}
            {footerError && (
              <p role="alert" className="text-destructive-text">
                {footerError}
              </p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {editing && (
              <Button
                type="button"
                variant="ghost"
                className="text-destructive-text hover:text-destructive-text"
                onClick={() => setConfirmingDelete(true)}
              >
                Delete season
              </Button>
            )}
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button"
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
            onCancel={() => setConfirmingDelete(false)}
            onDeleted={() => onDeleted(editing.slug)}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

/**
 * "Delete “Thanksgiving”?" (#137 D12): deleting unticks the season in every row, in one go. The server
 * refuses while it is any row's only season (409), and says which; that refusal shows here.
 */
function DeleteSeasonDialog({
  season,
  blocked,
  onCancel,
  onDeleted,
}: {
  season: Season;
  /** Why it can't be deleted from here (nothing is sent); null when it can. */
  blocked: string | null;
  onCancel: () => void;
  onDeleted: () => void;
}) {
  const remove = useDeleteSeason();
  const rows = season.used_by.map((row) => row.name);
  const title = rows.length > 0 ? `Remove “${season.name}” from ${andList(rows)} and delete it?` : `Delete “${season.name}”?`;
  const refusal =
    remove.error instanceof ApiError && remove.error.status === 409
      ? remove.error.message
      : remove.error
        ? apiErrorMessage(remove.error, "Couldn’t delete the season. Check Shortlist is running, then try again.")
        : null;

  return (
    <Dialog open onOpenChange={(open) => !open && onCancel()}>
      <DialogContent className="w-[calc(100%-2rem)]">
        <DialogHeader>
          <DialogTitle className="leading-snug">{title}</DialogTitle>
          <DialogDescription>
            {rows.length > 0
              ? "Those rows keep their other seasons. The season is deleted for the whole server, and can't be brought back."
              : "It's deleted for the whole server, and can't be brought back."}
          </DialogDescription>
        </DialogHeader>
        {blocked && <p className="rounded-md bg-muted/60 p-3 text-sm">{blocked}</p>}
        {refusal && (
          <p role="alert" className="rounded-md border border-destructive/40 p-3 text-sm">
            {refusal}
          </p>
        )}
        <DialogFooter className="gap-2">
          <Button type="button" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={blocked !== null}
            loading={remove.isPending}
            onClick={() => remove.mutate(season.slug, { onSuccess: onDeleted })}
          >
            Delete season
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
