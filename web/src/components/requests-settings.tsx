import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Film, PlugZap, Tv, XCircle } from "lucide-react";
import { type ReactNode, useId, useState } from "react";

import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api, ApiError } from "@/lib/api";
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
  const [enabled, setEnabled] = useState(
    settingBool(settings, "requests.enabled"),
  );
  const [radarr, setRadarr] = useState<ArrForm>(() =>
    readArr(settings, "requests.radarr"),
  );
  const [sonarr, setSonarr] = useState<ArrForm>(() =>
    readArr(settings, "requests.sonarr"),
  );
  const [ratingSource, setRatingSource] = useState<"tmdb" | "imdb">(
    settingString(settings, "requests.rating_source", "tmdb") === "imdb"
      ? "imdb"
      : "tmdb",
  );
  const [omdbKey, setOmdbKey] = useState(
    settingString(settings, "requests.omdb.apikey"),
  );
  const [minRating, setMinRating] = useState(
    settingNumber(settings, "requests.min_rating", 7),
  );
  const [minVotes, setMinVotes] = useState(
    settingNumber(settings, "requests.min_votes", 100),
  );
  const [minDemand, setMinDemand] = useState(
    settingNumber(settings, "requests.min_demand", 1),
  );
  const [minYear, setMinYear] = useState(
    settingNumber(settings, "requests.min_year", 0),
  );
  const [maxPerRun, setMaxPerRun] = useState(
    settingNumber(settings, "requests.max_per_run", 5),
  );
  const [saved, setSaved] = useState(false);

  const omdbTest = useMutation({
    mutationFn: () => api.testConnection("omdb"),
  });

  const ratingId = useId();
  const votesId = useId();
  const demandId = useId();
  const yearId = useId();
  const omdbId = useId();
  const ratingLabel = ratingSource === "imdb" ? "IMDb" : "TMDB";

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
        "requests.enabled": enabled,
        "requests.radarr.url": radarr.url,
        "requests.radarr.apikey": radarr.apikey,
        "requests.radarr.quality_profile_id": radarr.qualityProfileId,
        "requests.radarr.root_folder": radarr.rootFolder,
        "requests.sonarr.url": sonarr.url,
        "requests.sonarr.apikey": sonarr.apikey,
        "requests.sonarr.quality_profile_id": sonarr.qualityProfileId,
        "requests.sonarr.root_folder": sonarr.rootFolder,
        "requests.rating_source": ratingSource,
        "requests.omdb.apikey": omdbKey,
        "requests.min_rating": minRating,
        "requests.min_votes": minVotes,
        "requests.min_demand": minDemand,
        "requests.min_year": minYear,
        "requests.max_per_run": maxPerRun,
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
              When a great pick isn&rsquo;t in your library yet, Rowarr can ask
              Radarr or Sonarr to grab it. It stays cautious on purpose: only a
              handful per night, and only titles that are both highly rated and
              widely reviewed.
            </p>
          </div>
          <Switch
            checked={enabled}
            onCheckedChange={setEnabled}
            aria-label="Turn automatic requests on or off"
          />
        </div>

        {enabled && (
          <div className="space-y-5 border-t pt-5">
            <div className="grid gap-4 lg:grid-cols-2">
              <ArrCard
                service="radarr"
                title="Radarr"
                icon={<Film aria-hidden="true" />}
                form={radarr}
                onChange={setRadarr}
                connected={radarrConnected}
              />
              <ArrCard
                service="sonarr"
                title="Sonarr"
                icon={<Tv aria-hidden="true" />}
                form={sonarr}
                onChange={setSonarr}
                connected={sonarrConnected}
              />
            </div>

            <fieldset className="space-y-4 rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">Guardrails</legend>

              <div className="space-y-2">
                <Segmented
                  legend="Judge titles by"
                  value={ratingSource}
                  options={[
                    { value: "tmdb", label: "TMDB rating" },
                    { value: "imdb", label: "IMDb rating" },
                  ]}
                  onChange={(v) => setRatingSource(v as "tmdb" | "imdb")}
                />
                <p className="text-sm text-muted-foreground">
                  {ratingSource === "imdb"
                    ? "Uses IMDb scores — needs a free OMDb API key below."
                    : "Uses TMDB scores. No extra setup needed."}
                </p>
                {ratingSource === "imdb" && (
                  <div className="space-y-2 pt-1">
                    <Label htmlFor={omdbId}>OMDb API key</Label>
                    <div className="flex flex-wrap items-center gap-2">
                      <Input
                        id={omdbId}
                        type="password"
                        placeholder="Free key from omdbapi.com"
                        value={omdbKey}
                        onChange={(e) => setOmdbKey(e.target.value)}
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
                      {omdbTest.isSuccess &&
                        (omdbTest.data.ok ? (
                          <span className="flex items-center gap-1.5 text-sm text-success">
                            <CheckCircle2
                              className="h-4 w-4 shrink-0"
                              aria-hidden="true"
                            />
                            {omdbTest.data.message}
                          </span>
                        ) : (
                          <span className="flex items-center gap-1.5 text-sm text-destructive">
                            <XCircle
                              className="h-4 w-4 shrink-0"
                              aria-hidden="true"
                            />
                            {omdbTest.data.message}
                          </span>
                        ))}
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
                    value={minRating}
                    onChange={(e) => setMinRating(Number(e.target.value))}
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
                    value={minVotes}
                    onChange={(e) => setMinVotes(Number(e.target.value))}
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
                    value={minDemand}
                    onChange={(e) =>
                      setMinDemand(Math.max(1, Number(e.target.value)))
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
                    value={minYear || ""}
                    onChange={(e) => setMinYear(Number(e.target.value) || 0)}
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
                  value={String(maxPerRun)}
                  options={MAX_PER_RUN.map((n) => ({
                    value: String(n),
                    label: String(n),
                  }))}
                  onChange={(v) => setMaxPerRun(Number(v))}
                />
                <p className="text-sm text-muted-foreground">
                  A hard cap across both apps, so a night can never flood your
                  downloads.
                </p>
              </div>
            </fieldset>
          </div>
        )}

        <div className="flex items-center gap-3">
          <Button onClick={save} loading={saveSettings.isPending}>
            Save requests
          </Button>
          {saved && !saveSettings.isPending && (
            <p
              role="status"
              className="flex items-center gap-1.5 text-sm text-success"
            >
              <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
              Saved
            </p>
          )}
          {saveSettings.isError && (
            <p role="alert" className="text-sm text-destructive">
              {saveSettings.error instanceof ApiError
                ? saveSettings.error.message
                : "Saving failed. Check the server log and try again."}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
