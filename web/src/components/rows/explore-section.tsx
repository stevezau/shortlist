import { useId, useState } from "react";

import { DiffCard } from "@/components/rows/ai-diff-card";
import { DaysInput } from "@/components/rows/over-time-fields";
import { QueryBoundary } from "@/components/query-boundary";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { apiErrorMessage } from "@/lib/api";
import { formatDate } from "@/lib/format";
import {
  toSaveBody,
  themeGuidance,
  useRegenerateUpNext,
  useSaveTheme,
  useSetUpNext,
  useThemeCapabilities,
  useThemePreview,
  useThemePrompts,
  useThemeRotation,
} from "@/lib/themes";
import type { Collection, CollectionInput, RotationTarget, ThemePreview, ThemeRef } from "@/lib/types";

/** The API's limits on how long a theme lasts (`theme_days`) and on an Explore brief. */
const MIN_DAYS = 1;
const MAX_DAYS = 90;
const DEFAULT_DAYS = 7;
const MAX_EXPLORE_BRIEF = 500;

const PAUSED_REASON = "AI is paused for this row. Resume it below to write or change a theme.";
const NO_AI_REASON = "Add an AI provider in Settings to write themes.";

/**
 * Explore on an AI row (#138): keep one theme, or give each person a new one every few days. In Explore,
 * a saved row also shows each person's Up next, with the choice to change it or ask for another.
 * Nothing here is saved by itself except Up next, which is its own button and changes nothing on Plex.
 */
