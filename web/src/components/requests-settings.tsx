import { useMutation } from "@tanstack/react-query";
import { Film, PlugZap, Tv } from "lucide-react";
import { type ReactNode, useId, useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Segmented } from "@/components/segmented";
import { TestResult } from "@/components/test-result";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api, apiErrorMessage } from "@/lib/api";
import { settingBool, settingNumber, settingString } from "@/lib/format";
import { useArrOptions, useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

const MAX_PER_RUN = [3, 5, 10];
const REDACTED = "•••••";

type ArrForm = {
  url: string;
  apikey: string;
  qualityProfileId: number;
  rootFolder: string;
};

/** Every editable requests setting in one object, so the panel updates it with a single patcher. */
interface RequestsForm {
  enabled: boolean;
  radarr: ArrForm;
  sonarr: ArrForm;
  ratingSource: "tmdb" | "imdb";
  omdbKey: string;
  minRating: number;
  minVotes: number;
  minDemand: number;
  minYear: number;
  maxPerRun: number;
  autoSend: boolean;
  autoMinDemand: number;
  autoMinRating: number;
  tag: string;
}

function readArr(settings: Settings, prefix: string): ArrForm {
  return {
    url: settingString(settings, `${prefix}.url`),
    // A saved key comes back redacted; keep the placeholder so leaving it untouched means "no change".
    apikey: settingString(settings, `${prefix}.apikey`),
    qualityProfileId: settingNumber(
      settings,
      `${prefix}.quality_profile_id`,
      0,
    ),
    rootFolder: settingString(settings, `${prefix}.root_folder`),
  };
}

function readForm(settings: Settings): RequestsForm {
  return {
    enabled: settingBool(settings, "requests.enabled"),
    radarr: readArr(settings, "requests.radarr"),
    sonarr: readArr(settings, "requests.sonarr"),
    ratingSource:
      settingString(settings, "requests.rating_source", "tmdb") === "imdb"
        ? "imdb"
        : "tmdb",
    omdbKey: settingString(settings, "requests.omdb.apikey"),
    minRating: settingNumber(settings, "requests.min_rating", 7),
    minVotes: settingNumber(settings, "requests.min_votes", 100),
    minDemand: settingNumber(settings, "requests.min_demand", 1),
    minYear: settingNumber(settings, "requests.min_year", 0),
    maxPerRun: settingNumber(settings, "requests.max_per_run", 5),
    autoSend: settingBool(settings, "requests.auto_send", true),
    autoMinDemand: settingNumber(settings, "requests.auto_min_demand", 3),
    autoMinRating: settingNumber(settings, "requests.auto_min_rating", 8),
    tag: settingString(settings, "requests.tag", "shortlist"),
  };
}

const selectClass =
  "h-9 w-full rounded-md border bg-elevated px-3 text-sm focus-visible:outline-none " +
  "focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60";

/** One app's connection + where-to-file settings (Radarr for movies, Sonarr for shows). */
function ArrCard({
  service,
  title,
  icon,
  form,
  onChange,
  connected,
}: {
  service: "radarr" | "sonarr";
  title: string;
  icon: ReactNode;
  form: ArrForm;
  onChange: (next: ArrForm) => void;
  /** True once this app's URL + key are SAVED, so its profiles/folders can be fetched. */
  connected: boolean;
}) {
  const test = useMutation({ mutationFn: () => api.testConnection(service) });
  const options = useArrOptions(service, connected);
  const urlId = useId();
  const keyId = useId();
  const profileId = useId();
  const folderId = useId();

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <div className="flex items-center gap-2.5">
          <span className="grid h-9 w-9 place-items-center rounded-lg border bg-elevated text-primary [&>svg]:h-5 [&>svg]:w-5">
            {icon}
          </span>
          <div>
            <p className="font-medium">{title}</p>
            <p className="text-sm text-muted-foreground">
              {service === "radarr"
                ? "Handles movie requests."
                : "Handles TV show requests."}
            </p>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor={urlId}>Address</Label>
            <Input
              id={urlId}
              placeholder="http://localhost:7878"
              value={form.url}
              onChange={(e) => onChange({ ...form, url: e.target.value })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor={keyId}>API key</Label>
            <Input
              id={keyId}
              type="password"
              placeholder="From Settings → General in the app"
              value={form.apikey}
              onChange={(e) => onChange({ ...form, apikey: e.target.value })}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            onClick={() => test.mutate()}
            loading={test.isPending}
          >
            {!test.isPending && <PlugZap aria-hidden="true" />}
            Test connection
          </Button>
          {test.isSuccess && <TestResult result={test.data} />}
          {test.isError && <TestResult error={test.error} />}
        </div>

        {/* Profiles and folders come from the app itself once it's connected — no hunting for ids. */}
        {!connected ? (
          <p className="rounded-md border border-dashed bg-muted/30 p-3 text-sm text-muted-foreground">
            Save the address and API key below, then your quality profiles and
            folders will appear here to choose from.
          </p>
        ) : options.isError ? (
          <p className="text-sm text-destructive">
            Couldn&rsquo;t load {title}&rsquo;s profiles and folders — check it
            and test again.
          </p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor={profileId}>Quality</Label>
              <select
                id={profileId}
                className={selectClass}
                disabled={options.isPending}
                value={form.qualityProfileId}
                onChange={(e) =>
                  onChange({
                    ...form,
                    qualityProfileId: Number(e.target.value),
                  })
                }
              >
                <option value={0} disabled>
                  {options.isPending ? "Loading…" : "Choose a quality profile"}
                </option>
                {options.data?.quality_profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-2">
              <Label htmlFor={folderId}>Save to</Label>
              <select
                id={folderId}
                className={selectClass}
                disabled={options.isPending}
                value={form.rootFolder}
                onChange={(e) =>
                  onChange({ ...form, rootFolder: e.target.value })
                }
              >
                <option value="" disabled>
                  {options.isPending ? "Loading…" : "Choose a folder"}
                </option>
                {options.data?.root_folders.map((f) => (
                  <option key={f.id} value={f.path}>
                    {f.path}
                  </option>
                ))}
              </select>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function RequestsSettings({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const [form, setForm] = useState<RequestsForm>(() => readForm(settings));
  const [saved, setSaved] = useState(false);
  const set = (patch: Partial<RequestsForm>) =>
    setForm((prev) => ({ ...prev, ...patch }));

  const omdbTest = useMutation({
    mutationFn: () => api.testConnection("omdb"),
  });

  const ratingId = useId();
  const votesId = useId();
  const demandId = useId();
  const yearId = useId();
  const omdbId = useId();
  const autoDemandId = useId();
  const autoRatingId = useId();
  const tagId = useId();
  const ratingLabel = form.ratingSource === "imdb" ? "IMDb" : "TMDB";

  // "Connected" for the dropdown fetch means the SAVED settings already have a URL and key on file
  // (the key comes back redacted). A just-typed-but-unsaved value doesn't count — the server reads
  // the saved config to reach the app, so profiles/folders reflect what's saved.
  const radarrConnected =
    Boolean(settingString(settings, "requests.radarr.url")) &&
    settingString(settings, "requests.radarr.apikey") === REDACTED;
  const sonarrConnected =
    Boolean(settingString(settings, "requests.sonarr.url")) &&
    settingString(settings, "requests.sonarr.apikey") === REDACTED;

  const save = () => {
    setSaved(false);
    saveSettings.mutate(
      {
        "requests.enabled": form.enabled,
        "requests.radarr.url": form.radarr.url,
        "requests.radarr.apikey": form.radarr.apikey,
        "requests.radarr.quality_profile_id": form.radarr.qualityProfileId,
        "requests.radarr.root_folder": form.radarr.rootFolder,
        "requests.sonarr.url": form.sonarr.url,
        "requests.sonarr.apikey": form.sonarr.apikey,
        "requests.sonarr.quality_profile_id": form.sonarr.qualityProfileId,
        "requests.sonarr.root_folder": form.sonarr.rootFolder,
        "requests.rating_source": form.ratingSource,
        "requests.omdb.apikey": form.omdbKey,
        "requests.min_rating": form.minRating,
        "requests.min_votes": form.minVotes,
        "requests.min_demand": form.minDemand,
        "requests.min_year": form.minYear,
        "requests.max_per_run": form.maxPerRun,
        "requests.auto_send": form.autoSend,
        "requests.auto_min_demand": form.autoMinDemand,
        "requests.auto_min_rating": form.autoMinRating,
        "requests.tag": form.tag.trim(),
      },
      { onSuccess: () => setSaved(true) },
    );
  };

  return (
    <Card>
      <CardContent className="space-y-5 pt-6">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <p className="font-medium">Fill in the gaps automatically</p>
            <p className="text-sm text-muted-foreground">
              When a great pick isn&rsquo;t in your library yet, Shortlist can
              ask Radarr or Sonarr to grab it. The strongest picks are sent
              automatically; the rest wait in your{" "}
              <strong className="font-medium text-foreground">Requests</strong>{" "}
              inbox for a yes or no. You control where that line sits below.
            </p>
          </div>
          <Switch
            checked={form.enabled}
            onCheckedChange={(enabled) => set({ enabled })}
            aria-label="Turn automatic requests on or off"
          />
        </div>

        {form.enabled && (
          <div className="space-y-5 border-t pt-5">
            <div className="grid gap-4 lg:grid-cols-2">
              <ArrCard
                service="radarr"
                title="Radarr"
                icon={<Film aria-hidden="true" />}
                form={form.radarr}
                onChange={(radarr) => set({ radarr })}
                connected={radarrConnected}
              />
              <ArrCard
                service="sonarr"
                title="Sonarr"
                icon={<Tv aria-hidden="true" />}
                form={form.sonarr}
                onChange={(sonarr) => set({ sonarr })}
                connected={sonarrConnected}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor={tagId}>Tag added items</Label>
              <Input
                id={tagId}
                value={form.tag}
                onChange={(e) => set({ tag: e.target.value })}
                placeholder="shortlist"
                className="max-w-xs"
              />
              <p className="text-sm text-muted-foreground">
                Every movie/show Shortlist requests gets this tag in
                Radarr/Sonarr (created there if it doesn&rsquo;t exist), so you
                can spot, filter, or auto-manage what it added. Leave blank for
                no tag.
              </p>
            </div>

            <fieldset className="space-y-4 rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">Guardrails</legend>

              <div className="space-y-2">
                <Segmented
                  legend="Judge titles by"
                  value={form.ratingSource}
                  options={[
                    { value: "tmdb", label: "TMDB rating" },
                    { value: "imdb", label: "IMDb rating" },
                  ]}
                  onChange={(ratingSource) => set({ ratingSource })}
                />
                <p className="text-sm text-muted-foreground">
                  {form.ratingSource === "imdb"
                    ? "Uses IMDb scores — needs a free OMDb API key below."
                    : "Uses TMDB scores. No extra setup needed."}
                </p>
                {form.ratingSource === "imdb" && (
                  <div className="space-y-2 pt-1">
                    <Label htmlFor={omdbId}>OMDb API key</Label>
                    <div className="flex flex-wrap items-center gap-2">
                      <Input
                        id={omdbId}
                        type="password"
                        placeholder="Free key from omdbapi.com"
                        value={form.omdbKey}
                        onChange={(e) => set({ omdbKey: e.target.value })}
                        className="max-w-xs"
                      />
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => omdbTest.mutate()}
                        loading={omdbTest.isPending}
                      >
                        {!omdbTest.isPending && <PlugZap aria-hidden="true" />}
                        Test
                      </Button>
                      {omdbTest.isSuccess && (
                        <TestResult result={omdbTest.data} as="span" />
                      )}
                      {omdbTest.isError && (
                        <TestResult error={omdbTest.error} as="span" />
                      )}
                    </div>
                  </div>
                )}
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor={ratingId}>Minimum {ratingLabel} rating</Label>
                  <Input
                    id={ratingId}
                    type="number"
                    min={0}
                    max={10}
                    step={0.1}
                    value={form.minRating}
                    onChange={(e) => set({ minRating: Number(e.target.value) })}
                    className="w-28"
                  />
                  <p className="text-sm text-muted-foreground">
                    Out of 10. A title must score at least this to be requested.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor={votesId}>Minimum reviews</Label>
                  <Input
                    id={votesId}
                    type="number"
                    min={0}
                    step={10}
                    value={form.minVotes}
                    onChange={(e) => set({ minVotes: Number(e.target.value) })}
                    className="w-28"
                  />
                  <p className="text-sm text-muted-foreground">
                    Keeps out obscure titles with a high {ratingLabel} score
                    from very few votes.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor={demandId}>Wanted by at least</Label>
                  <Input
                    id={demandId}
                    type="number"
                    min={1}
                    step={1}
                    value={form.minDemand}
                    onChange={(e) =>
                      set({ minDemand: Math.max(1, Number(e.target.value)) })
                    }
                    className="w-28"
                  />
                  <p className="text-sm text-muted-foreground">
                    Number of people whose picks it appears in before it&rsquo;s
                    requested. 1 = anyone.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor={yearId}>Released on or after</Label>
                  <Input
                    id={yearId}
                    type="number"
                    min={0}
                    step={1}
                    placeholder="Any year"
                    value={form.minYear || ""}
                    onChange={(e) =>
                      set({ minYear: Number(e.target.value) || 0 })
                    }
                    className="w-28"
                  />
                  <p className="text-sm text-muted-foreground">
                    Skip anything older than this year. Blank = any age.
                  </p>
                </div>
              </div>

              <div className="space-y-2">
                <Segmented
                  legend="Most to request per night"
                  value={String(form.maxPerRun)}
                  options={MAX_PER_RUN.map((n) => ({
                    value: String(n),
                    label: String(n),
                  }))}
                  onChange={(v) => set({ maxPerRun: Number(v) })}
                />
                <p className="text-sm text-muted-foreground">
                  A hard cap across both apps, so a night can never flood your
                  downloads.
                </p>
              </div>
            </fieldset>

            <fieldset className="space-y-4 rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">
                Auto-send vs. ask me
              </legend>
              <div className="flex items-start justify-between gap-4">
                <div className="space-y-1">
                  <p className="text-sm font-medium">
                    Auto-send the strongest picks
                  </p>
                  <p className="text-sm text-muted-foreground">
                    Titles clearing the higher bar below are requested for you
                    each night. Everything else that clears the bars above waits
                    in your Requests inbox. Turn this off to review every title
                    yourself.
                  </p>
                </div>
                <Switch
                  checked={form.autoSend}
                  onCheckedChange={(autoSend) => set({ autoSend })}
                  aria-label="Auto-send the strongest picks"
                />
              </div>

              {form.autoSend && (
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor={autoDemandId}>
                      Auto-send when wanted by
                    </Label>
                    <Input
                      id={autoDemandId}
                      type="number"
                      min={1}
                      step={1}
                      value={form.autoMinDemand}
                      onChange={(e) =>
                        set({
                          autoMinDemand: Math.max(1, Number(e.target.value)),
                        })
                      }
                      className="w-28"
                    />
                    <p className="text-sm text-muted-foreground">
                      At least this many people. Wanted by fewer than this? It
                      waits in the inbox.
                    </p>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor={autoRatingId}>Auto-send when rated</Label>
                    <Input
                      id={autoRatingId}
                      type="number"
                      min={0}
                      max={10}
                      step={0.1}
                      value={form.autoMinRating}
                      onChange={(e) =>
                        set({ autoMinRating: Number(e.target.value) })
                      }
                      className="w-28"
                    />
                    <p className="text-sm text-muted-foreground">
                      At least this {ratingLabel} score. Lower-rated picks wait
                      for your OK.
                    </p>
                  </div>
                </div>
              )}
            </fieldset>
          </div>
        )}

        <div className="flex items-center gap-3">
          <Button onClick={save} loading={saveSettings.isPending}>
            Save requests
          </Button>
          <SavedIndicator show={saved && !saveSettings.isPending} />
          {saveSettings.isError && (
            <p role="alert" className="text-sm text-destructive">
              {apiErrorMessage(
                saveSettings.error,
                "Saving failed. Check the server log and try again.",
              )}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
