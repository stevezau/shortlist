import { useRef, useState } from "react";

import { NumberPresets } from "@/components/number-presets";
import { SaveStatus } from "@/components/save-status";
import { Segmented } from "@/components/segmented";
import { Card, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { settingBool } from "@/lib/format";
import { CleanupAuditCard } from "@/components/settings/cleanup-audit-card";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

const LEVELS = ["ERROR", "WARNING", "INFO", "DEBUG", "TRACE"] as const;
type Level = (typeof LEVELS)[number];

// Preset chips for the four number knobs; each also takes a "Custom…" value within the bounds the
// API validates (run.concurrency 1–16, runs.retention and events.retention 0–24 months,
// plex.timeout_s 5–300 — settings.py VALIDATORS).
const CONCURRENCY = [1, 2, 4, 8].map((n) => ({ value: n, label: String(n) }));
const RETENTION = [0, 3, 6, 12].map((n) => ({
  value: n,
  label: n === 0 ? "Forever" : `${n}mo`,
}));
const TIMEOUTS = [20, 30, 45, 60, 90].map((n) => ({
  value: n,
  label: `${n}s`,
}));

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
  const concurrency = (settings["run.concurrency"] as number | undefined) ?? 4;
  // Fallbacks match settings_store.DEFAULTS. They are unreachable — `all_public()` always folds the
  // default in — but a wrong one is a trap either way: this was `?? 100`, a value the API's 0–24
  // bound would now reject outright.
  const retention = (settings["runs.retention"] as number | undefined) ?? 3;
  const eventRetention =
    (settings["events.retention"] as number | undefined) ?? 0;
  const timeout = (settings["plex.timeout_s"] as number | undefined) ?? 45;
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
      {/* Moved out of the Danger zone: it only READS Plex and reports what it finds, so filing it
          under a destructive heading made the safest control on the page look like the riskiest. */}
      <CleanupAuditCard />
      <Card>
        <CardContent className="space-y-3 pt-6">
          <div>
            <p className="font-medium">Console log detail</p>
            {/* This sets ONLY the console sink. `configure_logging` opens the log FILE at DEBUG
                unconditionally (logging_config.py), and the Logs page + the .zip download both read
                that file — so this control cannot quieten them, and TRACE never reaches them. Saying
                "how much detail Shortlist writes to its logs" sent people to TRACE for a bug report
                and gave them a download with no prompts in it. */}
            <p className="text-sm text-muted-foreground">
              How much detail Shortlist prints to the container&rsquo;s console
              &mdash; what you see in <code>docker logs</code>.
              <br />
              The <strong>Logs</strong> page and its download are not affected:
              the log file always records DEBUG detail, whatever this is set to.
              <br />
              <strong>DEBUG</strong> adds per-source pick counts and AI timing
              and token use. <strong>TRACE</strong> also prints the full AI
              prompts &mdash; to the console only, since the log file stops at
              DEBUG.
              <br />
              Changes apply straight away &mdash; no restart.
            </p>
          </div>
          <Segmented<Level>
            value={level}
            ariaLabel="Console log detail"
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
          <NumberPresets
            value={concurrency}
            ariaLabel="Run concurrency"
            presets={CONCURRENCY}
            min={1}
            max={16}
            unit="people at once"
            onChange={(value) => save({ "run.concurrency": value })}
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
          <NumberPresets
            value={timeout}
            ariaLabel="Plex request timeout"
            presets={TIMEOUTS}
            min={5}
            max={300}
            unit="seconds"
            onChange={(value) => save({ "plex.timeout_s": value })}
          />
          <div className="border-t pt-4">
            <p className="font-medium">Runs kept</p>
            <p className="text-sm text-muted-foreground">
              How long to keep run history (the per-run detail and traces).
              <br />
              Older runs are auto-cleared after each run. Your dashboard metrics
              are kept forever regardless — only the browsable run history is
              pruned. <strong>Forever</strong> keeps everything.
            </p>
          </div>
          <NumberPresets
            value={retention}
            ariaLabel="History retention"
            presets={RETENTION}
            min={0}
            max={24}
            unit="months (0 = forever)"
            onChange={(value) => save({ "runs.retention": value })}
          />
          <div className="border-t pt-4">
            <p className="font-medium">Change log kept</p>
            {/* Kept apart from run history on purpose (`prune_events`): this is the record of what
                changed on whose account, which an operator may want long after the run detail around
                it has gone — so it defaults to Forever while runs default to three months. */}
            <p className="text-sm text-muted-foreground">
              How long to keep the log of what Shortlist has changed &mdash;
              each write to a Plex collection or to someone&rsquo;s sharing
              settings, and each change you make to these settings.
              <br />
              Cleared out by the same nightly tidy-up as run history.{" "}
              <strong>Forever</strong> keeps every entry, and is the default:
              it&rsquo;s the only lasting record of what changed on whose
              account, so it outlives the runs it belongs to.
              <br />
              There is no screen for it yet &mdash; it&rsquo;s read through
              Shortlist&rsquo;s API, at <code>/api/events/log</code>.
            </p>
          </div>
          <NumberPresets
            value={eventRetention}
            ariaLabel="Change log retention"
            presets={RETENTION}
            min={0}
            max={24}
            unit="months (0 = forever)"
            onChange={(value) => save({ "events.retention": value })}
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
