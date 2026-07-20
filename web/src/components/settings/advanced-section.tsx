import { useRef, useState } from "react";

import { SaveStatus } from "@/components/save-status";
import { Segmented } from "@/components/segmented";
import { Card, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { settingBool } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

const LEVELS = ["ERROR", "WARNING", "INFO", "DEBUG", "TRACE"] as const;
type Level = (typeof LEVELS)[number];

const CONCURRENCY = [1, 2, 4, 8] as const;
const RETENTION = [0, 50, 100, 250] as const; // 0 = keep every run
const TIMEOUTS = [20, 30, 45, 60, 90] as const; // seconds per Plex call

/** Power-user knobs: log verbosity + run concurrency. Both auto-save and apply live — no restart. */
export function AdvancedSection({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const [saved, setSaved] = useState(false);
  const lastPayload = useRef<Settings | null>(null);
  const level = (
    LEVELS.includes(settings["log.level"] as Level)
      ? settings["log.level"]
      : "DEBUG"
  ) as Level;
  const concurrency = String(
    (settings["run.concurrency"] as number | undefined) ?? 4,
  );
  const retention = String(
    (settings["runs.retention"] as number | undefined) ?? 100,
  );
  const timeout = String(
    (settings["plex.timeout_s"] as number | undefined) ?? 45,
  );
  const hideSharedFromDisabled = settingBool(
    settings,
    "privacy.hide_shared_from_disabled",
    true,
  );

  // Every change auto-saves — but with the same Saving…/Saved/failed feedback the other sections
  // give, so a rejected save isn't silently swallowed (the control would otherwise just snap back).
  const save = (payload: Settings) => {
    lastPayload.current = payload;
    setSaved(false);
    saveSettings.mutate(payload, { onSuccess: () => setSaved(true) });
  };

  return (
    <section aria-labelledby="advanced-heading" className="space-y-3">
      <h2 id="advanced-heading" className="text-lg font-semibold">
        Advanced
      </h2>
      <Card>
        <CardContent className="space-y-3 pt-6">
          <div>
            <p className="font-medium">Log level</p>
            <p className="text-sm text-muted-foreground">
              How much detail Shortlist writes to its logs — turn this up when
              you&rsquo;re chasing a problem or filing a bug report.
              <br />
              <strong>DEBUG</strong> adds per-source pick counts and AI timing
              and token use. <strong>TRACE</strong> also logs the full AI
              prompts.
              <br />
              Changes apply straight away — no restart.
            </p>
          </div>
          <Segmented<Level>
            value={level}
            ariaLabel="Log level"
            options={LEVELS.map((l) => ({ value: l, label: l }))}
            onChange={(value) => save({ "log.level": value })}
          />
          <div className="border-t pt-4">
            <p className="font-medium">Run concurrency</p>
            <p className="text-sm text-muted-foreground">
              How many people a run works on at the same time.
              <br />
              Only the reading and AI steps overlap — changes to Plex are always
              made one at a time, in order, so this never affects privacy.
              <br />
              Higher is faster on big servers. Set to <strong>1</strong> to work
              through people one after another.
            </p>
          </div>
          <Segmented<string>
            value={concurrency}
            ariaLabel="Run concurrency"
            options={CONCURRENCY.map((n) => ({
              value: String(n),
              label: String(n),
            }))}
            onChange={(value) => save({ "run.concurrency": Number(value) })}
          />
          <div className="border-t pt-4">
            <p className="font-medium">Plex request timeout</p>
            <p className="text-sm text-muted-foreground">
              How long to wait on a single Plex request before giving up and
              retrying it.
              <br />
              Rebuilding a big library&rsquo;s row (a TV row on a large server)
              can legitimately take 15&ndash;20s — set this too low and those
              time out and retry, slowing the whole run. Raise it if you see
              &ldquo;PMS SLOW … ERR&rdquo; timeouts in the log; lower it to give
              up on a stalled server faster.
            </p>
          </div>
          <Segmented<string>
            value={timeout}
            ariaLabel="Plex request timeout"
            options={TIMEOUTS.map((n) => ({
              value: String(n),
              label: `${n}s`,
            }))}
            onChange={(value) => save({ "plex.timeout_s": Number(value) })}
          />
          <div className="border-t pt-4">
            <p className="font-medium">Runs kept</p>
            <p className="text-sm text-muted-foreground">
              How many past runs to keep in history.
              <br />
              Older runs (and the picks they recorded) are cleared automatically
              after each run. <strong>All</strong> keeps everything.
              <br />A run is never cleared while it still counts toward the
              dashboard&rsquo;s 30-day watch tracking, so a low number
              won&rsquo;t cost you any stats.
            </p>
          </div>
          <Segmented<string>
            value={retention}
            ariaLabel="Runs kept"
            options={RETENTION.map((n) => ({
              value: String(n),
              label: n === 0 ? "All" : String(n),
            }))}
            onChange={(value) => save({ "runs.retention": Number(value) })}
          />
          <div className="flex items-start justify-between gap-4 border-t pt-4">
            <div className="space-y-0.5">
              <p className="font-medium">Disabled users see nothing</p>
              <p className="text-sm text-muted-foreground">
                When you disable a user, hide every shared row from them too —
                even the public “Popular on this server” rows everyone else
                sees. Off = a disabled user still sees public shared rows like
                any other account with library access.
              </p>
            </div>
            <Switch
              checked={hideSharedFromDisabled}
              onCheckedChange={(on) =>
                save({ "privacy.hide_shared_from_disabled": on })
              }
              aria-label="Hide shared rows from disabled users"
            />
          </div>
          <div className="pt-1">
            <SaveStatus
              isPending={saveSettings.isPending}
              isError={saveSettings.isError}
              error={saveSettings.error}
              saved={saved}
              onRetry={() => lastPayload.current && save(lastPayload.current)}
            />
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
