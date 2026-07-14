import { useId, useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { cronFromTime, settingString, timeFromCron } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

const CADENCES = ["nightly", "weekly"] as const;

/** When rows refresh: a run time and a nightly/weekly cadence, saved as one cron string. */
export function ScheduleSection({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const saved = timeFromCron(
    settingString(settings, "schedule.cron", "30 3 * * *"),
  );
  const [scheduleTime, setScheduleTime] = useState(saved.time);
  const [cadence, setCadence] = useState<(typeof CADENCES)[number]>(
    saved.weekly ? "weekly" : "nightly",
  );
  const [justSaved, setJustSaved] = useState(false);
  const timeId = useId();

  const save = () => {
    setJustSaved(false);
    saveSettings.mutate(
      { "schedule.cron": cronFromTime(scheduleTime, cadence === "weekly") },
      { onSuccess: () => setJustSaved(true) },
    );
  };

  return (
    <section aria-labelledby="schedule-heading" className="space-y-3">
      <h2 id="schedule-heading" className="text-lg font-semibold">
        Schedule
      </h2>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex flex-wrap items-end gap-4">
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
            <Segmented
              legend="Cadence"
              value={cadence}
              options={CADENCES.map((option) => ({
                value: option,
                label: option,
              }))}
              onChange={setCadence}
            />
            <Button onClick={save} loading={saveSettings.isPending}>
              Save schedule
            </Button>
            <SavedIndicator show={justSaved && !saveSettings.isPending} />
            {saveSettings.isError && (
              <p role="alert" className="text-sm text-destructive">
                {apiErrorMessage(
                  saveSettings.error,
                  "Saving failed. Try again.",
                )}
              </p>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            Rows refresh {cadence} at {scheduleTime} server time.
          </p>
        </CardContent>
      </Card>
    </section>
  );
}
