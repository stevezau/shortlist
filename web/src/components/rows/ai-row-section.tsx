import { useId, useState } from "react";

import { AiHandEdit } from "@/components/rows/ai-hand-edit";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { ErrorState, QueryBoundary } from "@/components/query-boundary";
import { apiErrorMessage } from "@/lib/api";
import {
  missingFromServer,
  rulesSummary,
  savedStats,
  themeGuidance,
  useSetAiPause,
  useTheme,
  useThemeCapabilities,
  useThemePreview,
  useThemePrompts,
  type PendingTheme,
} from "@/lib/themes";
import type { Collection, CollectionInput, Theme, ThemeDiff, ThemePreview, ThemeStats } from "@/lib/types";

/** The API's limit on a brief (`PreviewIn.brief`). */
const MAX_BRIEF = 1000;
/** How many titles the sample shows before "and N more". */
const SAMPLE_SIZE = 12;

const PAUSED_REASON = "AI is paused for this row. Resume it below to build or change its list.";
const SAVE_FIRST_REASON = "Save the row first, then you can change its list in words.";

type Counts = Pick<ThemeStats, "named" | "resolved" | "in_library" | "after_rules"> & { truncated?: boolean };

/**
 * An AI row's list (#138), in What goes in: describe it, build it with one AI call, change it in words, and
 * see what the AI named. Nothing here is saved by itself — the list rides with the row's Save changes — but
 * the AI calls are real and spend tokens, so each says so.
 *
 * Without an AI provider the AI half is gone and the same list is built by hand (`AiHandEdit`).
 */
export function AiRowSection({
  input,
  collection,
  pending,
  tokensSpent,
  onPending,
  onSpent,
}: {
  input: CollectionInput;
  /** The saved row; null while a new row is being added. */
  collection: Collection | null;
  /** The list built or edited here and not saved yet. */
  pending: PendingTheme | null;
  /** Tokens this edit has spent on AI calls so far, saved or not. */
  tokensSpent: number;
  onPending: (next: PendingTheme | null) => void;
  onSpent: (tokens: number) => void;
}) {
  const capabilities = useThemeCapabilities();
  const savedId = collection?.theme_id ?? null;
  const saved = useTheme(savedId);
  // A query that is switched off (a row with no list yet) also reads as pending, so ask for the id too.
  const waitingForSaved = savedId !== null && saved.isPending;

  return (
    <div className="space-y-5">
      {collection === null && (
        <p className="rounded-md bg-muted/60 px-3 py-2 text-sm text-muted-foreground">
          A new AI row starts switched off, so nothing reaches Plex until you switch it on. Build its list, add the
          row, then look it over.
        </p>
      )}
      <QueryBoundary query={capabilities} skeleton={<LoadingList />}>
        {({ ai }) =>
          ai ? (
            <AiHalf
              input={input}
              collection={collection}
              saved={saved}
              waitingForSaved={waitingForSaved}
              pending={pending}
              onPending={onPending}
              onSpent={onSpent}
            />
          ) : waitingForSaved ? (
            <LoadingList />
          ) : saved.isError ? (
            <ErrorState error={saved.error} onRetry={() => void saved.refetch()} />
          ) : (
            <AiHandEdit input={input} saved={saved.data ?? null} pending={pending} onPending={onPending} />
          )
        }
      </QueryBoundary>
      <UsageAndPause collection={collection} tokensSpent={tokensSpent} />
    </div>
  );
}

function LoadingList() {
  return (
    <div role="status" aria-label="Loading the AI row settings" className="space-y-3">
      <Skeleton className="h-4 w-1/3" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-9 w-32" />
    </div>
  );
}

