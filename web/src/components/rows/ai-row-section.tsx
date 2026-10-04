import { Loader2, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";

import { DiffCard } from "@/components/rows/ai-diff-card";
import { AiHandEdit } from "@/components/rows/ai-hand-edit";
import { AiPromptsSection } from "@/components/rows/ai-prompts-section";
import { ExploreSection } from "@/components/rows/explore-section";
import { OverTimeFields } from "@/components/rows/over-time-fields";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { ErrorState, QueryBoundary } from "@/components/query-boundary";
import { apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDate } from "@/lib/format";
import {
  ruleChips,
  savedStats,
  toThemeIn,
  themeGuidance,
  useSetAiPause,
  useTheme,
  useThemeCapabilities,
  useThemePreview,
  useThemePrompts,
  withoutRule,
  type PendingTheme,
  type RuleChip,
} from "@/lib/themes";
import type { Collection, CollectionInput, Theme, ThemePreview, ThemeSaveInput, ThemeStats } from "@/lib/types";

type ThemeIn = ThemeSaveInput["draft"];

/** The API's limit on a brief (`PreviewIn.brief`). */
const MAX_BRIEF = 1000;
/** How many titles the sample shows before "and N more". */
const SAMPLE_SIZE = 12;

const PAUSED_REASON = "AI is paused for this row. Resume it below to write or adjust its list.";

type Counts = Pick<ThemeStats, "named" | "resolved" | "in_library" | "after_rules"> & {
  /** The AI's own titles left in the row; absent on a list saved before it was counted. */
  ai_kept?: number;
  truncated?: boolean;
  runtime_total?: number;
  runtime_checked?: number;
};

/**
 * An AI row's list (#138), in What goes in: describe it, write it with one AI call, adjust it in words, and
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
  onChange,
  focusBrief = false,
}: {
  /** Bring the brief box into view and focus it: a new row started from the Describe a row template. */
  focusBrief?: boolean;
  input: CollectionInput;
  /** The saved row; null while a new row is being added. */
  collection: Collection | null;
  /** The list built or edited here and not saved yet. */
  pending: PendingTheme | null;
  /** Tokens this edit has spent on AI calls so far, saved or not. */
  tokensSpent: number;
  onPending: (next: PendingTheme | null) => void;
  onSpent: (tokens: number) => void;
  /** Writes Explore and the over-time controls into the row form. */
  onChange?: (patch: Partial<CollectionInput>) => void;
}) {
  const capabilities = useThemeCapabilities();
  const savedId = collection?.theme_id ?? null;
  const saved = useTheme(savedId);
  // A query that is switched off (a row with no list yet) also reads as pending, so ask for the id too.
  const waitingForSaved = savedId !== null && saved.isPending;
  // Explore and the over-time controls belong to a row that has a theme, saved or built in this editor.
  const hasTheme = savedId !== null || pending !== null;
  const change = onChange ?? (() => undefined);

  return (
    <div className="space-y-5">
      <p className="text-sm text-muted-foreground">
        Describe the row. The AI writes a list of about 60 titles once. Every night Shortlist picks each person’s best
        matches from it, with no AI.
      </p>
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
              focusBrief={focusBrief}
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
      {hasTheme && (
        <>
          <ExploreSection input={input} collection={collection} onChange={change} />
          <OverTimeFields input={input} ownSlug={collection?.slug ?? null} onChange={change} />
        </>
      )}
      <AdvancedPrompts input={input} onChange={change} />
      <UsageAndPause collection={collection} tokensSpent={tokensSpent} />
    </div>
  );
}

