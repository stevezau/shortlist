import type { ReactNode } from "react";
import { useId } from "react";

import { MAX_SEEDS_LABEL } from "@/components/max-seeds-field";
import {
  RECENT_COUNT_LABEL,
  RecentCountField,
} from "@/components/recent-count-field";
import { InheritableField } from "@/components/rows/inheritable-field";
import { RowAiInstructionsField } from "@/components/rows/row-ai-instructions-field";
import { RowLimitsFields } from "@/components/rows/row-limits-fields";
import { RowMaxSeedsSetting } from "@/components/rows/row-max-seeds-setting";
import {
  effectiveSources,
  RowSourcesField,
} from "@/components/rows/row-sources-field";
import { RecencySlider } from "@/components/settings/recency-slider";
import { WatchedSlider } from "@/components/settings/watched-slider";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  recencyGlobal,
  recencySeed,
  recentCountGlobal,
  recentCountSeed,
  watchedPctGlobal,
  watchedPctSeed,
} from "@/lib/row-globals";
import type { RowFill, RowSettingKey } from "@/lib/row-kinds";
import {
  aiInstructionsInert,
  sourceShortLabel,
  webSearchProvider,
} from "@/lib/sources";
import type { CollectionInput, Settings } from "@/lib/types";

/**
 * The part of "What goes in it" that depends on the row's kind: where picks come from and which
 * titles may be in it. Each setting renders only when the kind shows it (`visibleSettings`); a
 * control the engine ignores promises a behaviour.
 *
 * A Watch it again row groups its own under "When their finished titles run out": they only decide
 * the new picks that fill the row once the rewatches are used up.
 */
