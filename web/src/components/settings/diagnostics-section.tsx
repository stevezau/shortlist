import { Segmented } from "@/components/segmented";
import { Card, CardContent } from "@/components/ui/card";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

const LEVELS = ["ERROR", "WARNING", "INFO", "DEBUG", "TRACE"] as const;
type Level = (typeof LEVELS)[number];

/** Console/file log verbosity for the container. Auto-saves and applies live — no restart. */
export function DiagnosticsSection({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const level = (
    LEVELS.includes(settings["log.level"] as Level)
      ? settings["log.level"]
      : "INFO"
  ) as Level;

  return (
    <section aria-labelledby="diagnostics-heading" className="space-y-3">
      <h2 id="diagnostics-heading" className="text-lg font-semibold">
        Diagnostics
      </h2>
      <Card>
        <CardContent className="space-y-3 pt-6">
          <div>
            <p className="font-medium">Log level</p>
            <p className="text-sm text-muted-foreground">
              How much detail the container writes to its console and rotating
              log file. <strong>DEBUG</strong> shows per-source candidate
              counts, AI calls with timing and token usage, cache hits and
              rate-limit waits; <strong>TRACE</strong> adds the full AI prompts.
              Takes effect immediately — no restart needed.
            </p>
          </div>
          <Segmented<Level>
            value={level}
            ariaLabel="Log level"
            options={LEVELS.map((l) => ({ value: l, label: l }))}
            onChange={(value) => saveSettings.mutate({ "log.level": value })}
          />
        </CardContent>
      </Card>
    </section>
  );
}
