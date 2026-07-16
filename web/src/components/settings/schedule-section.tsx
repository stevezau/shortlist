import { useId, useState } from "react";

import { SaveStatus } from "@/components/save-status";
import { Segmented } from "@/components/segmented";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAutosave } from "@/lib/autosave";
import {
  cronFromTime,
  isPresetCron,
  settingString,
  timeFromCron,
} from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

type Mode = "nightly" | "weekly" | "custom";

const MODES: { value: Mode; label: string }[] = [
  { value: "nightly", label: "Nightly" },
  { value: "weekly", label: "Weekly" },
  { value: "custom", label: "Custom (cron)" },
];

/** Five non-empty fields — a cheap client-side gate so we don't POST an obviously incomplete cron.
 *  The server (APScheduler's CronTrigger) remains the authority on whether the fields are valid. */
function looksLikeCron(value: string): boolean {
  return value.trim().split(/\s+/).length === 5;
}

/** When rows refresh: a nightly/weekly preset (run time), or a raw cron for anything else. */
export function ScheduleSection({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const stored = settingString(settings, "schedule.cron", "30 3 * * *");
  const preset = timeFromCron(stored);
  const [mode, setMode] = useState<Mode>(
    !isPresetCron(stored) ? "custom" : preset.weekly ? "weekly" : "nightly",
  );
  const [scheduleTime, setScheduleTime] = useState(preset.time);
  const [cronText, setCronText] = useState(stored);
  const [justSaved, setJustSaved] = useState(false);
  const timeId = useId();
  const cronId = useId();

  const cron =
    mode === "custom"
      ? cronText.trim()
      : cronFromTime(scheduleTime, mode === "weekly");
  const canSave = mode !== "custom" || looksLikeCron(cron);

  const retry = useAutosave({ mode, scheduleTime, cronText }, () => {
    if (!canSave) return;
    setJustSaved(false);
    saveSettings.mutate(
      { "schedule.cron": cron },
      { onSuccess: () => setJustSaved(true) },
    );
  });

  return (
    <section aria-labelledby="schedule-heading" className="space-y-3">
      <h2 id="schedule-heading" className="text-lg font-semibold">
        Schedule
      </h2>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex flex-wrap items-end gap-4">
            <Segmented
              legend="Cadence"
              value={mode}
              options={MODES}
              onChange={setMode}
            />
            {mode !== "custom" && (
              <div className="space-y-2">
                <Label htmlFor={timeId}>Run at</Label>
                <Input
                  id={timeId}
                  type="time"
                  value={scheduleTime}
                  onChange={(event) => setScheduleTime(event.target.value)}
                  className="w-32"
                />
              </div>
            )}
            <SaveStatus
              isPending={saveSettings.isPending}
              isError={saveSettings.isError}
              error={saveSettings.error}
              saved={justSaved}
              onRetry={retry}
            />
          </div>

          {mode === "custom" ? (
            <div className="space-y-2">
              <Label htmlFor={cronId}>Cron expression</Label>
              <Input
                id={cronId}
                value={cronText}
                onChange={(event) => setCronText(event.target.value)}
                placeholder="30 3 * * *"
                spellCheck={false}
                className="max-w-xs font-mono"
                aria-describedby={`${cronId}-help`}
              />
              <p
                id={`${cronId}-help`}
                className="text-sm text-muted-foreground"
              >
                Five fields: minute, hour, day-of-month, month, day-of-week — in
                server time. For example,{" "}
                <span className="font-mono">0 */6 * * *</span> runs every 6
                hours, and <span className="font-mono">0 4 * * 1</span> runs
                Mondays at 4am.
              </p>
              {!canSave && cronText.trim() !== "" && (
                <p className="text-sm text-destructive">
                  A cron needs five space-separated fields.
                </p>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              {mode === "weekly"
                ? `Rows refresh every Sunday at ${scheduleTime} server time.`
                : `Rows refresh nightly at ${scheduleTime} server time.`}
            </p>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
