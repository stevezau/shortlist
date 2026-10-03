import { useState } from "react";
import { Link } from "react-router";

import { MAX_SEEDS_LABEL } from "@/components/max-seeds-field";
import { RECENT_COUNT_LABEL } from "@/components/recent-count-field";
import { SaveStatus } from "@/components/save-status";
import { AiWebSearchCard } from "@/components/settings/ai-web-search-card";
import { RefreshDaysField } from "@/components/settings/refresh-days-field";
import { IdleHoldField } from "@/components/settings/idle-hold-field";
import { InlineKeyField } from "@/components/settings/inline-key-field";
import { RecencySlider } from "@/components/settings/recency-slider";
import { WatchedSlider } from "@/components/settings/watched-slider";
import { SettingDisclosure } from "@/components/settings/setting-disclosure";
import { SettingsNumberField } from "@/components/settings/number-field";
import { useSaveBarReport } from "@/components/settings/save-bar-context";
import {
  SettingBlock,
  SettingRow,
  SettingsPanel,
  SettingsSection,
} from "@/components/settings/section-layout";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import type { ColdStart } from "@/lib/cold-start";
import {
  asColdStart,
  COLD_START_HINTS,
  COLD_START_LABELS,
  COLD_STARTS,
} from "@/lib/cold-start";
import type { RatingSource } from "@/lib/rating-sources";
import {
  asRatingSource,
  RATING_LABELS,
  RATING_SOURCES,
} from "@/lib/rating-sources";
import { useAutosavedSettings } from "@/lib/autosave";
import {
  IDLE_HOLD_DAYS_DEFAULT,
  REFRESH_DAYS_DEFAULT,
  RECENCY_DEFAULT,
  WATCHED_PCT_DEFAULT,
} from "@/lib/constants";
import { hasTrakt, SOURCES } from "@/lib/sources";
import type { Settings } from "@/lib/types";

// Every source except AI web search — that one gets its own card (its toggle plus what it costs;
// the backend it searches with lives on the Connections card).
const SIMPLE_SOURCES = SOURCES.filter((s) => s.id !== "llm_web");

function readSources(settings: Settings): string[] {
  const value = settings["candidates.sources"];
  return Array.isArray(value)
    ? value.filter((x): x is string => typeof x === "string")
    : ["tmdb_similar", "tmdb_discover"];
}

/** A global 0..1 setting, edited as whole percent. */
function readPercent(
  settings: Settings,
  key: string,
  fallback: number,
): number {
  const value = Number(settings[key]);
  if (!Number.isFinite(value)) return fallback;
  return Math.round(Math.min(1, Math.max(0, value)) * 100);
}

/** A global setting that is already a whole number in its own units (days, counts). */
function readWholeNumber(
  settings: Settings,
  key: string,
  fallback: number,
): number {
  const value = Number(settings[key]);
  return Number.isFinite(value) ? Math.round(value) : fallback;
}

/** When an enabled source is missing its dependency, show how to satisfy it RIGHT HERE. */
function InlineFix({
  sourceId,
  settings,
}: {
  sourceId: string;
  settings: Settings;
}) {
  if (sourceId === "trakt" && !hasTrakt(settings)) {
    return (
      <InlineKeyField
        settingKey="trakt.client_id"
        service="trakt"
        label="Trakt API key"
        placeholder="Trakt app client id"
        hint="Paste your Trakt app client id to switch this source on — no trip to Connections."
        helpUrl="https://trakt.tv/oauth/applications"
        settings={settings}
      />
    );
  }
  return null;
}

