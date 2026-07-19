import { Film, Tv } from "lucide-react";
import { type ReactNode, useId, useState } from "react";

import { SaveStatus } from "@/components/save-status";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { REDACTED } from "@/components/ui/secret-input";
import { Switch } from "@/components/ui/switch";
import { useAutosavedSettings } from "@/lib/autosave";
import { settingBool, settingNumber, settingString } from "@/lib/format";
import { useArrOptions } from "@/lib/queries";
import { hasMdblist } from "@/lib/sources";
import type { Settings } from "@/lib/types";

const MAX_PER_RUN = [3, 5, 10];

// Which score gates a title. TMDB needs no setup; the rest come from MDBList (one call, cached).
type RatingSource = "tmdb" | "imdb" | "tomatoes" | "metacritic" | "trakt";
const RATING_SOURCES: RatingSource[] = [
  "tmdb",
  "imdb",
  "tomatoes",
  "metacritic",
  "trakt",
];
const RATING_LABELS: Record<RatingSource, string> = {
  tmdb: "TMDB",
  imdb: "IMDb",
  tomatoes: "Rotten Tomatoes",
  metacritic: "Metacritic",
  trakt: "Trakt",
};

type ArrForm = {
  qualityProfileId: number;
  rootFolder: string;
};

/** Every editable requests setting in one object, so the panel updates it with a single patcher. */
interface RequestsForm {
  enabled: boolean;
  radarr: ArrForm;
  sonarr: ArrForm;
  ratingSource: RatingSource;
  minRating: number;
  minVotes: number;
  minDemand: number;
  minYear: number;
  maxYear: number;
  maxPerRun: number;
  autoSend: boolean;
  autoMinDemand: number;
  autoMinRating: number;
  tag: string;
  autoUserTag: boolean;
}

function readArr(settings: Settings, prefix: string): ArrForm {
  // The connection (address + key) lives in Settings → Connections now; this form only owns the
  // request-filing choices (quality profile + folder) for each app.
  return {
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
    ratingSource: RATING_SOURCES.includes(
      settingString(settings, "requests.rating_source", "tmdb") as RatingSource,
    )
      ? (settingString(
          settings,
          "requests.rating_source",
          "tmdb",
        ) as RatingSource)
      : "tmdb",
    minRating: settingNumber(settings, "requests.min_rating", 7),
    minVotes: settingNumber(settings, "requests.min_votes", 100),
    minDemand: settingNumber(settings, "requests.min_demand", 1),
    minYear: settingNumber(settings, "requests.min_year", 0),
    maxYear: settingNumber(settings, "requests.max_year", 0),
    maxPerRun: settingNumber(settings, "requests.max_per_run", 5),
    autoSend: settingBool(settings, "requests.auto_send", true),
    autoMinDemand: settingNumber(settings, "requests.auto_min_demand", 3),
    autoMinRating: settingNumber(settings, "requests.auto_min_rating", 8),
    tag: settingString(settings, "requests.tag", "shortlist"),
    autoUserTag: settingBool(settings, "requests.auto_user_tag"),
  };
}

const selectClass =
  "h-9 w-full rounded-md border bg-elevated px-3 text-sm focus-visible:outline-none " +
  "focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60";

/** One app's where-to-file choices for requests (Radarr for movies, Sonarr for shows). The
 *  connection itself — address + API key — lives in Settings → Connections; this only picks the
 *  quality profile and folder, and only once that app is connected. */