export function RowContentsFields({
  input,
  set,
  shown,
  settings,
  fill,
  takeTurns = null,
}: {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
  shown: ReadonlySet<RowSettingKey>;
  settings: Settings | undefined;
  fill: RowFill;
  /** Watch it again's "Take turns": the engine rotates the fill-up's seed list, so it sits here. */
  takeTurns?: ReactNode;
}) {
  const fillUpId = useId();
  const isSeasonal = input.seasons.length > 0;

  const sources = shown.has("candidate_sources") && (
    <div data-setting="candidate_sources" className="space-y-2">
      <RowSourcesField
        value={input.candidate_sources}
        onChange={(candidate_sources) => set({ candidate_sources })}
      />
      {/* The engine drops it on a seasonal row (`effective_row_sources`): its searches are per
          watched title, not seasonal, so nearly all it found would be filtered out. */}
      {isSeasonal && (
        <p className="text-sm text-muted-foreground">
          AI web search isn’t used on a seasonal row — it searches from what
          they watched, not the season, so almost everything it found would be
          thrown away. The season’s own titles are added instead.
        </p>
      )}
    </div>
  );

  const recentCount = shown.has("recent_count") && (
    <InheritableField
      setting="recent_count"
      label={RECENT_COUNT_LABEL}
      description={`AI web search looks up one watch at a time — “what to watch if you liked X”. This is how many of their most recent watches it asks about: the front slice of the same list “${MAX_SEEDS_LABEL}” sets. More gives wider results and takes more searches. It changes nothing for the other sources.`}
      inheriting={input.recent_count === null}
      globalValue={recentCountGlobal(settings)}
      onToggle={(on) =>
        set({ recent_count: on ? null : recentCountSeed(settings) })
      }
    >
      <RecentCountField
        label=""
        value={input.recent_count ?? 0}
        onChange={(next) => set({ recent_count: next })}
      />
    </InheritableField>
  );

  // Same test as the server's preview (`api/ai.py`). While settings load neither is known, so nothing
  // is claimed.
  const aiInstructions = shown.has("ai_instructions") && (
    <div data-setting="ai_instructions">
      <RowAiInstructionsField
        value={input.ai_instructions}
        onChange={(ai_instructions) => set({ ai_instructions })}
        otherSources={effectiveSources(input.candidate_sources, settings)
          .filter((source) => source !== "llm_web")
          .map(sourceShortLabel)}
        backend={settings ? webSearchProvider(settings) : "native"}
        inert={settings === undefined ? null : aiInstructionsInert(settings)}
      />
    </div>
  );

  // Anything that can hold shows, which is what the API accepts — it refuses this only on a
  // movies-only row, where `visibleSettings` leaves it out.
  const unstarted = shown.has("unstarted_only") && (
    <div
      data-setting="unstarted_only"
      className="flex items-start justify-between gap-4 border-t pt-4"
    >
      <div className="space-y-1">
        <Label htmlFor="row-unstarted">
          Only series they haven&rsquo;t started
        </Label>
        <p className="text-sm text-muted-foreground">
          Drops any show they&rsquo;ve watched even one episode of. This only
          changes anything if you&rsquo;ve allowed already-watched titles above
          — at 0% those are already left out.
        </p>
      </div>
      <Switch
        id="row-unstarted"
        aria-label="Only series they have not started"
        checked={input.unstarted_only}
        onCheckedChange={(unstarted_only) => set({ unstarted_only })}
      />
    </div>
  );

  const watched = shown.has("watched_pct") && (
    <InheritableField
      setting="watched_pct"
      label="Already-watched titles"
      labelFor="row-watched-pct"
      description="How much of this row can be things they have already finished. At 0 it is all new suggestions."
      inheriting={input.watched_pct === null}
      globalValue={watchedPctGlobal(settings)}
      onToggle={(on) =>
        set({ watched_pct: on ? null : watchedPctSeed(settings) })
      }
      after={unstarted}
    >
      <WatchedSlider
        id="row-watched-pct"
        value={Math.round((input.watched_pct ?? 0) * 100)}
        onChange={(pct) => set({ watched_pct: pct / 100 })}
      />
    </InheritableField>
  );

  // Every row that searches, including one that follows a watch: which titles win is a free choice
  // there even though its cadence is forced. A shared row has no scored pool for it to act on.
  const recency = shown.has("recency") && (
    <InheritableField
      setting="recency"
      label="Recent releases"
      labelFor="row-recency"
      description="How much a title’s release date counts for this row — up for “new and notable”, down for one that digs up older films. Older titles are never excluded, they just have to be a better match."
      inheriting={input.recency === null}
      globalValue={recencyGlobal(settings)}
      onToggle={(on) => set({ recency: on ? null : recencySeed(settings) })}
    >
      <RecencySlider
        id="row-recency"
        value={Math.round((input.recency ?? 0) * 100)}
        onChange={(pct) => set({ recency: pct / 100 })}
      />
    </InheritableField>
  );

  if (fill === "popular") {
    // A shared row IS the count: the titles the most people on this server have watched, most
    // watched first. There is no search to configure, so there is nothing else to show here.
    return (
      <p className="text-sm text-muted-foreground">
        A shared row is simply your server’s most-watched titles, most watched
        first — so there is nothing to choose about where its picks come from.
        Set how many people must have watched a title under &ldquo;What counts
        as popular&rdquo;, and pick its libraries above.
      </p>
    );
  }

  if (fill === "again") {
    return (
      <section aria-labelledby={fillUpId} className="space-y-4 border-t pt-4">
        <div className="space-y-1">
          <h3 id={fillUpId} className="text-sm font-semibold">
            When their finished titles run out
          </h3>
          <p className="text-sm text-muted-foreground">
            The rest of the row is filled with new picks, found like this.
          </p>
        </div>
        {shown.has("max_seeds") && (
          <RowMaxSeedsSetting input={input} set={set} settings={settings} />
        )}
        {takeTurns}
        {sources}
        {recentCount}
        {aiInstructions}
        {recency}
        {shown.has("limits") && <RowLimitsFields input={input} set={set} />}
      </section>
    );
  }

  return (
    <>
      {sources}
      {recentCount}
      {aiInstructions}
      {watched}
      {/* Defensive: every kind that shows this also shows the cap it sits under. */}
      {!shown.has("watched_pct") && unstarted}
      {recency}
      {shown.has("limits") && <RowLimitsFields input={input} set={set} />}
    </>
  );
}
