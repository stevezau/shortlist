import { useState } from "react";
import { Link } from "react-router";

import { MAX_SEEDS_LABEL } from "@/components/max-seeds-field";
import { RECENT_COUNT_LABEL } from "@/components/recent-count-field";
import { SaveStatus } from "@/components/save-status";
import { AiWebSearchCard } from "@/components/settings/ai-web-search-card";
import { RefreshDaysField } from "@/components/settings/refresh-days-field";
import { InlineKeyField } from "@/components/settings/inline-key-field";
import { RecencySlider } from "@/components/settings/recency-slider";
import { WatchedSlider } from "@/components/settings/watched-slider";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
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
      "recommendations.recency": recency / 100,
      "recommendations.recent_count": recentCount,
      "recommendations.max_seeds": maxSeeds,
      "recommendations.rating_source": ratingSource,
    }),
  );

  return (
    <section aria-labelledby="recs-heading" className="space-y-6">
      <header className="space-y-1 border-b pb-4">
        <h2 id="recs-heading" className="text-lg font-semibold">
          Finding titles
        </h2>
        <p className="text-sm text-muted-foreground">
          Where Shortlist looks for titles to suggest, and how AI enhances the
          search. This is the <strong>default every row inherits</strong> — any
          row can override in its editor.
        </p>
      </header>

      {/* Each sub-heading hugs the card it labels (tight gap inside, wide gap between), and sits a
          clear rank below the section title — three same-weight headings under one h2 read as three
          separate sections rather than as the parts of this one. */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Title sources
        </h3>
        <Card>
          <CardContent className="space-y-4 pt-6">
            <p className="text-sm text-muted-foreground">
              Shortlist gathers from every source you enable, keeps only titles
              already in your library, then ranks them. More sources → wider
              reach.
            </p>
            {SIMPLE_SOURCES.map((source) => (
              <div key={source.id} className="space-y-2">
                <div className="flex items-start justify-between gap-4">
                  <div className="space-y-0.5">
                    <p className="text-sm font-medium">{source.label}</p>
                    <p className="text-sm text-muted-foreground">
                      {source.desc}
                    </p>
                  </div>
                  <Switch
                    checked={enabled.includes(source.id)}
                    onCheckedChange={() => toggle(source.id)}
                    aria-label={`Enable ${source.label}`}
                  />
                </div>
                {enabled.includes(source.id) && (
                  <InlineFix sourceId={source.id} settings={settings} />
                )}
              </div>
            ))}
            {enabled.length === 0 && (
              // Empty isn't "no discovery" — the engine floors it to its defaults, so say so out loud
              // (the setting must never read as fully off while a run still uses two sources). It's an
              // advisory, not an error, so it's role="status".
              <p role="status" className="text-sm text-warning">
                Nothing enabled — Shortlist falls back to its defaults (TMDB
                similar + discover). Turn on at least one source to choose your
                own.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          AI enhancement
        </h3>
        <div className="space-y-1.5 rounded-md border bg-muted/40 p-4 text-sm text-muted-foreground">
          <p className="font-medium text-foreground">How AI is used</p>
          <p>
            The <strong>TMDB</strong> sources above use no AI — just the free
            TMDB key — and find most titles.
          </p>
          <p>
            <strong>AI web search</strong> below is optional but proven
            valuable: it searches the web for acclaimed titles TMDB misses,
            using your AI provider.
          </p>
          <p>
            Prefer no AI at all? Leave the AI provider set to{" "}
            <strong>None</strong> in{" "}
            <Link to="/settings#connections" className="font-medium underline">
              Connections
            </Link>{" "}
            — you still get full rows, ranked by score with plain reasons.
          </p>
        </div>

        <AiWebSearchCard
          settings={settings}
          enabled={enabled.includes("llm_web")}
          onToggle={() => toggle("llm_web")}
        />
      </div>

      <div className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Row behavior
        </h3>
        <Card>
          <CardContent className="space-y-4 pt-6">
            <div className="space-y-2">
              <Label htmlFor="watched-pct">Already-watched titles</Label>
              <p className="text-sm text-muted-foreground">
                How much of a row may be things a person has already finished.
                The default every row inherits; any row can choose its own.
              </p>
              <WatchedSlider
                id="watched-pct"
                value={watchedPct}
                onChange={setWatchedPct}
              />
            </div>
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="refresh-days">How often rows rebuild</Label>
              <p className="text-sm text-muted-foreground">
                How often a row swaps in new titles. On the nights in between it
                keeps the same set and nothing is rewritten to Plex; on its
                rebuild night the strongest picks stay and the weakest are
                swapped for new ones. Longer = stickier and cheaper; shorter =
                fresher. This decides <strong>which</strong> titles a row holds
                — the order they appear in is that row’s own{" "}
                <strong>Order</strong> setting. The default every row inherits;
                any row can choose its own.
              </p>
              <RefreshDaysField
                id="refresh-days"
                value={refreshDays}
                onChange={setRefreshDays}
              />
            </div>
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="recency">Recent releases</Label>
              <p className="text-sm text-muted-foreground">
                How much a title’s <strong>release date</strong> counts when
                ranking it. Without this, a well-rated 1996 film beats a 2024
                one every time, and rows fill up with older titles. It’s a
                preference, not a filter — old titles still reach rows, they
                just have to be a better match. Distinct from{" "}
                <strong>How often rows rebuild</strong> above, which is how
                often a row re-picks rather than which titles win. The default
                every row inherits; any row can choose its own.
              </p>
              <RecencySlider
                id="recency"
                value={recency}
                onChange={setRecency}
              />
            </div>
            {/* The BROADER knob first. These two were the other way round, which gave no clue that
                this one governs every source and the one below only slices the front of that same
                list — `candidates.py` searches `seeds[:recent_count]`. Both labels are imported, not
                retyped: they are shared with the row editor, and a setting that goes by two names
                across two screens is the bug this pairing already shipped once. */}
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="max-seeds">{MAX_SEEDS_LABEL}</Label>
              <p className="text-sm text-muted-foreground">
                Shortlist works backwards from what someone recently watched.
                This is how far back it looks &mdash; and it applies to every
                source, not just the AI one. Fewer makes a row tighter and more
                about a couple of things; more covers more of their taste. Any
                row can set its own, and a row named after one title (
                <span className="font-mono">{"{top_seed}"}</span>) should
                &mdash; the row editor prompts you there. This server-wide
                default stops at 5 for that reason: a row covering movies and TV
                needs at least one of each to work from.
              </p>
              <div className="flex items-center gap-2">
                <Input
                  id="max-seeds"
                  type="number"
                  min={5}
                  max={100}
                  value={maxSeeds}
                  onChange={(e) =>
                    setMaxSeeds(
                      Math.max(5, Math.min(100, Number(e.target.value) || 5)),
                    )
                  }
                  className="w-24"
                />
                <span className="text-sm text-muted-foreground">watches</span>
              </div>
            </div>
            <div className="space-y-2 border-t pt-4">
              <Label htmlFor="recent-count">{RECENT_COUNT_LABEL}</Label>
              <p className="text-sm text-muted-foreground">
                A narrower slice of the same list. The AI web-search source
                takes the most recent few of the watches above and runs one
                search each &mdash; &ldquo;what to watch if you liked X&rdquo;.
                This is how many. Setting it higher than the number above
                changes nothing, since there is nothing further to search.
                Results are cached for two weeks and shared across people, so a
                popular title is searched once for the whole server. Fewer =
                tighter and cheaper. Nothing else uses this; any row &mdash; and
                any person on a row &mdash; can set their own.
              </p>
              <div className="flex items-center gap-2">
                <Input
                  id="recent-count"
                  type="number"
                  min={1}
                  max={25}
                  value={recentCount}
                  onChange={(e) =>
                    setRecentCount(
                      Math.max(1, Math.min(25, Number(e.target.value) || 1)),
                    )
                  }
                  className="w-24"
                />
                <span className="text-sm text-muted-foreground">watches</span>
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
                    find similar things for them. They rate it in Plex like they
                    always have &mdash; there is nothing for them to sign in to,
                    and nothing for you to do per person. A title they
                    haven&rsquo;t rated is unaffected, which is nearly all of
                    them. This one is server-wide: what someone thought of a
                    film is true on every row, so no row overrides it. Shared
                    rows ignore ratings entirely &mdash; one person&rsquo;s
                    opinion shouldn&rsquo;t reshape a row everyone sees.
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
                    A thumbs-down in Plex counts as 1 star. It stays in their
                    watch history either way &mdash; this only changes what gets
                    recommended next.
                  </p>
                  <div className="flex items-center gap-2">
                    <Input
                      id="dislike-threshold"
                      type="number"
                      min={0.5}
                      max={3}
                      step={0.5}
                      // Stored on Plex's 0..10 scale, shown as stars — the scale people actually see
                      // in Plex. Halving/doubling here rather than storing stars keeps the setting in
                      // the same units as the raw `userRating` every comparison uses.
                      value={dislikeThreshold / 2}
                      onChange={(e) =>
                        setDislikeThreshold(
                          Math.max(
                            1,
                            Math.min(6, (Number(e.target.value) || 1) * 2),
                          ),
                        )
                      }
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
                recommends from <strong>their</strong> taste. Below this there
                isn&rsquo;t enough to work from, so they get whatever you choose
                next. New people cross it on their own; nothing here needs
                revisiting.
              </p>
              <div className="flex items-center gap-2">
                <Input
                  id="min-history"
                  type="number"
                  min={1}
                  max={100}
                  value={minHistory}
                  onChange={(e) =>
                    setMinHistory(
                      Math.max(1, Math.min(100, Number(e.target.value) || 1)),
                    )
                  }
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
                The default every row inherits; any row can choose its own. A
                row named after one title (
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
                on. TMDB needs no setup. The others come from MDBList &mdash; a
                free service that returns every site&rsquo;s score in one lookup
                &mdash; so they need its API key, which you paste into the
                MDBList card in{" "}
                <Link
                  to="/settings#connections"
                  className="font-medium underline"
                >
                  Connections
                </Link>
                . Without one, those rows quietly fall back to TMDB.
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
            <div className="pt-1">
              <SaveStatus
                isPending={save.isPending}
                isError={save.isError}
                error={save.error}
                saved={save.saved}
                onRetry={save.retry}
              />
            </div>
          </CardContent>
        </Card>
      </div>
    </section>
  );
}
