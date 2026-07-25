import { useState } from "react";
import { Link } from "react-router-dom";

import { SaveStatus } from "@/components/save-status";
import { AiWebSearchCard } from "@/components/settings/ai-web-search-card";
import { FreshnessSlider } from "@/components/settings/freshness-slider";
import { InlineKeyField } from "@/components/settings/inline-key-field";
import { WatchedSlider } from "@/components/settings/watched-slider";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useAutosavedSettings } from "@/lib/autosave";
import { FRESHNESS_DEFAULT, WATCHED_PCT_DEFAULT } from "@/lib/constants";
import { hasTrakt, SOURCES, webSearchProvider } from "@/lib/sources";
import type { Settings } from "@/lib/types";

// Every source except AI web search — that one gets its own card (backend choice + inline key).
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
  const [freshness, setFreshness] = useState<number>(() =>
    readPercent(settings, "recommendations.freshness", FRESHNESS_DEFAULT),
  );
  const [recentCount, setRecentCount] = useState<number>(() => {
    const value = Number(settings["recommendations.recent_count"]);
    return Number.isFinite(value) ? Math.min(25, Math.max(1, value)) : 10;
  });
  const [searchBackend, setSearchBackend] = useState<string>(() =>
    webSearchProvider(settings),
  );

  const toggle = (id: string) =>
    setEnabled((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id],
    );

  // Persist the owner's INTENT (the enabled set as chosen). A source whose dependency isn't met yet
  // no-ops safely in the engine and shows an inline "here's what's needed" prompt — never a silent lie.
  const save = useAutosavedSettings(
    { enabled, watchedPct, freshness, recentCount, searchBackend },
    () => ({
      "candidates.sources": enabled,
      "recommendations.watched_pct": watchedPct / 100,
      "recommendations.freshness": freshness / 100,
      "recommendations.recent_count": recentCount,
      "llm_web.search_provider": searchBackend,
    }),
  );

  return (
    <section aria-labelledby="recs-heading" className="space-y-3">
      <h2 id="recs-heading" className="text-lg font-semibold">
        Finding titles
      </h2>
      <p className="text-sm text-muted-foreground">
        Where Shortlist looks for titles to suggest, and how AI enhances the
        search. This is the <strong>default every row inherits</strong> — any
        row can override in its editor.
      </p>

      <h3 className="pt-2 text-base font-semibold">Title sources</h3>
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
                  <p className="text-sm text-muted-foreground">{source.desc}</p>
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

      <h3 className="pt-4 text-base font-semibold">AI enhancement</h3>
      <div className="space-y-1.5 rounded-md border bg-muted/40 p-4 text-sm text-muted-foreground">
        <p className="font-medium text-foreground">How AI is used</p>
        <p>
          The <strong>TMDB</strong> sources above use no AI — just the free TMDB
          key — and find most titles.
        </p>
        <p>
          <strong>AI web search</strong> below is optional but proven valuable:
          it searches the web for acclaimed titles TMDB misses, using your AI
          provider.
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
        backend={searchBackend}
        onBackendChange={setSearchBackend}
      />

      <h3 className="pt-4 text-base font-semibold">Row behavior</h3>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="space-y-2">
            <Label htmlFor="watched-pct">Already-watched titles</Label>
            <p className="text-sm text-muted-foreground">
              How much of a row may be things a person has already finished. The
              default every row inherits; any row can choose its own.
            </p>
            <WatchedSlider
              id="watched-pct"
              value={watchedPct}
              onChange={setWatchedPct}
            />
          </div>
          <div className="space-y-2 border-t pt-4">
            <Label htmlFor="freshness">Freshness</Label>
            <p className="text-sm text-muted-foreground">
              How often a row refreshes — not a nightly reshuffle. Most nights a
              row stays exactly as it is (nothing rewritten to Plex); on its
              refresh night the strongest picks stay and the weakest are swapped
              for new ones. Lower = stickier and cheaper; higher = fresher. The
              default every row inherits; any row can choose its own.
            </p>
            <FreshnessSlider
              id="freshness"
              value={freshness}
              onChange={setFreshness}
            />
          </div>
          <div className="space-y-2 border-t pt-4">
            <Label htmlFor="recent-count">Recent watches to search</Label>
            <p className="text-sm text-muted-foreground">
              How many of a person’s most recent watches the AI web-search
              source looks up — one search each, “what to watch if you liked X.”
              Results are cached for two weeks and shared across people, so a
              popular title is searched once for the whole server. Fewer =
              tighter and cheaper. Only affects the AI web-search source; any
              row — and any person on a row — can set their own.
            </p>
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
              className="w-28"
            />
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
    </section>
  );
}
