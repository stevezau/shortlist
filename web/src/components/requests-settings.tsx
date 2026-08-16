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
import type { RatingSource } from "@/lib/rating-sources";
import { RATING_LABELS, RATING_SOURCES } from "@/lib/rating-sources";
import { useAutosavedSettings } from "@/lib/autosave";
import { settingBool, settingNumber, settingString } from "@/lib/format";
import { useArrOptions } from "@/lib/queries";
import { hasMdblist } from "@/lib/sources";
import type { Settings } from "@/lib/types";

const MAX_PER_RUN = [3, 5, 10];

// Which score gates a title. TMDB needs no setup; the rest come from MDBList (one call, cached).

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
                ? "Fetches the films Shortlist asks for."
                : "Fetches the TV shows Shortlist asks for."}
            </p>
          </div>
        </div>

        {/* Profiles and folders come from the app itself once it's connected — no hunting for ids. */}
        {!connected ? (
          <div className="space-y-2 rounded-md border border-dashed bg-muted/30 p-3">
            <p className="text-sm text-muted-foreground">
              {title} isn&rsquo;t connected yet. Add its address and API key on
              the {title} card in{" "}
              <strong className="font-medium text-foreground">
                Connections
              </strong>
              , then come back and choose how good a copy to grab and which
              folder to save it in.
            </p>
            <Button variant="outline" size="sm" onClick={onGoToConnections}>
              Go to Connections
            </Button>
          </div>
        ) : options.isError ? (
          <p className="text-sm text-destructive-text">
            Couldn&rsquo;t reach {title} to load its quality profiles and
            folders. Check its address and API key on the {title} card in
            Connections, and press Test there.
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
  const ratingLabel = RATING_LABELS[form.ratingSource];
  // MDBList reports a vote count only for the audience-scored sources; the engine's rating gate
  // skips the vote floor for the two critic scores (`VOTE_SOURCES` in clients/mdblist.py).
  const countsVotes = !["tomatoes", "metacritic"].includes(form.ratingSource);

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
              Ask Radarr or Sonarr for titles that would have been good picks
              but aren&rsquo;t in your library. You choose which go out on their
              own and which wait in{" "}
              <strong className="font-medium text-foreground">Requests</strong>{" "}
              for a yes or no.
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
            Off &mdash; runs won&rsquo;t ask Radarr or Sonarr for anything, and
            nothing new reaches your Requests inbox. Turn it on to connect the
            apps and set the rules.
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
                Every film or show Shortlist asks for gets this label in
                Radarr/Sonarr &mdash; a &ldquo;tag&rdquo;, in their words, which
                Shortlist creates there if it doesn&rsquo;t already exist. It
                lets you spot or filter what Shortlist added. Leave blank for no
                tag.
              </p>
            </div>

            {/* Deliberately BEFORE Guardrails. Read the other way round, "Minimum rating 7" looked
                like the bar for requesting at all, and the owner only met the second, higher bar two
                fieldsets later. The big choice — sent on its own, or waits for you — comes first;
                the floor underneath both comes after it. */}
            <fieldset className="space-y-4 rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">
                Send on its own, or ask me first
              </legend>
              <div className="flex items-start justify-between gap-4">
                <div className="space-y-1">
                  <p className="text-sm font-medium">
                    Send the strongest titles without asking
                  </p>
                  <p className="text-sm text-muted-foreground">
                    Titles that clear the higher bars here go out as soon as a
                    run finds them. Everything else that clears your guardrails
                    waits in your Requests inbox. Turn this off to look at every
                    title yourself.
                  </p>
                </div>
                <Switch
                  checked={form.autoSend}
                  onCheckedChange={(autoSend) => set({ autoSend })}
                  aria-label="Send the strongest titles without asking"
                />
              </div>

              {form.autoSend && (
                <>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div className="space-y-2">
                      <Label htmlFor={autoDemandId}>
                        Send without asking when wanted by
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
                      <Label htmlFor={autoRatingId}>
                        Send without asking when rated
                      </Label>
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
                        At least this {ratingLabel} score. Anything lower waits
                        for your OK.
                      </p>
                    </div>
                    {/* Weaker than "everything will be sent", on purpose: this fires when EITHER bar
                        is below its guardrail, and one low bar only means that bar never holds a
                        title back. */}
                    {(form.autoMinDemand < form.minDemand ||
                      form.autoMinRating < form.minRating) && (
                      <p
                        role="alert"
                        className="text-sm text-warning sm:col-span-2"
                      >
                        A bar lower than the matching guardrail below stops
                        nothing &mdash; anything that gets past the guardrails
                        already clears it. Raise it above the guardrail to keep
                        a queue to review.
                      </p>
                    )}
                  </div>

                  <div className="space-y-2">
                    {/* Lives here, not in Guardrails: the cap is only ever applied to automatic
                        sends (`request_missing` checks it after the auto bars), and with this
                        switch off it is never reached at all. */}
                    <Segmented
                      legend="Most to send automatically in one run"
                      value={String(form.maxPerRun)}
                      options={MAX_PER_RUN.map((n) => ({
                        value: String(n),
                        label: String(n),
                      }))}
                      onChange={(v) => set({ maxPerRun: Number(v) })}
                    />
                    {/* "per run", not "per night": `max_per_run` is counted once per run, and rows
                        carry their own schedules, so a server can run more than once a night. */}
                    <p className="text-sm text-muted-foreground">
                      A hard cap on the titles a single run sends on its own,
                      across both apps, so one run can never flood your
                      downloads. Titles you approve by hand in the Requests
                      inbox aren&rsquo;t capped.
                    </p>
                  </div>
                </>
              )}
            </fieldset>

            <fieldset className="space-y-4 rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">Guardrails</legend>
              <p className="text-sm text-muted-foreground">
                The lowest bar a title must clear before Shortlist will ask for
                it at all &mdash; whether it goes out on its own or waits in
                your inbox.
              </p>

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
                      Using your MDBList connection. The key lives on the
                      MDBList card in{" "}
                      <button
                        type="button"
                        onClick={goToConnections}
                        className="font-medium text-primary underline underline-offset-2"
                      >
                        Connections
                      </button>
                      , where you can change or test it.
                    </p>
                  ) : (
                    <div
                      role="alert"
                      className="space-y-2 rounded-lg border border-warning/40 bg-warning/5 p-4"
                    >
                      <p className="text-sm font-medium">
                        MDBList isn&rsquo;t connected
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {RATING_LABELS[form.ratingSource]} scores come from
                        MDBList &mdash; one lookup that returns a title&rsquo;s
                        score on every site. Paste its free API key on the
                        MDBList card in Connections, or Shortlist falls back to
                        TMDB scores and this choice won&rsquo;t take effect.
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
                  <Label htmlFor={votesId}>Minimum votes</Label>
                  <Input
                    id={votesId}
                    type="number"
                    min={0}
                    step={10}
                    value={form.minVotes}
                    onChange={(e) => set({ minVotes: Number(e.target.value) })}
                    className="w-28"
                  />
                  {/* Rotten Tomatoes and Metacritic are critics' verdicts, and MDBList reports no
                      vote count for them — `_gate_by_source` skips the floor entirely for those two
                      (`VOTE_SOURCES`). Saying "keeps out obscure titles" there would be a lie. */}
                  <p className="text-sm text-muted-foreground">
                    {countsVotes
                      ? `Keeps out obscure titles with a high ${ratingLabel} score from very few votes.`
                      : `${ratingLabel} is a critics' verdict rather than a public vote, so this number is ignored while it is your chosen source.`}
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
                  {/* Deliberately not "whose picks it appears in": a missing title can never BE
                      anyone's pick (`filter_candidates` drops everything the library lacks). The
                      count is the people Shortlist considered it for — `_record_demand`. */}
                  <p className="text-sm text-muted-foreground">
                    How many different people it has to be a good match for
                    before Shortlist asks. 1 = anyone.
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
                      <p className="text-sm text-destructive-text">
                        The latest year is before the earliest &mdash; no titles
                        can match this range.
                      </p>
                    )}
                </div>
              </div>
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
