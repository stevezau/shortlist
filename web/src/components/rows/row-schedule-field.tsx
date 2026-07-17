import { useId, useState } from "react";

import { Segmented } from "@/components/segmented";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cronFromTime, isPresetCron, timeFromCron } from "@/lib/format";

type Mode = "nightly" | "weekly" | "custom" | "off";

const MODES: { value: Mode; label: string }[] = [
  { value: "nightly", label: "Nightly" },
  { value: "weekly", label: "Weekly" },
  { value: "custom", label: "Custom (cron)" },
  { value: "off", label: "Off" },
];

/** Five non-empty fields — a cheap client gate so we don't save an obviously incomplete cron; the
 *  server (APScheduler) stays the authority on validity. */
function looksLikeCron(value: string): boolean {
  return value.trim().split(/\s+/).length === 5;
}

/**
 * When THIS row rebuilds on its own — a nightly/weekly preset, a raw cron, or Off (never runs on a
 * schedule). Controlled: emits the cron string, or "" for Off. There is no global schedule; every
 * row carries its own.
 */
export function RowScheduleField({
  value,
  onChange,
}: {
  value: string;
  onChange: (cron: string) => void;
}) {
  const trimmed = value.trim();
  const preset = timeFromCron(trimmed || "30 3 * * *");
  const [mode, setMode] = useState<Mode>(
    !trimmed
      ? "off"
      : !isPresetCron(trimmed)
        ? "custom"
        : preset.weekly
          ? "weekly"
          : "nightly",
  );
  const [time, setTime] = useState(preset.time);
  const [cronText, setCronText] = useState(trimmed || "30 3 * * *");
  const timeId = useId();
  const cronId = useId();

  const apply = (nextMode: Mode, nextTime: string, nextCron: string) => {
    setMode(nextMode);
    if (nextMode === "off") onChange("");
    else if (nextMode === "custom") onChange(nextCron.trim());
    else onChange(cronFromTime(nextTime, nextMode === "weekly"));
  };

  return (
    <div className="space-y-3 border-t pt-4">
      <Label>Schedule</Label>
      <p className="text-sm text-muted-foreground">
        When this row rebuilds on its own. Every row runs on its own schedule —
        set it <strong>Off</strong> to only run it by hand.
      </p>
      <div className="flex flex-wrap items-end gap-4">
        <Segmented
          legend="Cadence"
          value={mode}
          options={MODES}
          onChange={(next) => apply(next, time, cronText)}
        />
        {(mode === "nightly" || mode === "weekly") && (
          <div className="space-y-2">
            <Label htmlFor={timeId}>Run at</Label>
            <Input
              id={timeId}
              type="time"
              value={time}
              onChange={(event) => {
                setTime(event.target.value);
                apply(mode, event.target.value, cronText);
              }}
              className="w-32"
            />
          </div>
        )}
      </div>

      {mode === "custom" && (
        <div className="space-y-2">
          <Label htmlFor={cronId}>Cron expression</Label>
          <Input
            id={cronId}
            value={cronText}
            onChange={(event) => {
              setCronText(event.target.value);
              apply("custom", time, event.target.value);
            }}
            placeholder="30 3 * * *"
            spellCheck={false}
            className="max-w-xs font-mono"
          />
          <p className="text-sm text-muted-foreground">
            Five fields: minute, hour, day-of-month, month, day-of-week — in
            server time. For example{" "}
            <span className="font-mono">0 */6 * * *</span> runs every 6 hours.
          </p>
          {!looksLikeCron(cronText) && cronText.trim() !== "" && (
            <p className="text-sm text-destructive">
              A cron needs five space-separated fields.
            </p>
          )}
        </div>
      )}

      {mode === "off" && (
        <p className="text-sm text-muted-foreground">
          This row won&rsquo;t run on a schedule — only when you trigger a run
          yourself.
        </p>
      )}

      {(mode === "nightly" || mode === "weekly") && (
        <p className="text-sm text-muted-foreground">
          {mode === "weekly"
            ? `Rebuilds every Sunday at ${time} server time.`
            : `Rebuilds nightly at ${time} server time.`}
        </p>
      )}
    </div>
  );
}