/** The AI's instructions, folded away: most owners never need them, and they are the same data as ever. */
function AdvancedPrompts({
  input,
  onChange,
}: {
  input: CollectionInput;
  onChange: (patch: Partial<CollectionInput>) => void;
}) {
  const capabilities = useThemeCapabilities();
  if (!capabilities.data?.ai) return null;
  return (
    <details className="group rounded-lg border bg-elevated">
      <summary className="cursor-pointer list-none rounded-lg px-4 py-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
        Advanced: how the AI is instructed
      </summary>
      <div className="border-t p-4">
        <AiPromptsSection input={input} set={onChange} />
      </div>
    </details>
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
  focusBrief,
}: {
  focusBrief: boolean;
  input: CollectionInput;
  collection: Collection | null;
  saved: ReturnType<typeof useTheme>;
  waitingForSaved: boolean;
  pending: PendingTheme | null;
  onPending: (next: PendingTheme | null) => void;
  onSpent: (tokens: number) => void;
}) {
  const briefId = useId();
  const briefHintId = useId();
  const briefRef = useRef<HTMLTextAreaElement>(null);
  // The brief is the one thing a new AI row needs, and it sits below Name & look and Who gets it.
  useEffect(() => {
    if (!focusBrief) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    briefRef.current?.scrollIntoView?.({ behavior: reduce ? "auto" : "smooth", block: "center" });
    briefRef.current?.focus({ preventScroll: true });
  }, [focusBrief]);
  const builder = useThemePreview();
  const prompts = useThemePrompts();
  const [typed, setTyped] = useState<string | null>(null);
  const [change, setChange] = useState("");
  const [refinement, setRefinement] = useState<ThemePreview | null>(null);
  const [failed, setFailed] = useState<{ message: string; retry: () => void } | null>(null);
  const [working, setWorking] = useState<"build" | "adjust">("build");

  const savedTheme = saved.data ?? null;
  const shown: Theme | null = pending?.draft ?? savedTheme;
  const brief = typed ?? shown?.brief ?? "";
  const paused = collection?.ai_paused === true;
  const mode = input.ai_instructions.mode;
  // "Add to the default" is the default's words plus the owner's, so it can't be sent before the default is known.
  const guidanceReady = mode !== "add" || prompts.isSuccess;
  const guidance = guidanceReady ? themeGuidance(input.ai_instructions, prompts.data?.guidance ?? "") : "";
  const media = input.media;

  const run = async (
    request: { brief?: string; change?: string; current_theme_id?: number; current_draft?: ThemeIn },
    then: (p: ThemePreview) => void,
  ) => {
    setFailed(null);
    setWorking(request.change !== undefined ? "adjust" : "build");
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

  // An unsaved list is sent whole, so adjusting works before the row is saved; the saved one is read by id.
  const refine = () =>
    run(
      pending ? { change, current_draft: toThemeIn(pending) } : { change, current_theme_id: savedTheme?.id ?? undefined },
      (preview) => setRefinement(preview),
    );

  const buildReason = paused ? PAUSED_REASON : !guidanceReady ? "Loading the default instructions…" : null;
  const clearRule = (chip: RuleChip) => {
    if (shown === null) return;
    // The counts described the list with the limit on, so they are dropped rather than left to mislead.
    onPending({
      draft: { ...shown, rules: withoutRule(shown.rules, chip) },
      stats: null,
      origin: shown.origin === "manual" ? "manual" : "ai",
    });
  };

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <Label htmlFor={briefId}>What should this row be?</Label>
        <Textarea
          id={briefId}
          ref={briefRef}
          rows={3}
          maxLength={MAX_BRIEF}
          value={brief}
          onChange={(event) => setTyped(event.target.value)}
          aria-describedby={briefHintId}
          placeholder="e.g. Films with a twist ending that make you want to watch them again"
        />
        <p id={briefHintId} className="text-sm text-muted-foreground">
          Used once, to write the list. It isn’t shown on Plex.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            variant="outline"
            onClick={() => void build()}
            loading={builder.isPending}
            disabled={!brief.trim() || buildReason !== null}
          >
            {shown ? "Rewrite the list" : "Write the list"}
          </Button>
          <p className="text-sm text-muted-foreground">Uses your AI provider once.</p>
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

      {builder.isPending && <Working doing={working} />}

      {waitingForSaved && pending === null ? (
        <LoadingList />
      ) : shown === null && !builder.isPending && !saved.isError ? (
        <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
          No list yet. Say what the row should be, then press Write the list. The AI names titles; Shortlist checks each
          one against TMDB and your libraries.
        </p>
      ) : (
        shown && (
          <ListCard
            theme={shown}
            counts={pending ? pending.stats : savedStats(shown)}
            unsaved={pending !== null}
            rowCoversBoth={input.media === "both"}
            onClearRule={clearRule}
          >
            <AdjustList
              value={change}
              reason={buildReason}
              busy={builder.isPending}
              onChange={setChange}
              onAdjust={() => void refine()}
            />
          </ListCard>
        )
      )}
      {shown && input.theme_mode === "explore" && (
        <p className="text-sm text-muted-foreground">This list is used until each person’s own list is ready.</p>
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

    </div>
  );
}

/** Say what to change about the list on the card. Works before the row is saved. */
function AdjustList({
  value,
  reason,
  busy,
  onChange,
  onAdjust,
}: {
  value: string;
  /** Why adjusting can't be done right now, or null. */
  reason: string | null;
  busy: boolean;
  onChange: (next: string) => void;
  onAdjust: () => void;
}) {
  const id = useId();
  return (
    <div className="space-y-2 border-t pt-3">
      <Label htmlFor={id}>Adjust the list</Label>
      <Textarea
        id={id}
        rows={2}
        maxLength={MAX_BRIEF}
        value={value}
        disabled={reason !== null}
        onChange={(event) => onChange(event.target.value)}
        placeholder="e.g. Less gore, and more from the last ten years"
      />
      <Button type="button" variant="outline" onClick={onAdjust} loading={busy} disabled={!value.trim() || reason !== null}>
        Adjust the list
      </Button>
      {reason && <p className="text-sm text-warning">{reason}</p>}
      <p className="text-sm text-muted-foreground">
        Tell the AI what to change. What you wrote above stays as it is, and you see what would be added and removed
        before anything is kept.
      </p>
    </div>
  );
}

/** What the AI call is doing, for the minutes it can take: a long wait with no words reads as a hang. */
function Working({ doing }: { doing: "build" | "adjust" }) {
  const what = doing === "build" ? "Writing the list" : "Adjusting the list";
  return (
    <div
      role="status"
      aria-label={what}
      className="flex items-start gap-3 rounded-lg border border-dashed p-4 text-sm text-muted-foreground"
    >
      <Loader2 aria-hidden="true" className="mt-0.5 size-4 shrink-0 motion-safe:animate-spin" />
      <p>{what}… this can take a couple of minutes. Keep this page open.</p>
    </div>
  );
}

function ListCard({
  theme,
  counts,
  unsaved,
  rowCoversBoth,
  onClearRule,
  children,
}: {
  theme: Theme;
  counts: Counts | null;
  unsaved: boolean;
  rowCoversBoth: boolean;
  onClearRule: (chip: RuleChip) => void;
  children?: ReactNode;
}) {
  const rules = ruleChips(theme.rules);
  const sample = theme.picks.slice(0, SAMPLE_SIZE);
  const more = theme.picks.length - sample.length;
  const tagMatches = counts?.ai_kept === undefined ? 0 : Math.max(0, counts.after_rules - counts.ai_kept);
  const only = rowCoversBoth && theme.media.length === 1 ? theme.media[0] : null;
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

      {only && (
        <p className="text-sm text-muted-foreground">
          {only === "movie"
            ? "Films only — the AI named no TV series, so this row stays out of your TV libraries."
            : "TV series only — the AI named no films, so this row stays out of your movie libraries."}
        </p>
      )}
      {theme.topped_up_at && (
        <p className="text-sm text-muted-foreground">
          {`Topped up once on ${formatDate(theme.topped_up_at, { dateOnly: true })}`}
          {theme.stats.topped_up != null &&
            ` — ${theme.stats.topped_up} more ${theme.stats.topped_up === 1 ? "title" : "titles"}`}
        </p>
      )}
      {counts && (
        <dl className={cn("grid gap-3", counts.ai_kept === undefined ? "grid-cols-2" : "grid-cols-3")}>
          <Count label="Named by the AI" value={counts.named} />
          <Count label="Found on TMDB" value={counts.resolved} />
          {counts.ai_kept !== undefined && <Count label="On your server" value={counts.ai_kept} />}
        </dl>
      )}
      {counts?.ai_kept !== undefined && (
        <div className="space-y-0.5 text-sm">
          <p>
            {`${counts.ai_kept} of the AI’s ${counts.named} ${counts.named === 1 ? "title is" : "titles are"} on your server.`}
          </p>
          {tagMatches > 0 && <p>{`Plus ${tagMatches} more that match its tags and genres.`}</p>}
        </div>
      )}
      {counts?.truncated && (
        <p role="status" className="text-sm text-warning">
          The AI’s list was cut short; {counts.named} {counts.named === 1 ? "title" : "titles"} kept. Rewrite it for a
          fuller list.
        </p>
      )}
      {counts?.runtime_total != null && counts.runtime_checked != null && counts.runtime_checked < counts.runtime_total && (
        <p className="text-sm text-muted-foreground">
          Checked running time for {counts.runtime_checked} of {counts.runtime_total} titles; the nightly run checks the
          rest.
        </p>
      )}
      {counts?.ai_kept !== undefined && counts.ai_kept < counts.named && (
        <p className="text-sm text-muted-foreground">
          Titles the AI named that aren’t on your server, or that your limits rule out, are left out. An AI row only
          picks from what you already have.
        </p>
      )}
      {rules.length > 0 && (
        <ul aria-label="Limits" className="flex flex-wrap gap-1.5">
          {rules.map((rule) => (
            <li key={rule.label}>
              <Badge variant="outline" className="gap-1 pr-1">
                {rule.label}
                <button
                  type="button"
                  aria-label={`Remove limit: ${rule.label}`}
                  onClick={() => onClearRule(rule)}
                  className="rounded-full p-0.5 hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X aria-hidden="true" className="size-3" />
                </button>
              </Badge>
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
      {children}
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
              writing or adjusting one. This takes effect at once, apart from Save changes.
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