export function RecommendationsSection({ settings }: { settings: Settings }) {
  const [enabled, setEnabled] = useState<string[]>(() => readSources(settings));
  const [watchedPct, setWatchedPct] = useState<number>(() =>
    readPercent(settings, "recommendations.watched_pct", WATCHED_PCT_DEFAULT),
  );
  const [refreshDays, setRefreshDays] = useState<number>(() =>
    readWholeNumber(
      settings,
      "recommendations.refresh_days",
      REFRESH_DAYS_DEFAULT,
    ),
  );
  const [idleHoldDays, setIdleHoldDays] = useState<number>(() =>
    readWholeNumber(
      settings,
      "recommendations.idle_hold_days",
      IDLE_HOLD_DAYS_DEFAULT,
    ),
  );
  const [recency, setRecency] = useState<number>(() =>
    readPercent(settings, "recommendations.recency", RECENCY_DEFAULT),
  );
  const [recentCount, setRecentCount] = useState<number>(() => {
    const value = Number(settings["recommendations.recent_count"]);
    return Number.isFinite(value) ? Math.min(25, Math.max(1, value)) : 10;
  });
  const [ratingSource, setRatingSource] = useState<RatingSource>(() =>
    asRatingSource(settings["recommendations.rating_source"]),
  );
  const [maxSeeds, setMaxSeeds] = useState<number>(() => {
    const value = Number(settings["recommendations.max_seeds"]);
    return Number.isFinite(value) ? Math.min(100, Math.max(5, value)) : 30;
  });
  const [minHistory, setMinHistory] = useState<number>(() => {
    const value = Number(settings["recommendations.min_history"]);
    return Number.isFinite(value) ? Math.min(100, Math.max(1, value)) : 10;
  });
  const [coldStart, setColdStart] = useState<ColdStart>(() =>
    asColdStart(settings["recommendations.cold_start"]),
  );
  const [usePlexRatings, setUsePlexRatings] = useState<boolean>(
    () => settings["recommendations.use_plex_ratings"] !== false,
  );
  const [dislikeThreshold, setDislikeThreshold] = useState<number>(() => {
    const value = Number(settings["recommendations.dislike_threshold"]);
    return Number.isFinite(value) ? Math.min(6, Math.max(0, value)) : 2;
  });

  const toggle = (id: string) =>
    setEnabled((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id],
    );

  // Persist the owner's INTENT (the enabled set as chosen). A source whose dependency isn't met yet
  // no-ops safely in the engine and shows an inline "here's what's needed" prompt — never a silent lie.
  const save = useAutosavedSettings(
    {
      enabled,
      watchedPct,
      refreshDays,
      idleHoldDays,
      recency,
      recentCount,
      maxSeeds,
      ratingSource,
      minHistory,
      coldStart,
      usePlexRatings,
      dislikeThreshold,
    },
    () => ({
      "candidates.sources": enabled,
      "recommendations.min_history": minHistory,
      "recommendations.cold_start": coldStart,
      "recommendations.use_plex_ratings": usePlexRatings,
      "recommendations.dislike_threshold": dislikeThreshold,
      "recommendations.watched_pct": watchedPct / 100,
      "recommendations.refresh_days": refreshDays,
      "recommendations.idle_hold_days": idleHoldDays,
      "recommendations.recency": recency / 100,
      "recommendations.recent_count": recentCount,
      "recommendations.max_seeds": maxSeeds,
      "recommendations.rating_source": ratingSource,
    }),
  );

  const inSaveBar = useSaveBarReport("recommendations", save);
  const webSearchOn = enabled.includes("llm_web");

  return (
    <>
      <SettingsSection
        id="sources"
        title="Title sources"
        description="Where each person’s candidates come from before they’re ranked. These are defaults; each row can override them in its editor."
      >
        {!inSaveBar && <SaveStatus isPending={save.isPending} isError={save.isError} error={save.error} saved={save.saved} onRetry={save.retry} />}
        <SettingsPanel>
          {SIMPLE_SOURCES.map((source) => (
            <SettingRow
              key={source.id}
              title={source.label}
              description={source.desc}
              control={<Switch checked={enabled.includes(source.id)} onCheckedChange={() => toggle(source.id)} aria-label={`Enable ${source.label}`} />}
            >
              {enabled.includes(source.id) && <InlineFix sourceId={source.id} settings={settings} />}
            </SettingRow>
          ))}
          {enabled.length === 0 && <p role="status" className="px-4 py-3 text-xs text-warning sm:px-5">Nothing enabled — Shortlist falls back to its defaults (TMDB similar + discover). Turn on at least one source to choose your own.</p>}
          <SettingDisclosure title="Web search" value={webSearchOn ? "On" : "Off"} description="Discovery beyond the usual sources, through AI & web search." defaultOpen={webSearchOn}>
            <p className="text-xs leading-relaxed text-muted-foreground">The TMDB sources find titles without AI. Set the provider to <strong>None</strong> in <Link to="/settings/connections#connection-llm" className="font-medium text-primary hover:underline">Connections</Link> and you still get full rows, ranked by score with plain reasons.</p>
            <AiWebSearchCard settings={settings} enabled={webSearchOn} onToggle={() => toggle("llm_web")} />
          </SettingDisclosure>
        </SettingsPanel>
      </SettingsSection>

      <SettingsSection id="refresh" title="Refresh & variety" description="When a row changes, and how much of it may be familiar.">
        <SettingsPanel>
          <SettingBlock
            title="Titles refresh every"
            htmlFor="refresh-days"
            description="Longer is stickier and cheaper, shorter is fresher. Each row keeps its own schedule; its Order setting decides the order."
          >
            <RefreshDaysField id="refresh-days" value={refreshDays} onChange={setRefreshDays} />
          </SettingBlock>
          <SettingBlock title="Already-watched titles" htmlFor="watched-pct" description="How much of a row may be familiar." value={`Up to ${watchedPct}%`}>
            <WatchedSlider id="watched-pct" value={watchedPct} onChange={setWatchedPct} />
          </SettingBlock>
          <SettingBlock title="Recent releases" htmlFor="recency" description="Give newer titles more weight without filtering older ones out." value={`${recency}% preference`}>
            <RecencySlider id="recency" value={recency} onChange={setRecency} />
          </SettingBlock>
          <SettingDisclosure title="More recommendation controls" value="Show" description="Watch history, ratings and inactive viewers. Plex ratings are server-wide and do not change shared rows.">
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="idle-hold-days">
                Hold rows for inactive viewers
              </Label>
              <p className="text-sm text-muted-foreground">
                Someone who hasn&rsquo;t watched anything since their row was
                built has nothing new to base fresh picks on, so the row can wait
                — which also saves a write to Plex for every row held. Off by
                default.
              </p>
              <IdleHoldField
                id="idle-hold-days"
                value={idleHoldDays}
                cadence={refreshDays}
                scope="global"
                onChange={setIdleHoldDays}
              />
            </div>
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="max-seeds">{MAX_SEEDS_LABEL}</Label>
              <p className="text-sm text-muted-foreground">
                How far back Shortlist looks when working out someone&rsquo;s
                taste. Applies to every source. Fewer makes a row tighter and
                more about a couple of things; more covers more of their taste.
              </p>
              <div className="flex items-center gap-2">
                <SettingsNumberField
                  id="max-seeds"

                  min={5}
                  max={100}
                  value={maxSeeds}
                  onCommit={setMaxSeeds}
                  className="w-24"
                />
                <span className="text-sm text-muted-foreground">watches</span>
              </div>
              <div className="space-y-2 pt-2">
                <Label htmlFor="recent-count">{RECENT_COUNT_LABEL}</Label>
                {/* No "cached for 7 days" here any more: the AI web search card above owns the
                    cost story and already says it, in more detail. */}
                <p className="text-sm text-muted-foreground">
                  How many of those the AI web search runs a &ldquo;what to
                  watch if you liked X&rdquo; search for, newest first. Higher
                  than the number above changes nothing.
                </p>
                <div className="flex items-center gap-2">
                  <SettingsNumberField
                    id="recent-count"

                    min={1}
                    max={25}
                    value={recentCount}
                    onCommit={setRecentCount}
                  className="w-24"
                  />
                  <span className="text-sm text-muted-foreground">watches</span>
                </div>
              </div>
            </div>
            {/* The switch and the line it draws stay in one block: "respect ratings" says nothing
                about WHICH ratings, and a threshold with no switch above it can't be turned off. */}
            <div className="space-y-2 border-t pt-4">
              <div className="flex items-start justify-between gap-4">
                <div className="space-y-0.5">
                  <Label htmlFor="use-plex-ratings">Respect Plex ratings</Label>
                  {/* Every other setting in this card advertises "any row can choose its own", so
                      staying silent about scope reads as "presumably also per row". Say it: a
                      rating is a fact about a PERSON, and "respect what they disliked on the movies
                      row but ignore it on the TV row" is not a preference anyone holds. */}
                  <p className="text-sm text-muted-foreground">
                    When someone rates a title low in Plex, stop using it to
                    find similar things for them. Server-wide, and shared rows
                    ignore ratings entirely &mdash; one person&rsquo;s opinion
                    shouldn&rsquo;t reshape a row everyone sees.
                  </p>
                </div>
                <Switch
                  id="use-plex-ratings"
                  checked={usePlexRatings}
                  onCheckedChange={setUsePlexRatings}
                  aria-label="Respect Plex ratings"
                />
              </div>
              {usePlexRatings && (
                <div className="space-y-2 pt-2">
                  <Label htmlFor="dislike-threshold">
                    Treat as &ldquo;didn&rsquo;t like it&rdquo;
                  </Label>
                  <p className="text-sm text-muted-foreground">
                    At or below this rating, a title stops shaping their picks.
                    A thumbs-down in Plex counts as 1 star.
                  </p>
                  <div className="flex items-center gap-2">
                    <SettingsNumberField
                      id="dislike-threshold"

                      min={0.5}
                      max={3}
                      step={0.5}
                      // Stored on Plex's 0..10 scale, shown as stars — the scale people actually see
                      // in Plex. Halving/doubling here rather than storing stars keeps the setting in
                      // the same units as the raw `userRating` every comparison uses.
                      value={dislikeThreshold / 2}
                      onCommit={(value) => setDislikeThreshold(value * 2)}
                  className="w-24"
                    />
                    <span className="text-sm text-muted-foreground">
                      stars and below
                    </span>
                  </div>
                </div>
              )}
            </div>
            {/* Threshold and consequence together: the number is meaningless without knowing what
                happens below it, and the choice is meaningless without knowing where the line is. */}
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="min-history">Enough watch history</Label>
              <p className="text-sm text-muted-foreground">
                How many titles someone needs watched before Shortlist
                recommends from <strong>their</strong> taste. Below it they get
                whatever you choose next.
              </p>
              <div className="flex items-center gap-2">
                <SettingsNumberField
                  id="min-history"

                  min={1}
                  max={100}
                  value={minHistory}
                  onCommit={setMinHistory}
                  className="w-24"
                />
                <span className="text-sm text-muted-foreground">
                  watched titles
                </span>
              </div>
            </div>
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="cold-start">
                When someone hasn&rsquo;t watched enough
              </Label>
              <p className="text-sm text-muted-foreground">
                A row named after one title (
                <span className="font-mono">{"{top_seed}"}</span>) is the one
                worth skipping &mdash; it has no favourite to name itself after,
                so it falls back to a plain title.
              </p>
              <select
                id="cold-start"
                value={coldStart}
                onChange={(e) => setColdStart(asColdStart(e.target.value))}
                className="h-9 w-full max-w-md rounded-md border bg-background px-3 text-sm"
              >
                {COLD_STARTS.map((choice) => (
                  <option key={choice} value={choice}>
                    {COLD_START_LABELS[choice]}
                  </option>
                ))}
              </select>
              <p className="text-sm text-muted-foreground">
                {COLD_START_HINTS[coldStart]}
              </p>
            </div>
            <div className="space-y-1.5 border-t pt-4">
              <Label htmlFor="rating-source">Rate titles using</Label>
              <p className="text-sm text-muted-foreground">
                Which score a row set to <strong>Highest rated</strong> sorts
                on. Anything but TMDB needs an MDBList key in{" "}
                <Link
                  to="/settings/connections#connection-mdblist"
                  className="font-medium underline"
                >
                  Connections
                </Link>
                , and falls back to TMDB without one.
              </p>
              <select
                id="rating-source"
                value={ratingSource}
                onChange={(e) =>
                  setRatingSource(asRatingSource(e.target.value))
                }
                className="h-9 w-56 rounded-md border bg-background px-3 text-sm"
              >
                {RATING_SOURCES.map((source) => (
                  <option key={source} value={source}>
                    {RATING_LABELS[source]}
                  </option>
                ))}
              </select>
            </div>
          </SettingDisclosure>
        </SettingsPanel>
      </SettingsSection>
    </>
  );
}
