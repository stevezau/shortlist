import { useId, useState } from "react";

import { CronInput } from "@/components/cron-input";
import { Segmented } from "@/components/segmented";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cronFromTime, isPresetCron, timeFromCron } from "@/lib/cron";

type Mode = "nightly" | "weekly" | "custom" | "off";

const MODES: { value: Mode; label: string }[] = [
  { value: "nightly", label: "Nightly" },
  { value: "weekly", label: "Weekly" },
  { value: "custom", label: "Custom" },
  { value: "off", label: "Off" },
];

/**
 * When THIS row runs on its own — a nightly/weekly preset, a raw cron, or Off (never runs on a
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
    <div className="space-y-3">
      {/* "Runs on…", never "rebuilds": the titles' own cadence is "Titles refresh every…" just
          below, and one word for both read as one setting. */}
      <Label>Runs on…</Label>
      <div className="flex flex-wrap items-end gap-4">
        <Segmented
          ariaLabel="Runs on"
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
          <Label htmlFor={cronId}>Your own schedule</Label>
          <CronInput
            id={cronId}
            value={cronText}
            onChange={(next) => {
              setCronText(next);
              apply("custom", time, next);
            }}
          />
          <p className="text-sm text-muted-foreground">
            Times use the clock on the machine running Shortlist, not your own device.
          </p>
        </div>
      )}

      {mode === "off" && (
        <p className="text-sm text-muted-foreground">
          This row won&rsquo;t run on a schedule — only when you press Run
          now.
        </p>
      )}

      {(mode === "nightly" || mode === "weekly") && (
        <p className="text-sm text-muted-foreground">
          {mode === "weekly"
            ? `Runs every Sunday at ${time}, Shortlist's clock.`
            : `Runs every night at ${time}, Shortlist's clock.`}
        </p>
      )}
    </div>
  );
}