function AiHalf({
  input,
  collection,
  saved,
  waitingForSaved,
  pending,
  onPending,
  onSpent,
}: {
  input: CollectionInput;
  collection: Collection | null;
  saved: ReturnType<typeof useTheme>;
  waitingForSaved: boolean;
  pending: PendingTheme | null;
  onPending: (next: PendingTheme | null) => void;
  onSpent: (tokens: number) => void;
}) {
  const briefId = useId();
  const changeId = useId();
  const builder = useThemePreview();
  const prompts = useThemePrompts();
  const [typed, setTyped] = useState<string | null>(null);
  const [change, setChange] = useState("");
  const [refinement, setRefinement] = useState<ThemePreview | null>(null);
  const [failed, setFailed] = useState<{ message: string; retry: () => void } | null>(null);

  const savedTheme = saved.data ?? null;
  const shown: Theme | null = pending?.draft ?? savedTheme;
  const brief = typed ?? shown?.brief ?? "";
  const paused = collection?.ai_paused === true;
  const mode = input.ai_instructions.mode;
  // "Add to the default" is the default's words plus the owner's, so it can't be sent before the default is known.
  const guidanceReady = mode !== "add" || prompts.isSuccess;
  const guidance = guidanceReady ? themeGuidance(input.ai_instructions, prompts.data?.guidance ?? "") : "";
  const canRefine = collection?.theme_id != null && pending === null;
  const media = input.media;

  const run = async (request: { brief?: string; change?: string; current_theme_id?: number }, then: (p: ThemePreview) => void) => {
    setFailed(null);
    try {
      const { preview, cached } = await builder.build(
        { ...request, media, guidance, ...(collection ? { collection_id: collection.id } : {}) },
        savedTheme?.content_hash ?? "",
      );
      if (!cached) onSpent(preview.tokens);
      then(preview);
    } catch (error) {
      setFailed({
        message: apiErrorMessage(error, "Couldn’t reach the AI provider. Check it in Settings, then try again."),
        retry: () => void run(request, then),
      });
    }
  };

  const build = () =>
    run({ brief }, (preview) => {
      setRefinement(null);
      onPending({ draft: preview.draft, stats: preview.stats, origin: "ai" });
    });

  const refine = () =>
    run({ change, current_theme_id: collection?.theme_id ?? undefined }, (preview) => setRefinement(preview));

  const buildReason = paused ? PAUSED_REASON : !guidanceReady ? "Loading the default instructions…" : null;
  const changeReason = paused ? PAUSED_REASON : !canRefine ? SAVE_FIRST_REASON : buildReason;

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <Label htmlFor={briefId}>Describe it</Label>
        <Textarea
          id={briefId}
          rows={3}
          maxLength={MAX_BRIEF}
          value={brief}
          onChange={(event) => setTyped(event.target.value)}
          placeholder="e.g. Films with a twist ending that make you want to watch them again"
        />
        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            variant="outline"
            onClick={() => void build()}
            loading={builder.isPending}
            disabled={!brief.trim() || buildReason !== null}
          >
            Build the list
          </Button>
          <p className="text-sm text-muted-foreground">Building uses your AI provider once per theme.</p>
        </div>
        {buildReason && <p className="text-sm text-warning">{buildReason}</p>}
      </div>

      {failed && (
        <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-3">
          <p className="text-sm">{failed.message}</p>
          <Button type="button" variant="outline" size="sm" onClick={failed.retry}>
            Try again
          </Button>
        </div>
      )}

      {builder.isPending && !refinement && <LoadingList />}

      {waitingForSaved && pending === null ? (
        <LoadingList />
      ) : shown === null && !builder.isPending && !saved.isError ? (
        <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
          No list yet. Describe the row above, then press Build the list. The AI names titles; Shortlist checks each
          one against TMDB and your libraries.
        </p>
      ) : (
        shown && (
          <ListCard
            theme={shown}
            counts={pending ? pending.stats : savedStats(shown)}
            unsaved={pending !== null}
          />
        )
      )}
      {saved.isError && (
        <ErrorState error={saved.error} onRetry={() => void saved.refetch()} />
      )}

      {refinement?.diff && (
        <DiffCard
          preview={refinement}
          diff={refinement.diff}
          onKeep={() => {
            onPending({ draft: refinement.draft, stats: refinement.stats, origin: "ai" });
            setRefinement(null);
            setChange("");
          }}
          onDiscard={() => setRefinement(null)}
        />
      )}

      <div className="space-y-2 border-t pt-4">
        <Label htmlFor={changeId}>Change it</Label>
        <Textarea
          id={changeId}
          rows={2}
          maxLength={MAX_BRIEF}
          value={change}
          disabled={changeReason !== null}
          onChange={(event) => setChange(event.target.value)}
          placeholder="e.g. Less gore, and more from the last ten years"
        />
        <Button
          type="button"
          variant="outline"
          onClick={() => void refine()}
          loading={builder.isPending}
          disabled={!change.trim() || changeReason !== null}
        >
          Change it
        </Button>
        {changeReason && <p className="text-sm text-warning">{changeReason}</p>}
        <p className="text-sm text-muted-foreground">
          Tell the AI what to change. Your description stays as it is, and you see what would be added and removed
          before anything is kept.
        </p>
      </div>
    </div>
  );
}

