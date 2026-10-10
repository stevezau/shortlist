import { useRef, useState } from "react";
import { Link } from "react-router";

import { NumberPresets } from "@/components/number-presets";
import { SaveStatus } from "@/components/save-status";
import { Segmented } from "@/components/segmented";
import { CleanupAuditCard } from "@/components/settings/cleanup-audit-card";
import { useSaveBarReport } from "@/components/settings/save-bar-context";
import { PauseAllRow } from "@/components/settings/pause-all-row";
import { SettingBlock, SettingsPanel, SettingsSection } from "@/components/settings/section-layout";
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

/**
 * System knobs: history kept, console log detail, run concurrency and the Plex timeout. Each change
 * saves on its own and applies live — no restart.
 *
 * "Disabled users see nothing" is not here: it decides who can see which rows, so it lives on the
 * Privacy page.
 */
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

  // Every change auto-saves — but with the same Saving…/Saved/failed feedback the other sections
  // give, so a rejected save isn't silently swallowed (the control would otherwise just snap back).
  const save = (payload: Settings) => {
    lastPayload.current = payload;
    setSaved(false);
    saveSettings.mutate(payload, { onSuccess: () => setSaved(true) });
  };

  const inSaveBar = useSaveBarReport("advanced", {
    isPending: saveSettings.isPending,
    isError: saveSettings.isError,
    error: saveSettings.error,
    saved,
    retry: () => lastPayload.current && save(lastPayload.current),
  });

  return (
    <>
      <SettingsSection
        id="advanced"
        title="History & logs"
        description="What Shortlist keeps, and how much it says while it works. Every change here applies straight away."
      >
        {!inSaveBar && (
          <SaveStatus
            isPending={saveSettings.isPending}
            isError={saveSettings.isError}
            error={saveSettings.error}
            saved={saved}
            onRetry={() => lastPayload.current && save(lastPayload.current)}
          />
        )}
        <SettingsPanel>
          <SettingBlock
            title="Runs kept"
            description={
              <>
                How long to keep run history (the per-run detail and traces).
                Older runs are auto-cleared after each run. Your dashboard
                metrics are kept forever regardless — only the browsable run
                history is pruned. <strong className="text-foreground">Forever</strong> keeps everything.
              </>
            }
          >
            <div id="runs-retention" className="scroll-mt-32 md:scroll-mt-8">
              <NumberPresets
                value={retention}
                ariaLabel="History retention"
                presets={RETENTION}
                min={0}
                max={24}
                unit="months (0 = forever)"
                onChange={(value) => save({ "runs.retention": value })}
              />
            </div>
          </SettingBlock>
          {/* Kept apart from run history on purpose (`prune_events`): this is the record of what
              changed on whose account, which an operator may want long after the run detail around
              it has gone — so it defaults to Forever while runs default to three months.

              No raw API path in the sentence: it used to end "Read it at /api/events/log", which
              handed the owner of the ONE lasting audit trail a URL to curl. It has a screen now. */}
          <SettingBlock
            title="Change log kept"
            description={
              <>
                How long to keep the record of every write to Plex and every
                settings change. <strong className="text-foreground">Forever</strong> by default &mdash; it is
                the only lasting account of what changed on whose account, and
                it&rsquo;s what a support question gets answered from. Read it
                under{" "}
                <Link to="/activity?tab=changes" className="font-medium text-accent-foreground underline underline-offset-2 hover:text-foreground">
                  Activity → Changes on Plex
                </Link>
                ; it also comes with every backup.
              </>
            }
          >
            <div id="events-retention" className="scroll-mt-32 md:scroll-mt-8">
              <NumberPresets
                value={eventRetention}
                ariaLabel="Change log retention"
                presets={RETENTION}
                min={0}
                max={24}
                unit="months (0 = forever)"
                onChange={(value) => save({ "events.retention": value })}
              />
            </div>
          </SettingBlock>
          {/* This sets ONLY the console sink. `configure_logging` opens the log FILE at DEBUG
              unconditionally (logging_config.py), and the Log tab + the .zip download both read
              that file — so this control cannot quieten them, and TRACE never reaches them. Saying
              "how much detail Shortlist writes to its logs" sent people to TRACE for a bug report
              and gave them a download with no prompts in it. */}
          <SettingBlock
            title="Console log detail"
            description={
              <>
                How much detail reaches <code className="font-mono">docker logs</code>. Never affects the{" "}
                <strong className="text-foreground">Log</strong> tab on Activity or its download &mdash;
                the file always records DEBUG. <strong className="text-foreground">TRACE</strong> adds the
                full AI prompts, console only.
              </>
            }
          >
            <div id="log-level" className="scroll-mt-32 md:scroll-mt-8">
              <Segmented<Level>
                value={level}
                ariaLabel="Console log detail"
                options={LEVELS.map((l) => ({ value: l, label: l }))}
                onChange={(value) => save({ "log.level": value })}
              />
            </div>
          </SettingBlock>
        </SettingsPanel>
      </SettingsSection>

      <SettingsSection id="run-speed" title="Run speed" description="How hard a run works your Plex server.">
        <SettingsPanel>
          <SettingBlock
            title="Run concurrency"
            description="How many people a run reads and curates at once — higher is faster on a big server. Writes to Plex stay one at a time whatever this says, so it never affects privacy."
          >
            <div id="run-concurrency" className="scroll-mt-32 md:scroll-mt-8">
              <NumberPresets
                value={concurrency}
                ariaLabel="Run concurrency"
                presets={CONCURRENCY}
                min={1}
                max={16}
                unit="people at once"
                onChange={(value) => save({ "run.concurrency": value })}
              />
            </div>
          </SettingBlock>
          <SettingBlock
            title="Plex request timeout"
            description={
              <>
                How long to wait on one Plex request before retrying it. A TV
                row on a big server can legitimately take 15&ndash;20s, so too
                low here makes runs slower, not faster. Raise it if the log
                shows &ldquo;PMS SLOW … ERR&rdquo;.
              </>
            }
          >
            <div id="plex-timeout" className="scroll-mt-32 md:scroll-mt-8">
              <NumberPresets
                value={timeout}
                ariaLabel="Plex request timeout"
                presets={TIMEOUTS}
                min={5}
                max={300}
                unit="seconds"
                onChange={(value) => save({ "plex.timeout_s": value })}
              />
            </div>
          </SettingBlock>
          <PauseAllRow settings={settings} />
        </SettingsPanel>
      </SettingsSection>

      {/* Not under the Danger zone: it only READS Plex and reports what it finds, so filing it
          under a destructive heading made the safest control on the page look like the riskiest. */}
      <SettingsSection id="plex-audit" title="On your Plex" description="Check what Shortlist has created on your server.">
        <CleanupAuditCard />
      </SettingsSection>
    </>
  );
}
