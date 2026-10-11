import { Minus, Plus } from "lucide-react";
import { useState } from "react";

import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import {
  MAX_SEASON_COUNT,
  MIN_SEASON_COUNT,
  monitorModesFor,
  SONARR_MONITOR_LABELS,
  sonarrMonitorHint,
  takesSeasonCount,
  type SonarrMonitor,
} from "@/lib/sonarr-monitor";

type Start = "firstSeason" | "lastSeason";

/**
 * "How much of a show to grab", for either request route: where to start (first or last seasons),
 * how many, or every season. Pilot episode and Add unmonitored are Sonarr's own options, so they
 * only appear when requests go straight to Sonarr.
 *
 * Shared by the global setting and the per-row override so the two cannot drift. It owns no data:
 * the caller stores `mode` and `count` and gets both back on every change.
 */
export function ShowSeasonsControl({
  mode,
  count,
  target,
  onChange,
}: {
  mode: SonarrMonitor;
  count: number;
  target: "arr" | "overseerr";
  onChange: (next: { mode: SonarrMonitor; count: number }) => void;
}) {
  // "Every season" has no start of its own, so the last one chosen is kept while it is ticked —
  // unticking it returns to where the person was, instead of always snapping to First.
  const [remembered, setRemembered] = useState<Start>(
    mode === "lastSeason" ? "lastSeason" : "firstSeason",
  );
  const every = mode === "all";
  const start: SonarrMonitor = every ? remembered : mode;
  const offered = monitorModesFor(target).filter((m) => m !== "all");
  const notSendable = !monitorModesFor(target).includes(mode);

  const pickStart = (next: SonarrMonitor) => {
    if (every) {
      setRemembered(next as Start);
      return;
    }
    onChange({ mode: next, count });
  };
  const toggleEvery = (on: boolean) => {
    if (on) {
      if (takesSeasonCount(mode)) setRemembered(mode as Start);
      onChange({ mode: "all", count });
    } else {
      onChange({ mode: remembered, count });
    }
  };
  const step = (by: number) =>
    onChange({
      mode,
      count: Math.min(MAX_SEASON_COUNT, Math.max(MIN_SEASON_COUNT, count + by)),
    });
  const stepperOn = takesSeasonCount(mode);

  return (
    <div className="space-y-3">
      <Segmented<SonarrMonitor>
        legend="Start from"
        value={start}
        onChange={pickStart}
        options={offered.map((m) => ({
          value: m,
          label: SONARR_MONITOR_LABELS[m],
        }))}
      />

      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <div
          role="group"
          aria-label="How many seasons"
          className="flex items-center gap-2"
        >
          <span className="text-sm font-medium">How many seasons</span>
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label="Fewer seasons"
            disabled={!stepperOn || count <= MIN_SEASON_COUNT}
            onClick={() => step(-1)}
          >
            <Minus aria-hidden="true" />
          </Button>
          <output
            aria-label="Number of seasons"
            className={`w-8 text-center text-sm tabular-nums ${stepperOn ? "" : "text-muted-foreground"}`}
          >
            {count}
          </output>
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label="More seasons"
            disabled={!stepperOn || count >= MAX_SEASON_COUNT}
            onClick={() => step(1)}
          >
            <Plus aria-hidden="true" />
          </Button>
        </div>

        <label className="flex cursor-pointer items-center gap-2 text-sm has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
          <input
            type="checkbox"
            checked={every}
            onChange={(e) => toggleEvery(e.target.checked)}
            className="h-4 w-4 accent-primary focus-visible:outline-none"
          />
          Every season
        </label>
      </div>

      <p className="text-sm text-muted-foreground">
        {sonarrMonitorHint(mode, count)}
      </p>

      {notSendable && (
        <p role="alert" className="text-sm text-destructive">
          “{SONARR_MONITOR_LABELS[mode]}” is saved here, but it won’t be sent:
          Overseerr only takes seasons. Pick first or last seasons to ask for a
          part of a show.
        </p>
      )}
      {target === "overseerr" ? (
        <p className="text-sm text-muted-foreground">
          Seasons that air later follow Overseerr’s own “Monitor New Seasons”
          option on its Sonarr server (All, by default).
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">
          Seasons that air later are picked up only with Every season.
        </p>
      )}
    </div>
  );
}