function ListCard({ theme, counts, unsaved }: { theme: Theme; counts: Counts | null; unsaved: boolean }) {
  const rules = rulesSummary(theme.rules);
  const sample = theme.picks.slice(0, SAMPLE_SIZE);
  const more = theme.picks.length - sample.length;
  const missing = counts ? missingFromServer(counts) : 0;
  return (
    <section aria-label="The list" className="space-y-3 rounded-lg border bg-elevated p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">
          {theme.emoji && <span aria-hidden="true">{theme.emoji} </span>}
          {theme.name}
        </h3>
        {unsaved && <Badge variant="warning">Not saved yet</Badge>}
        {theme.origin === "manual" && <Badge variant="secondary">Written by hand</Badge>}
      </div>

      {counts && (
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Count label="Named by the AI" value={counts.named} />
          <Count label="Found on TMDB" value={counts.resolved} />
          <Count label="On your server" value={counts.in_library} />
          <Count label="After your limits" value={counts.after_rules} />
        </dl>
      )}
      {counts?.truncated && (
        <p role="status" className="text-sm text-warning">
          The AI’s list was cut short; {counts.named} {counts.named === 1 ? "title" : "titles"} kept. Build again for a
          fuller list.
        </p>
      )}
      {missing > 0 && (
        <p className="text-sm text-muted-foreground">
          {missing} {missing === 1 ? "title the AI named isn’t" : "titles the AI named aren’t"} on your server. They
          are left out of the row; an AI row only picks from what you already have.
        </p>
      )}
      {rules.length > 0 && (
        <ul aria-label="Limits" className="flex flex-wrap gap-1.5">
          {rules.map((rule) => (
            <li key={rule}>
              <Badge variant="outline">{rule}</Badge>
            </li>
          ))}
        </ul>
      )}

      {theme.picks.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          The list is made from tags and genres, so its titles are found when the row runs.
        </p>
      ) : (
        <ul aria-label="Sample titles" className="space-y-1.5 text-sm">
          {sample.map((pick) => (
            <li key={`${pick.media}/${pick.tmdb_id}`} className="flex items-start gap-2">
              <Badge variant={pick.origin === "ai" ? "default" : "secondary"} className="mt-0.5 shrink-0">
                {pick.origin === "ai" ? "AI" : "You"}
              </Badge>
              <span className="min-w-0">
                <span className="font-medium">
                  {pick.title}
                  {pick.year ? ` (${pick.year})` : ""}
                </span>
                {pick.reason && <span className="text-muted-foreground"> — {pick.reason}</span>}
              </span>
            </li>
          ))}
          {more > 0 && <li className="text-muted-foreground">…and {more} more.</li>}
        </ul>
      )}
    </section>
  );
}

function Count({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-lg font-semibold">{value}</dd>
    </div>
  );
}

function DiffCard({
  preview,
  diff,
  onKeep,
  onDiscard,
}: {
  preview: ThemePreview;
  diff: ThemeDiff;
  onKeep: () => void;
  onDiscard: () => void;
}) {
  return (
    <section aria-label="What would change" className="space-y-3 rounded-lg border border-border-strong bg-elevated p-4">
      <h3 className="text-sm font-semibold">What would change</h3>
      {diff.rules_changed && <p className="text-sm">The limits changed (length, year or rating).</p>}
      <TitleList label={`Titles added (${diff.added_count})`} titles={diff.added} />
      <TitleList label={`Titles removed (${diff.removed_count})`} titles={diff.removed} />
      <TitleList label={`Tags added (${diff.tags_added.length})`} titles={diff.tags_added} />
      <TitleList label={`Tags removed (${diff.tags_removed.length})`} titles={diff.tags_removed} />
      <TitleList label={`Genres added (${diff.genres_added.length})`} titles={diff.genres_added} />
      <TitleList label={`Genres removed (${diff.genres_removed.length})`} titles={diff.genres_removed} />
      <p className="text-sm text-muted-foreground">
        Named titles: {diff.before_count} before, {diff.after_count} after. {diff.unchanged.length}{" "}
        {diff.unchanged.length === 1 ? "stays" : "stay"} the same. The new list has {preview.stats.after_rules}{" "}
        titles on your server after limits.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" onClick={onKeep}>
          Keep
        </Button>
        <Button type="button" variant="ghost" onClick={onDiscard}>
          Discard
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        Keep puts the new list in this editor. It is saved with the row when you press Save changes.
      </p>
    </section>
  );
}

function TitleList({ label, titles }: { label: string; titles: string[] }) {
  if (titles.length === 0) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <ul className="flex flex-wrap gap-1.5 text-sm">
        {titles.map((title) => (
          <li key={title}>
            <Badge variant="outline">{title}</Badge>
          </li>
        ))}
      </ul>
    </div>
  );
}

function UsageAndPause({ collection, tokensSpent }: { collection: Collection | null; tokensSpent: number }) {
  const pause = useSetAiPause();
  const pauseId = useId();
  if (collection === null) {
    return tokensSpent > 0 ? (
      <p className="text-sm text-muted-foreground">
        Used {tokensSpent.toLocaleString()} tokens so far in this editor, not saved yet.
      </p>
    ) : null;
  }
  const paused = pause.data?.ai_paused ?? collection.ai_paused;
  return (
    <div className="space-y-3 border-t pt-4">
      <p className="text-sm text-muted-foreground">
        Used {collection.ai_tokens.toLocaleString()} tokens on this row
        {tokensSpent > 0 && (
          <span>. {tokensSpent.toLocaleString()} more this edit, not saved yet</span>
        )}
        .
      </p>
      {collection.theme_id !== null && (
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <Label htmlFor={pauseId}>Pause AI for this row</Label>
            <p className="text-sm text-muted-foreground">
              A paused row keeps its list and keeps picking from it for each person. It just won’t spend tokens
              building or changing one. This takes effect at once, apart from Save changes.
            </p>
          </div>
          <Switch
            id={pauseId}
            checked={paused}
            disabled={pause.isPending}
            onCheckedChange={(next) => pause.mutate({ collectionId: collection.id, paused: next })}
          />
        </div>
      )}
      {pause.isError && (
        <p role="alert" className="text-sm text-destructive-text">
          {apiErrorMessage(pause.error, "Couldn’t change this. Try again.")}
        </p>
      )}
    </div>
  );
}
