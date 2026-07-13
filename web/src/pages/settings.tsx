import { useMutation } from "@tanstack/react-query";
import {
  CheckCircle2,
  PlugZap,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
  XCircle,
} from "lucide-react";
import { type ReactNode, useId, useState } from "react";

import {
  PlexGlyph,
  ProviderGlyph,
  TautulliGlyph,
  TmdbGlyph,
} from "@/components/brand-glyphs";
import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { PageHeader } from "@/components/page-header";
import { QueryBoundary } from "@/components/query-boundary";
import { UninstallDialog } from "@/components/uninstall-dialog";
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
import { cn } from "@/lib/utils";

const CADENCES = ["nightly", "weekly"] as const;
const ROW_SIZES = [10, 15, 20];

function ConnectionCard({
  service,
  title,
  purpose,
  summary,
  glyph,
}: {
  service: TestableService;
  title: string;
  /** Plain-English, non-technical explanation of what this connection is for. */
  purpose: string;
  summary: string;
  /** The service's brand mark, shown in the logo tile. */
  glyph: ReactNode;
}) {
  const test = useMutation({ mutationFn: () => api.testConnection(service) });
  const configured = Boolean(summary);

  // A status dot on the corner of the logo tile so a glance across the four cards tells you what's
  // wired up, before you test anything: green = last test passed, red = last test failed, amber =
  // configured but untested, grey = nothing set.
  const dot = test.isSuccess
    ? test.data.ok
      ? "bg-success"
      : "bg-destructive"
    : test.isError
      ? "bg-destructive"
      : configured
        ? "bg-warning"
        : "bg-muted-foreground/40";

  return (
    <Card data-testid={`connection-${service}`}>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between">
          <span className="flex items-center gap-2.5">
            <span className="relative">
              <span className="grid h-9 w-9 place-items-center rounded-lg border bg-elevated [&>svg]:h-5 [&>svg]:w-5">
                {glyph}
              </span>
              <span
                aria-hidden="true"
                className={cn(
                  "absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full ring-2 ring-card",
                  dot,
                )}
              />
            </span>
            {title}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => test.mutate()}
            loading={test.isPending}
          >
            {!test.isPending && <PlugZap aria-hidden="true" />}
            Test
          </Button>
        </CardTitle>
        <CardDescription>{purpose}</CardDescription>
      </CardHeader>
      <CardContent>
        {/* Idle: show what's on file (URL / "API key saved") or that nothing is set — the plain
            state, not a "please test" nudge. Once tested, that line becomes the pass/fail result. */}
        {test.isIdle && (
          <p className="text-sm text-muted-foreground">
            {summary || "Not set up yet."}
          </p>
        )}
        {test.isSuccess &&
          (test.data.ok ? (
            <p className="flex items-center gap-1.5 text-sm text-success">
              <CheckCircle2 className="h-4 w-4 shrink-0" aria-hidden="true" />
              {test.data.message}
            </p>
          ) : (
            <p className="flex items-center gap-1.5 text-sm text-destructive">
              <XCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
              {test.data.message}
            </p>
          ))}
        {test.isError && (
          <p className="flex items-center gap-1.5 text-sm text-destructive">
            <XCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
            {test.error instanceof ApiError
              ? test.error.message
              : "The test could not be completed."}
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
  const [curation, setCuration] = useState<CurationStyleValue>({
    tone: settingString(settings, "curator.prompt_tone", "balanced"),
    guidance: settingString(settings, "curator.prompt_guidance"),
    template: settingString(settings, "curator.prompt_template"),
  });
  const [uninstallOpen, setUninstallOpen] = useState(false);

  const timeId = useId();
  const rowNameId = useId();

  const pausedAll = settings["paused_all"] === true;

  // The read-only check (T1 filter read-back + T2 canary view) is seconds and touches nothing.
  // The full probe creates and removes a throwaway collection — the same proof the wizard runs.
  const privacyCheck = useMutation({
    mutationFn: (probe: boolean) => api.runPrivacyCheck({ probe }),
  });

  const uninstall = useMutation({ mutationFn: () => api.uninstall(false) });
  const uninstallPreview = useMutation({
    mutationFn: () => api.uninstall(true),
  });

  // "Save schedule" and "Save defaults" share one mutation, so track which section last saved to
  // show its "Saved" confirmation under the right button (and clear the other's).
  const [savedSection, setSavedSection] = useState<
    "schedule" | "defaults" | "curation" | null
  >(null);

  // PUT sends only the keys being changed; the server merges into the rest.
  const save = (
    section: "schedule" | "defaults" | "curation",
    values: Settings,
  ) => {
    setSavedSection(null);
    saveSettings.mutate(values, { onSuccess: () => setSavedSection(section) });
  };

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
            purpose="Your media server — where the personalized rows appear."
            summary={settingString(settings, "plex.url")}
            glyph={<PlexGlyph />}
          />
          <ConnectionCard
            service="tautulli"
            title="Tautulli"
            purpose="Optional. A richer source of who-watched-what history."
            summary={settingString(settings, "tautulli.url")}
            glyph={<TautulliGlyph />}
          />
          <ConnectionCard
            service="tmdb"
            title="TMDB"
            purpose="Finds similar titles to suggest. Needs a free key."
            summary={
              settingString(settings, "tmdb.apikey") ? "API key saved" : ""
            }
            glyph={<TmdbGlyph />}
          />
          <ConnectionCard
            service="llm"
            title="AI curator"
            purpose="Writes each row and its “why we picked this”. Optional — a no-AI mode works too."
            summary={settingString(settings, "curator.provider")}
            glyph={
              <ProviderGlyph
                provider={settingString(settings, "curator.provider")}
                fallback={<Sparkles aria-hidden className="text-primary" />}
              />
            }
          />
        </div>
        {/* Required by the TMDB API terms of use whenever their data is displayed. */}
        <p className="text-xs text-muted-foreground">
          This product uses the TMDB API but is not endorsed or certified by
          TMDB.
        </p>
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
                  save("schedule", {
                    "schedule.cron": cronFromTime(
                      scheduleTime,
                      cadence === "weekly",
                    ),
                  })
                }
                loading={saveSettings.isPending}
              >
                Save schedule
              </Button>
              {savedSection === "schedule" && !saveSettings.isPending && (
                <p
                  role="status"
                  className="flex items-center gap-1.5 text-sm text-success"
                >
                  <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                  Saved
                </p>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              Rows refresh {cadence} at {scheduleTime} server time.
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
            <div className="flex items-center gap-3">
              <Button
                onClick={() =>
                  save("defaults", {
                    "row.name_template": rowNameTpl,
                    "row.size": rowSize,
                  })
                }
                loading={saveSettings.isPending}
              >
                Save defaults
              </Button>
              {savedSection === "defaults" && !saveSettings.isPending && (
                <p
                  role="status"
                  className="flex items-center gap-1.5 text-sm text-success"
                >
                  <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                  Saved
                </p>
              )}
            </div>
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

      <section aria-labelledby="curation-heading" className="space-y-3">
        <h2 id="curation-heading" className="text-lg font-semibold">
          Curation style
        </h2>
        <Card>
          <CardContent className="space-y-4 pt-6">
            <p className="text-sm text-muted-foreground">
              How the AI writes everyone&rsquo;s rows. Set a tone and a few
              plain notes, or write the whole prompt yourself. You can override
              this per person on their page.
            </p>
            <CurationStyleFields value={curation} onChange={setCuration} />
            <div className="flex items-center gap-3">
              <Button
                onClick={() =>
                  save("curation", {
                    "curator.prompt_tone": curation.tone,
                    "curator.prompt_guidance": curation.guidance,
                    "curator.prompt_template": curation.template,
                  })
                }
                loading={saveSettings.isPending}
              >
                Save curation style
              </Button>
              {savedSection === "curation" && !saveSettings.isPending && (
                <p
                  role="status"
                  className="flex items-center gap-1.5 text-sm text-success"
                >
                  <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                  Saved
                </p>
              )}
            </div>
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="privacy-heading" className="space-y-3">
        <h2 id="privacy-heading" className="text-lg font-semibold">
          Privacy
        </h2>
        <Card>
          <CardContent className="space-y-4 pt-6">
            <p className="text-sm text-muted-foreground">
              Rowarr will not write to Plex unless a Privacy Check has passed in
              the last seven days. The quick check reads every user&rsquo;s
              share filters back from plex.tv and looks at a canary
              account&rsquo;s own Home. The full probe goes further: it creates
              a throwaway collection, proves it disappears for the canary, and
              removes it again.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                onClick={() => privacyCheck.mutate(false)}
                loading={privacyCheck.isPending}
              >
                <ShieldCheck aria-hidden="true" />
                Run Privacy Check
              </Button>
              <Button
                variant="outline"
                onClick={() => privacyCheck.mutate(true)}
                disabled={privacyCheck.isPending}
              >
                Run full probe (~90s)
              </Button>
            </div>
            {privacyCheck.isSuccess ? (
              <p
                className="text-sm"
                role="status"
                data-testid="privacy-check-result"
              >
                {privacyCheck.data.passed
                  ? "Passed — your server keeps rows private."
                  : "Failed — rows are NOT private on this server. Rowarr will refuse to write."}
              </p>
            ) : null}
            {privacyCheck.isError ? (
              <p className="text-sm text-destructive" role="alert">
                {privacyCheck.error instanceof ApiError
                  ? privacyCheck.error.message
                  : "The Privacy Check could not run."}
              </p>
            ) : null}
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
                  Stops scheduled and manual runs alike — no user stays enabled
                  or gets disabled, and rows stay on Plex untouched until you
                  resume.
                </p>
              </div>
              <Button
                variant="outline"
                onClick={() => saveSettings.mutate({ paused_all: !pausedAll })}
                loading={saveSettings.isPending}
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
    <div>
      <PageHeader
        icon={SettingsIcon}
        title="Settings"
        subtitle="Connections, schedule, row defaults, privacy, and uninstall."
      />

      <QueryBoundary
        query={settingsQuery}
        skeleton={<Skeleton className="h-96 w-full" />}
      >
        {(settings) => <SettingsForm settings={settings} />}
      </QueryBoundary>
    </div>
  );
}
