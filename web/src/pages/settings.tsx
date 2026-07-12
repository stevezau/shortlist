import { useMutation } from "@tanstack/react-query";
import { Loader2, PlugZap } from "lucide-react";
import { useId, useState } from "react";

import { QueryBoundary } from "@/components/query-boundary";
import { UninstallDialog } from "@/components/uninstall-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import {
  cronFromTime,
  renderRowName,
  settingNumber,
  settingString,
  timeFromCron,
} from "@/lib/format";
import { useSaveSettings, useSettings } from "@/lib/queries";
import type { Settings, TestableService } from "@/lib/types";

const CADENCES = ["nightly", "weekly"] as const;
const ROW_SIZES = [10, 15, 20];

function ConnectionCard({
  service,
  title,
  summary,
}: {
  service: TestableService;
  title: string;
  summary: string;
}) {
  const test = useMutation({ mutationFn: () => api.testConnection(service) });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between">
          {title}
          <Button
            variant="outline"
            size="sm"
            onClick={() => test.mutate()}
            disabled={test.isPending}
          >
            {test.isPending ? (
              <Loader2 className="animate-spin" aria-hidden="true" />
            ) : (
              <PlugZap aria-hidden="true" />
            )}
            Test
          </Button>
        </CardTitle>
        <CardDescription>{summary || "Not configured yet."}</CardDescription>
      </CardHeader>
      <CardContent>
        {test.isSuccess &&
          (test.data.ok ? (
            <Badge variant="success">Connected — {test.data.message}</Badge>
          ) : (
            <Badge variant="destructive">{test.data.message}</Badge>
          ))}
        {test.isError && (
          <Badge variant="destructive">
            {test.error instanceof ApiError
              ? test.error.message
              : "The test could not be completed."}
          </Badge>
        )}
        {test.isIdle && (
          <p className="text-sm text-muted-foreground">
            Run a test to confirm this connection works.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function SettingsForm({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();

  const savedSchedule = timeFromCron(
    settingString(settings, "schedule.cron", "30 3 * * *"),
  );
  const [scheduleTime, setScheduleTime] = useState(savedSchedule.time);
  const [cadence, setCadence] = useState<(typeof CADENCES)[number]>(
    savedSchedule.weekly ? "weekly" : "nightly",
  );
  const [rowNameTpl, setRowNameTpl] = useState(
    settingString(settings, "row.name_template", "✨ Picked for You"),
  );
  const [rowSize, setRowSize] = useState(
    settingNumber(settings, "row.size", 15),
  );
  const [uninstallOpen, setUninstallOpen] = useState(false);

  const timeId = useId();
  const rowNameId = useId();

  const pausedAll = settings["paused_all"] === true;

  const uninstall = useMutation({ mutationFn: () => api.uninstall(false) });
  const uninstallPreview = useMutation({
    mutationFn: () => api.uninstall(true),
  });

  // PUT sends only the keys being changed; the server merges into the rest.
  const save = (values: Settings) => saveSettings.mutate(values);

  return (
    <div className="space-y-8">
      <section aria-labelledby="connections-heading" className="space-y-3">
        <h2 id="connections-heading" className="text-lg font-semibold">
          Connections
        </h2>
        <div className="grid gap-4 lg:grid-cols-2">
          <ConnectionCard
            service="plex"
            title="Plex"
            summary={settingString(settings, "plex.url")}
          />
          <ConnectionCard
            service="tautulli"
            title="Tautulli"
            summary={settingString(settings, "tautulli.url")}
          />
          <ConnectionCard
            service="tmdb"
            title="TMDB"
            summary={
              settingString(settings, "tmdb.apikey") ? "API key saved" : ""
            }
          />
          <ConnectionCard
            service="llm"
            title="Curator (LLM)"
            summary={settingString(settings, "curator.provider")}
          />
        </div>
      </section>

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
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">Cadence</legend>
                <div className="flex gap-2">
                  {CADENCES.map((option) => (
                    <Button
                      key={option}
                      type="button"
                      size="sm"
                      variant={cadence === option ? "default" : "outline"}
                      aria-pressed={cadence === option}
                      onClick={() => setCadence(option)}
                    >
                      {option}
                    </Button>
                  ))}
                </div>
              </fieldset>
              <Button
                onClick={() =>
                  save({
                    "schedule.cron": cronFromTime(
                      scheduleTime,
                      cadence === "weekly",
                    ),
                  })
                }
                disabled={saveSettings.isPending}
              >
                Save schedule
              </Button>
            </div>
            <p className="text-sm text-muted-foreground">
              Rows refresh {cadence} at {scheduleTime} server time. Cron
              expressions and per-user overrides are coming to this page.
            </p>
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="defaults-heading" className="space-y-3">
        <h2 id="defaults-heading" className="text-lg font-semibold">
          Defaults
        </h2>
        <Card>
          <CardContent className="space-y-4 pt-6">
            <div className="space-y-2">
              <Label htmlFor={rowNameId}>Row name template</Label>
              <Input
                id={rowNameId}
                value={rowNameTpl}
                onChange={(event) => setRowNameTpl(event.target.value)}
              />
              <p className="text-sm text-muted-foreground">
                Use <span className="font-mono">{"{top_seed}"}</span> for each
                user's top watched title.
              </p>
              <div className="rounded-md border bg-card p-3">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  On Plex this looks like
                </p>
                <p className="font-medium text-primary">
                  {renderRowName(rowNameTpl) || "✨ Picked for You"}
                </p>
              </div>
            </div>
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Row size</legend>
              <div className="flex gap-2">
                {ROW_SIZES.map((size) => (
                  <Button
                    key={size}
                    type="button"
                    size="sm"
                    variant={rowSize === size ? "default" : "outline"}
                    aria-pressed={rowSize === size}
                    onClick={() => setRowSize(size)}
                  >
                    {size}
                  </Button>
                ))}
              </div>
            </fieldset>
            <Button
              onClick={() =>
                save({ "row.name_template": rowNameTpl, "row.size": rowSize })
              }
              disabled={saveSettings.isPending}
            >
              Save defaults
            </Button>
            {saveSettings.isError && (
              <p role="alert" className="text-sm text-destructive">
                {saveSettings.error instanceof ApiError
                  ? saveSettings.error.message
                  : "Saving failed. Check the server log and try again."}
              </p>
            )}
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="danger-heading" className="space-y-3">
        <h2
          id="danger-heading"
          className="text-lg font-semibold text-destructive"
        >
          Danger zone
        </h2>
        <Card className="border-destructive/40">
          <CardContent className="space-y-4 pt-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="font-medium">
                  {pausedAll ? "Everything is paused" : "Pause all users"}
                </p>
                <p className="text-sm text-muted-foreground">
                  Rows stay on Plex but stop refreshing until you resume.
                </p>
              </div>
              <Button
                variant="outline"
                onClick={() => save({ paused_all: !pausedAll })}
                disabled={saveSettings.isPending}
              >
                {pausedAll ? "Resume all" : "Pause all"}
              </Button>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
              <div>
                <p className="font-medium">Full uninstall</p>
                <p className="text-sm text-muted-foreground">
                  Removes every Rowarr collection and label, and restores all
                  share filters from the original snapshots. Preview the exact
                  changes before committing.
                </p>
              </div>
              <Button
                variant="destructive"
                onClick={() => setUninstallOpen(true)}
              >
                Uninstall Rowarr…
              </Button>
            </div>
            {uninstall.isSuccess && (
              <p role="status" className="text-sm text-success">
                {uninstall.data.message ||
                  "Uninstall complete. Your server is as Rowarr found it."}
              </p>
            )}
            {uninstall.isError && (
              <p role="alert" className="text-sm text-destructive">
                {uninstall.error instanceof ApiError
                  ? uninstall.error.message
                  : "Uninstall failed. Nothing was left half-done — see the server log, then try again."}
              </p>
            )}
          </CardContent>
        </Card>
      </section>

      <UninstallDialog
        open={uninstallOpen}
        onOpenChange={(open) => {
          setUninstallOpen(open);
          if (!open) uninstallPreview.reset();
        }}
        pending={uninstall.isPending}
        onConfirm={() =>
          uninstall.mutate(undefined, {
            onSuccess: () => setUninstallOpen(false),
          })
        }
        onPreview={() => uninstallPreview.mutate()}
        previewPending={uninstallPreview.isPending}
        preview={uninstallPreview.data ?? null}
      />
    </div>
  );
}

export function SettingsPage() {
  const settingsQuery = useSettings();

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Connections, schedule, defaults, and the way out.
        </p>
      </header>

      <QueryBoundary
        query={settingsQuery}
        skeleton={<Skeleton className="h-96 w-full" />}
      >
        {(settings) => <SettingsForm settings={settings} />}
      </QueryBoundary>
    </div>
  );
}