function ArrCard({
  service,
  title,
  icon,
  form,
  onChange,
  connected,
  onGoToConnections,
}: {
  service: "radarr" | "sonarr";
  title: string;
  icon: ReactNode;
  form: ArrForm;
  onChange: (next: ArrForm) => void;
  /** True once this app's URL + key are SAVED (in Connections), so its profiles/folders can load. */
  connected: boolean;
  onGoToConnections: () => void;
}) {
  const options = useArrOptions(service, connected);
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

        {/* Profiles and folders come from the app itself once it's connected — no hunting for ids. */}
        {!connected ? (
          <div className="space-y-2 rounded-md border border-dashed bg-muted/30 p-3">
            <p className="text-sm text-muted-foreground">
              {title} isn&rsquo;t connected yet. Add its address and API key in{" "}
              <strong className="font-medium text-foreground">
                Connections
              </strong>
              , then pick its quality profile and folder here.
            </p>
            <Button variant="outline" size="sm" onClick={onGoToConnections}>
              Go to Connections
            </Button>
          </div>
        ) : options.isError ? (
          <p className="text-sm text-destructive">
            Couldn&rsquo;t load {title}&rsquo;s profiles and folders — check its
            connection in the Connections section and test it again.
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
  const [form, setForm] = useState<RequestsForm>(() => readForm(settings));
  const set = (patch: Partial<RequestsForm>) =>
    setForm((prev) => ({ ...prev, ...patch }));

  // The MDBList key now lives in Settings → Connections (like TMDB/Trakt). Here we only need to know
  // whether it's set up, so a non-TMDB rating source can warn when it isn't.
  const mdblistConnected = hasMdblist(settings);

  const ratingId = useId();
  const votesId = useId();
  const demandId = useId();
  const yearId = useId();
  const yearMaxId = useId();
  const autoDemandId = useId();
  const autoRatingId = useId();
  const tagId = useId();
  const autoUserTagId = useId();
  const ratingLabel = RATING_LABELS[form.ratingSource];

  // "Connected" for the dropdown fetch means the SAVED settings already have a URL and key on file
  // (the key comes back redacted). A just-typed-but-unsaved value doesn't count — the server reads
  // the saved config to reach the app, so profiles/folders reflect what's saved.
  const radarrConnected =
    Boolean(settingString(settings, "requests.radarr.url")) &&
    settingString(settings, "requests.radarr.apikey") === REDACTED;
  const sonarrConnected =
    Boolean(settingString(settings, "requests.sonarr.url")) &&
    settingString(settings, "requests.sonarr.apikey") === REDACTED;

  const goToConnections = () =>
    document
      .getElementById("connections")
      ?.scrollIntoView({ behavior: "smooth" });

  // Auto-save: no Save button. Any change persists shortly after you stop (so text fields never
  // save mid-keystroke; toggles feel instant).
  const save = useAutosavedSettings(form, () => {
    const values: Settings = {
      "requests.enabled": form.enabled,
      // Address + API key are owned by Settings → Connections now; this form only saves the
      // request-filing choices (quality profile + folder) and the policy below.
      "requests.radarr.quality_profile_id": form.radarr.qualityProfileId,
      "requests.radarr.root_folder": form.radarr.rootFolder,
      "requests.sonarr.quality_profile_id": form.sonarr.qualityProfileId,
      "requests.sonarr.root_folder": form.sonarr.rootFolder,
      "requests.rating_source": form.ratingSource,
      "requests.min_rating": form.minRating,
      "requests.min_votes": form.minVotes,
      "requests.min_demand": form.minDemand,
      "requests.min_year": form.minYear,
      "requests.max_year": form.maxYear,
      "requests.max_per_run": form.maxPerRun,
      "requests.auto_send": form.autoSend,
      "requests.auto_min_demand": form.autoMinDemand,
      "requests.auto_min_rating": form.autoMinRating,
      "requests.tag": form.tag.trim(),
      "requests.auto_user_tag": form.autoUserTag,
    };
    return values;
  });

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

        {!form.enabled && (
          <p className="text-sm text-muted-foreground">
            Off — turn this on to set up automatic requests and choose the
            rules.
          </p>
        )}

        {form.enabled && (
          <div className="space-y-5 border-t pt-5">
            {!radarrConnected && !sonarrConnected && (
              <div className="space-y-2 rounded-lg border border-primary/40 bg-primary/5 p-4">
                <p className="text-sm font-medium">
                  Connect Radarr or Sonarr to start requesting
                </p>
                <p className="text-sm text-muted-foreground">
                  Requests need at least one of them connected. Add its address
                  and API key in the Connections section, then come back here to
                  set the rules.
                </p>
                <Button variant="outline" size="sm" onClick={goToConnections}>
                  Go to Connections
                </Button>
              </div>
            )}
            <div className="grid gap-4 lg:grid-cols-2">
              <ArrCard
                service="radarr"
                title="Radarr"
                icon={<Film aria-hidden="true" />}
                form={form.radarr}
                onChange={(radarr) => set({ radarr })}
                connected={radarrConnected}
                onGoToConnections={goToConnections}
              />
              <ArrCard
                service="sonarr"
                title="Sonarr"
                icon={<Tv aria-hidden="true" />}
                form={form.sonarr}
                onChange={(sonarr) => set({ sonarr })}
                connected={sonarrConnected}
                onGoToConnections={goToConnections}
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

            <div className="flex items-start justify-between gap-4">
              <div className="space-y-1">
                <Label htmlFor={autoUserTagId}>Also tag by person</Label>
                <p className="text-sm text-muted-foreground">
                  Adds each requester&rsquo;s username as a tag too, so you can
                  tell in Radarr/Sonarr who a title was requested for — without
                  setting a tag on every user by hand. A user with their own tag
                  keeps it.
                </p>
              </div>
              <Switch
                id={autoUserTagId}
                checked={form.autoUserTag}
                onCheckedChange={(on) => set({ autoUserTag: on })}
                aria-label="Also tag requests with each person's username"
              />
            </div>

            <fieldset className="space-y-4 rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">Guardrails</legend>

              <div className="space-y-2">
                <Segmented
                  legend="Judge titles by"
                  value={form.ratingSource}
                  options={[
                    { value: "tmdb", label: "TMDB" },
                    { value: "imdb", label: "IMDb" },
                    { value: "tomatoes", label: "Rotten Tomatoes" },
                    { value: "metacritic", label: "Metacritic" },
                    { value: "trakt", label: "Trakt" },
                  ]}
                  onChange={(ratingSource) => set({ ratingSource })}
                />
                <p className="text-sm text-muted-foreground">
                  {form.ratingSource === "tmdb"
                    ? "Uses TMDB scores. No extra setup needed."
                    : `Uses ${RATING_LABELS[form.ratingSource]} scores from MDBList (one lookup returns every score, cached for a week). Shown on a 0–10 scale.`}
                </p>
                {form.ratingSource !== "tmdb" &&
                  (mdblistConnected ? (
                    <p className="text-sm text-muted-foreground">
                      Using your MDBList connection. Manage or test the key in{" "}
                      <button
                        type="button"
                        onClick={goToConnections}
                        className="font-medium text-primary underline underline-offset-2"
                      >
                        Connections
                      </button>
                      .
                    </p>
                  ) : (
                    <div
                      role="alert"
                      className="space-y-2 rounded-lg border border-warning/40 bg-warning/5 p-4"
                    >
                      <p className="text-sm font-medium">
                        MDBList isn’t connected
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {RATING_LABELS[form.ratingSource]} scores come from
                        MDBList. Add its free API key in Connections, or
                        Shortlist falls back to TMDB scores and this choice
                        won’t take effect.
                      </p>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={goToConnections}
                      >
                        Set up MDBList in Connections
                      </Button>
                    </div>
                  ))}
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
                    Skip anything older than this year. Blank = no lower limit.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor={yearMaxId}>Released on or before</Label>
                  <Input
                    id={yearMaxId}
                    type="number"
                    min={0}
                    step={1}
                    placeholder="Any year"
                    value={form.maxYear || ""}
                    onChange={(e) =>
                      set({ maxYear: Number(e.target.value) || 0 })
                    }
                    className="w-28"
                  />
                  <p className="text-sm text-muted-foreground">
                    Skip anything newer than this year. Blank = no upper limit.
                    A show is judged by its first-air year.
                  </p>
                  {form.minYear > 0 &&
                    form.maxYear > 0 &&
                    form.maxYear < form.minYear && (
                      <p className="text-sm text-destructive">
                        The latest year is before the earliest — no titles can
                        match this range.
                      </p>
                    )}
                </div>
              </div>

              <div className="space-y-2">
                <Segmented
                  legend="Most to auto-request per night"
                  value={String(form.maxPerRun)}
                  options={MAX_PER_RUN.map((n) => ({
                    value: String(n),
                    label: String(n),
                  }))}
                  onChange={(v) => set({ maxPerRun: Number(v) })}
                />
                <p className="text-sm text-muted-foreground">
                  A hard cap on titles sent automatically each night, across
                  both apps — so a night can never flood your downloads. Titles
                  you approve by hand from the Requests inbox aren&rsquo;t
                  capped.
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
                  {(form.autoMinDemand < form.minDemand ||
                    form.autoMinRating < form.minRating) && (
                    <p
                      role="alert"
                      className="text-sm text-warning sm:col-span-2"
                    >
                      The auto-send bar is below the minimums above, so
                      effectively everything that qualifies auto-sends. Raise it
                      above the Guardrails to keep a manual queue.
                    </p>
                  )}
                </div>
              )}
            </fieldset>
          </div>
        )}

        <SaveStatus
          isPending={save.isPending}
          isError={save.isError}
          error={save.error}
          saved={save.saved}
          onRetry={save.retry}
        />
      </CardContent>
    </Card>
  );
}