export function ExploreSection({
  input,
  collection,
  onChange,
}: {
  input: CollectionInput;
  /** The saved row; null while a new row is being added. */
  collection: Collection | null;
  onChange: (patch: Partial<CollectionInput>) => void;
}) {
  const modeName = useId();
  const briefId = useId();
  const daysId = useId();
  const exploring = input.theme_mode === "explore";
  // Up next only exists once the row is saved as Explore: that is when the rotation starts.
  const rotating = collection !== null && collection.theme_mode === "explore";

  return (
    <section aria-label="Explore" className="space-y-4 border-t pt-4">
      <h3 className="text-sm font-semibold">Themes over time</h3>
      <div role="radiogroup" aria-label="Themes over time" className="space-y-2">
        <label className="flex cursor-pointer items-center gap-2.5 text-sm has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
          <input
            type="radio"
            name={modeName}
            checked={!exploring}
            onChange={() => onChange({ theme_mode: "fixed" })}
            className="h-4 w-4 shrink-0 accent-primary focus-visible:outline-none"
          />
          Keep the same theme
        </label>
        <label className="flex cursor-pointer flex-wrap items-center gap-2.5 text-sm has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
          <input
            type="radio"
            name={modeName}
            checked={exploring}
            onChange={() => onChange({ theme_mode: "explore" })}
            className="h-4 w-4 shrink-0 accent-primary focus-visible:outline-none"
          />
          Pick a new theme every few days
        </label>
      </div>

      {exploring && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Label htmlFor={daysId} className="font-normal">
              Days each theme lasts
            </Label>
            <DaysInput
              id={daysId}
              label="Days each theme lasts"
              value={input.theme_days ?? DEFAULT_DAYS}
              min={MIN_DAYS}
              max={MAX_DAYS}
              onCommit={(days) => onChange({ theme_days: days })}
            />
            <span>days</span>
          </div>
          <p className="text-sm text-muted-foreground">
            Shortlist asks your AI provider for a new theme for each person, each time. It uses your AI provider once
            per person per change.
          </p>
          <div className="space-y-2">
            <Label htmlFor={briefId}>What kinds of lists should it pick? (optional)</Label>
            <Textarea
              id={briefId}
              rows={2}
              maxLength={MAX_EXPLORE_BRIEF}
              value={input.explore_brief}
              onChange={(event) => onChange({ explore_brief: event.target.value })}
              placeholder="Leave blank and each person gets a theme chosen from what they watch."
            />
          </div>
          {rotating ? (
            <Rotation collection={collection} input={input} />
          ) : (
            <p className="text-sm text-muted-foreground">
              Save the row to see each person’s lists. Each person’s first list is written overnight.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function Rotation({ collection, input }: { collection: Collection; input: CollectionInput }) {
  const rotation = useThemeRotation(collection.id);
  return (
    <QueryBoundary
      query={rotation}
      skeleton={
        <div role="status" aria-label="Reading each person’s themes" className="space-y-3">
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-16 w-full" />
        </div>
      }
      isEmpty={(data) => data.targets.length === 0}
      empty={
        <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
          No one is in this row’s audience yet, so there is nothing to queue.
        </p>
      }
    >
      {(data) => (
        <div className="space-y-3">
          {data.targets.map((target) => (
            <PersonCard key={target.user_id} target={target} collection={collection} input={input} />
          ))}
        </div>
      )}
    </QueryBoundary>
  );
}

function PersonCard({
  target,
  collection,
  input,
}: {
  target: RotationTarget;
  collection: Collection;
  input: CollectionInput;
}) {
  const capabilities = useThemeCapabilities();
  const regenerate = useRegenerateUpNext();
  const [editing, setEditing] = useState(false);
  const paused = collection.ai_paused;
  const noAi = capabilities.data?.ai === false;
  const reason = paused ? PAUSED_REASON : noAi ? NO_AI_REASON : null;
  const next = target.next;

  return (
    <section aria-label={target.name} className="space-y-3 rounded-lg border bg-elevated p-4">
      <h4 className="text-sm font-semibold">{target.name}</h4>
      <p className="text-sm text-muted-foreground">
        {target.current ? `Now: ${label(target.current)}` : "No theme yet"}
      </p>
      {next ? (
        <p className="text-sm">
          Up next: {label(next)}
          {target.next_due_at && ` — starts ${formatDate(target.next_due_at, { dateOnly: true })}`}
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">
          Nothing queued yet. The next theme is built a day before it starts.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {next?.theme_id != null && (
          <Button type="button" variant="outline" size="sm" disabled={reason !== null} onClick={() => setEditing(!editing)}>
            Change it
          </Button>
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          loading={regenerate.isPending}
          disabled={reason !== null}
          onClick={() => regenerate.mutate({ collectionId: collection.id, userId: target.user_id })}
        >
          Pick another
        </Button>
      </div>
      {reason && <p className="text-sm text-warning">{reason}</p>}
      {!reason && <p className="text-sm text-muted-foreground">Picking another uses your AI provider once.</p>}
      {regenerate.isError && (
        <p role="alert" className="text-sm text-destructive-text">
          {apiErrorMessage(regenerate.error, "Couldn’t pick another theme. Try again.")}
        </p>
      )}
      {editing && next?.theme_id != null && (
        <ChangeIt
          themeId={next.theme_id}
          target={target}
          collection={collection}
          input={input}
          onDone={() => setEditing(false)}
        />
      )}
      {target.history.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium text-muted-foreground">Recent themes</p>
          <ul className="space-y-0.5 text-sm">
            {target.history.map((past) => (
              <li key={`${past.theme_id}-${past.started_at}`}>
                {label(past)} <span className="text-muted-foreground">({formatDate(past.started_at, { dateOnly: true })})</span>
              </li>
            ))}
          </ul>
          <p className="text-sm text-muted-foreground">Shortlist won’t pick these again soon.</p>
        </div>
      )}
    </section>
  );
}

function label(theme: ThemeRef): string {
  return theme.emoji ? `${theme.emoji} ${theme.name}` : theme.name;
}

/**
 * Change one person's queued theme in words: ask the AI for the change, see what it would add and remove,
 * and only then save it. The theme is saved first, then Up next is pointed at it, so a failed save never
 * leaves Up next on a theme that wasn't kept.
 */
function ChangeIt({
  themeId,
  target,
  collection,
  input,
  onDone,
}: {
  themeId: number;
  target: RotationTarget;
  collection: Collection;
  input: CollectionInput;
  onDone: () => void;
}) {
  const changeId = useId();
  const builder = useThemePreview();
  const prompts = useThemePrompts();
  const saveTheme = useSaveTheme();
  const setUpNext = useSetUpNext();
  const [change, setChange] = useState("");
  const [shown, setShown] = useState<ThemePreview | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const mode = input.ai_instructions.mode;
  const guidanceReady = mode !== "add" || prompts.isSuccess;
  const guidance = guidanceReady ? themeGuidance(input.ai_instructions, prompts.data?.guidance ?? "") : "";
  const [spent, setSpent] = useState(0);

  const preview = async () => {
    setProblem(null);
    try {
      const { preview: built, cached } = await builder.build({
        change,
        current_theme_id: themeId,
        collection_id: collection.id,
        media: input.media,
        guidance,
      });
      if (!cached) setSpent((total) => total + built.tokens);
      setShown(built);
    } catch (error) {
      setProblem(apiErrorMessage(error, "Couldn’t reach the AI provider. Check it in Settings, then try again."));
    }
  };

  const keep = async (built: ThemePreview) => {
    setProblem(null);
    try {
      const saved = await saveTheme.mutateAsync({
        id: themeId,
        body: toSaveBody(
          { draft: built.draft, stats: built.stats, origin: "ai" },
          { tokens: spent, collectionId: collection.id },
        ),
      });
      await setUpNext.mutateAsync({ collectionId: collection.id, userId: target.user_id, themeId: saved.id ?? themeId });
      onDone();
    } catch (error) {
      setProblem(apiErrorMessage(error, "Couldn’t save the change. Try again."));
    }
  };

  return (
    <div className="space-y-3 border-t pt-3">
      <div className="space-y-2">
        <Label htmlFor={changeId}>What should change?</Label>
        <Textarea
          id={changeId}
          rows={2}
          maxLength={1000}
          value={change}
          onChange={(event) => setChange(event.target.value)}
          placeholder="e.g. Shorter films, and nothing before 2000"
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          loading={builder.isPending}
          disabled={!change.trim() || !guidanceReady}
          onClick={() => void preview()}
        >
          Preview the change
        </Button>
        <p className="text-sm text-muted-foreground">
          Uses your AI provider once. You see what would be added and removed before anything is saved.
        </p>
      </div>
      {problem && (
        <p role="alert" className="text-sm text-destructive-text">
          {problem}
        </p>
      )}
      {shown?.diff && (
        <DiffCard
          preview={shown}
          diff={shown.diff}
          keepLabel="Use this theme"
          note={`Saves the changed theme and makes it ${target.name}’s Up next. Nothing changes on Plex until it starts.`}
          onKeep={() => void keep(shown)}
          onDiscard={() => setShown(null)}
        />
      )}
    </div>
  );
}
