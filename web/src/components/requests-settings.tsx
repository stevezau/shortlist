import { Film, Inbox, Tv } from "lucide-react";
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
import {
  COMMON_LANGUAGES,
  LANGUAGE_MODE_HINTS,
  LANGUAGE_MODE_LABELS,
  LANGUAGE_MODES,
  OTHER_LANGUAGE_BAR_GAP,
  asLanguageMode,
  languageName,
  otherLanguageBar,
  type LanguageMode,
} from "@/lib/request-language";
import { useAutosavedSettings } from "@/lib/autosave";
import { settingBool, settingNumber, settingString } from "@/lib/format";
import { useArrOptions, useSeerrOptions } from "@/lib/queries";
import {
  autoSendBarsMatchGuardrails,
  describeRequestFlow,
} from "@/lib/request-flow";
import type { SonarrMonitor } from "@/lib/sonarr-monitor";
import {
  asSonarrMonitor,
  SONARR_MONITOR_HINTS,
  SONARR_MONITOR_LABELS,
  SONARR_MONITOR_MODES,
} from "@/lib/sonarr-monitor";
import { hasMdblist } from "@/lib/sources";
import type { Settings } from "@/lib/types";

const MAX_PER_RUN = [3, 5, 10];

// Which score gates a title. TMDB needs no setup; the rest come from MDBList (one call, cached).

type ArrForm = {
  qualityProfileId: number;
  rootFolder: string;
};

/** Where a request is filed. "arr" posts to Radarr/Sonarr; "overseerr" hands the title to
 *  Overseerr/Jellyseerr and lets IT drive them. Exclusive — never both. */
type RequestTarget = "arr" | "overseerr";

/** Every editable requests setting in one object, so the panel updates it with a single patcher. */
interface RequestsForm {
  enabled: boolean;
  target: RequestTarget;
  /** Which Overseerr account requests go out as. 0 = the API key's own (usually auto-approving). */
  overseerrUserId: number;
  radarr: ArrForm;
  sonarr: ArrForm;
  /** How much of a show Sonarr monitors when a request goes out. Sonarr's own Add Series choice. */
  sonarrMonitor: SonarrMonitor;
  ratingSource: RatingSource;
  /** How the gate treats a title's original language. "any" is the shipped default. */
  languageMode: LanguageMode;
  preferredLanguages: string[];
  /** null = follow `minRating` + 1.5. Not 0 — 0 is a real bar that nothing can fail. */
  minRatingOther: number | null;
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

/** A stored language list, defensively cleaned — the API accepts any two-letter code, and this
 * screen must render whatever is already in the DB rather than assume it wrote it. */
function readLanguages(raw: unknown): string[] {
  if (!Array.isArray(raw)) return ["en"];
  return raw
    .filter((c): c is string => typeof c === "string")
    .map((c) => c.trim().toLowerCase())
    .filter((c, i, all) => c.length === 2 && all.indexOf(c) === i);
}

/** A stored number, or null — never a silent 0. `null` is "follow the minimum rating". */
function readOptionalNumber(raw: unknown): number | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
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
    target:
      settingString(settings, "requests.target", "arr") === "overseerr"
        ? "overseerr"
        : "arr",
    overseerrUserId: settingNumber(
      settings,
      "requests.overseerr.request_as_user_id",
      0,
    ),
    radarr: readArr(settings, "requests.radarr"),
    sonarr: readArr(settings, "requests.sonarr"),
    sonarrMonitor: asSonarrMonitor(settings["requests.sonarr.monitor"]),
    ratingSource: RATING_SOURCES.includes(
      settingString(settings, "requests.rating_source", "tmdb") as RatingSource,
    )
      ? (settingString(
          settings,
          "requests.rating_source",
          "tmdb",
        ) as RatingSource)
      : "tmdb",
    languageMode: asLanguageMode(settings["requests.language_mode"]),
    preferredLanguages: readLanguages(settings["requests.preferred_languages"]),
    // Read WITHOUT a `??` fallback to a number: null is a MEANING here ("follow the minimum
    // rating"), so defaulting it to one would show the owner a bar they never chose.
    minRatingOther: readOptionalNumber(settings["requests.min_rating_other"]),
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

// Split so a caller can opt OUT of full width. Appending `w-auto` to a class string that already
// contains `w-full` does not work: both utilities land in the same Tailwind layer, so the winner is
// whichever comes later in the GENERATED stylesheet, not in the attribute. The language picker asked
// for `w-auto` and rendered full-panel-width for a two-letter choice.
const selectBase =
  "h-9 rounded-md border bg-elevated px-3 text-sm focus-visible:outline-none " +
  "focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60";
const selectClass = `${selectBase} w-full`;

/** One app's where-to-file choices for requests (Radarr for movies, Sonarr for shows). The
 *  connection itself — address + API key — lives in Settings → Connections; this only picks the
 *  quality profile, folder and (Sonarr) how much of a show to take, and only once that app is
 *  connected. */
function ArrCard({
  service,
  title,
  icon,
  form,
  onChange,
  connected,
  onGoToConnections,
  monitor,
  onMonitorChange,
}: {
  service: "radarr" | "sonarr";
  title: string;
  icon: ReactNode;
  form: ArrForm;
  onChange: (next: ArrForm) => void;
  /** True once this app's URL + key are SAVED (in Connections), so its profiles/folders can load. */
  connected: boolean;
  onGoToConnections: () => void;
  /** Sonarr only — how much of a show to take. Films have no seasons, so Radarr passes neither. */
  monitor?: SonarrMonitor;
  onMonitorChange?: (next: SonarrMonitor) => void;
}) {
  const options = useArrOptions(service, connected);
  const profileId = useId();
  const folderId = useId();
  const monitorId = useId();

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
        ) : (
          <>
            {options.isError ? (
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
                      {options.isPending
                        ? "Loading…"
                        : "Choose a quality profile"}
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

            {/* Sonarr only, and outside the error branch above on purpose: this list is Sonarr's
                enum, not something fetched, so an unreachable Sonarr is no reason to hide it. */}
            {monitor !== undefined && onMonitorChange && (
              <div className="space-y-2">
                <Label htmlFor={monitorId}>How much of a show to grab</Label>
                <select
                  id={monitorId}
                  className={selectClass}
                  value={monitor}
                  onChange={(e) =>
                    onMonitorChange(asSonarrMonitor(e.target.value))
                  }
                >
                  {SONARR_MONITOR_MODES.map((mode) => (
                    <option key={mode} value={mode}>
                      {SONARR_MONITOR_LABELS[mode]}
                    </option>
                  ))}
                </select>
                <p className="text-sm text-muted-foreground">
                  {SONARR_MONITOR_HINTS[monitor]}
                </p>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** Where requests go when Overseerr/Jellyseerr is the target. Deliberately much smaller than
 *  `ArrCard`: quality profile, root folder and 4K routing are the *seerr's own rules, which is the
 *  whole reason to route through it. The only choice left is whose name the request goes out under. */
/** What picking this account will DO, in the dropdown itself.
 *
 *  The difference between "filed for you to look at" and "already downloading" is the single most
 *  consequential thing on this screen, and it is a property of the ACCOUNT, not of anything Shortlist
 *  sends. Leaving it to be discovered from where the titles ended up is how someone finds out the
 *  hard way. Partial is real and worth naming: an account can auto-approve films but not shows. */
function accountEffect(u: {
  auto_approve_movies?: boolean;
  auto_approve_tv?: boolean;
}): string {
  const films = u.auto_approve_movies ?? false;
  const shows = u.auto_approve_tv ?? false;
  if (films && shows) return "approves automatically";
  if (!films && !shows) return "requests wait for approval";
  return films
    ? "films approve automatically, shows wait"
    : "shows approve automatically, films wait";
}

function OverseerrCard({
  userId,
  onUserChange,
  connected,
  onGoToConnections,
}: {
  userId: number;
  onUserChange: (next: number) => void;
  connected: boolean;
  onGoToConnections: () => void;
}) {
  const options = useSeerrOptions(connected);
  const userSelectId = useId();
  // Undefined while the list is still loading as well as when it genuinely lacks the account —
  // both mean "cannot name it yet", which is exactly when the fallback option below is needed.
  const defaultAccount = options.data?.users.find(
    (u) => u.id === options.data?.default_user_id,
  );
  // Resolve 0 ("Server default") to the account the API key actually is, the same way the panel
  // does. Looking up the raw 0 finds nothing, so everything keyed on `chosen` silently vanished on
  // the one setting almost everybody leaves alone.
  const chosen =
    options.data?.users.find(
      (u) => u.id === (userId || options.data?.default_user_id),
    ) ?? undefined;
  const others = (options.data?.users ?? []).filter(
    (u) => u.id !== options.data?.default_user_id,
  );
  const serviceAccounts = others.filter((u) => !u.is_plex_user);
  // Real people are NOT offered, and that is a decision rather than an omission. A title here is
  // wanted by several people at once — that is the whole demand model — while an Overseerr request
  // has exactly one requester. Choosing a person does not file each title under whoever wanted it;
  // it files EVERY title under that one person for ever, including ones they had nothing to do
  // with. The job people might reach for it to do — holding requests for approval — is what a local
  // account does properly, which is what the help text recommends.
  const peopleHidden = others.length - serviceAccounts.length;
  // An account already able to hold requests for review, if there is one.
  const holdingAccount = serviceAccounts.find(
    (u) => !u.auto_approve_movies && !u.auto_approve_tv,
  );

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <div className="flex items-center gap-2.5">
          <span className="grid h-9 w-9 place-items-center rounded-lg border bg-elevated text-primary [&>svg]:h-5 [&>svg]:w-5">
            <Inbox aria-hidden="true" />
          </span>
          <div>
            <p className="font-medium">Overseerr / Jellyseerr</p>
            <p className="text-sm text-muted-foreground">
              Files a request for films and shows alike, and it decides the
              quality, the folder and who approves it. Overseerr, Jellyseerr and
              Seerr all speak the same API, so any of them works here.
            </p>
          </div>
        </div>

        {!connected ? (
          <div className="space-y-2 rounded-md border border-dashed bg-muted/30 p-3">
            <p className="text-sm text-muted-foreground">
              Overseerr isn&rsquo;t connected yet. Add its address and API key
              on the Overseerr card in{" "}
              <strong className="font-medium text-foreground">
                Connections
              </strong>
              , then come back and choose who requests go out as.
            </p>
            <Button variant="outline" size="sm" onClick={onGoToConnections}>
              Go to Connections
            </Button>
          </div>
        ) : (
          <div className="space-y-2">
            {/* Label and control on one line. A single short choice in a full-width box reads as an
                empty text field waiting to be typed into — the two Arr cards get away with w-full
                because they sit two-up in a grid, and this one does not. Stacks under `sm`. */}
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <Label htmlFor={userSelectId} className="shrink-0">
                Request as
              </Label>
              {/* The error is shown ABOVE the control, never instead of it. An unreachable Overseerr
                cannot name its accounts, but the choice already saved is still the owner's to see
                and to undo — hiding the select left someone whose instance was briefly down unable
                to put it back to Server default. Same reasoning as the Sonarr monitor select. */}
              {options.isError && (
                <p className="text-sm text-destructive-text">
                  Couldn&rsquo;t reach Overseerr to load its accounts. Check its
                  address and API key on the Overseerr card in Connections, and
                  press Test there. You can still change this back to the server
                  default in the meantime.
                </p>
              )}
              <select
                id={userSelectId}
                className={`${selectBase} w-full sm:w-auto sm:min-w-[24rem] sm:max-w-full`}
                disabled={options.isPending}
                value={userId}
                onChange={(e) => onUserChange(Number(e.target.value))}
              >
                {/* The default carries its effect too, and it is the one that matters most — it is
                  what nearly everyone will leave selected. `default_user_id` is what makes it
                  nameable at all; without it this said "whoever owns the API key", which is a
                  shrug where the consequence should be. */}
                <option value={0}>
                  {options.isPending
                    ? "Loading…"
                    : defaultAccount
                      ? `Server default (${defaultAccount.name}) — ${accountEffect(defaultAccount)}`
                      : "Server default (whoever owns the API key)"}
                </option>
                {/* Only accounts made FOR this. The default is already the option above, and real
                  people are deliberately absent — see `peopleHidden` below. */}
                {serviceAccounts.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.name} &mdash; {accountEffect(u)}
                  </option>
                ))}
                {/* A saved account the list does not contain — because the fetch failed, or because
                  it was since deleted in Overseerr. Without it the select falls back to its first
                  option and the screen silently misreports the saved value as "Server default",
                  which the next autosave would then WRITE. Keyed on the LIST, not on isError, so
                  the deleted-account case is covered by the same three lines. */}
                {userId !== 0 && !chosen && (
                  <option value={userId}>Account #{userId}</option>
                )}
              </select>
            </div>
            {/* Only the ACTION. The dropdown above already states whether this account approves,
                and the summary below states what that means for a title — saying it a third time
                here meant hedging ("usually an admin, so they'll be approved") what the dropdown
                states as fact, and promising "go straight to Radarr/Sonarr", which is Overseerr's
                own setup to decide, not ours. An account that already holds requests needs no line
                at all: the other two have said it. */}
            {chosen &&
              (chosen.auto_approve_movies || chosen.auto_approve_tv) && (
                <p className="text-sm text-muted-foreground">
                  Want to check them in Overseerr first?{" "}
                  {holdingAccount ? (
                    <>
                      Pick{" "}
                      <strong className="font-medium text-foreground">
                        {holdingAccount.name}
                      </strong>{" "}
                      above.
                    </>
                  ) : (
                    <>
                      Make a user there without auto-approve, and pick it here.
                    </>
                  )}
                </p>
              )}
            {peopleHidden > 0 && (
              <p className="text-sm text-muted-foreground">
                People on your server aren&rsquo;t listed &mdash; a title is
                usually wanted by several at once, so one name would end up on
                everything.
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function RequestsSettings({ settings }: { settings: Settings }) {
  const [form, setForm] = useState<RequestsForm>(() => readForm(settings));
  const viaSeerr = form.target === "overseerr";
  const set = (patch: Partial<RequestsForm>) =>
    setForm((prev) => ({ ...prev, ...patch }));

  // The MDBList key now lives in Settings → Connections (like TMDB/Trakt). Here we only need to know
  // whether it's set up, so a non-TMDB rating source can warn when it isn't.
  const mdblistConnected = hasMdblist(settings);

  const ratingId = useId();
  const ratingOtherId = useId();
  const addLanguageId = useId();
  const votesId = useId();
  const demandId = useId();
  const yearId = useId();
  const yearMaxId = useId();
  const autoDemandId = useId();
  const autoRatingId = useId();
  const tagId = useId();
  const autoUserTagId = useId();
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
  const overseerrConnected =
    Boolean(settingString(settings, "requests.overseerr.url")) &&
    settingString(settings, "requests.overseerr.apikey") === REDACTED;

  // Shared with OverseerrCard's own call — TanStack dedupes on the key, so asking twice costs
  // nothing and neither component has to thread the list through the other.
  const seerrUsers = useSeerrOptions(viaSeerr && overseerrConnected);
  // "Server default" (0) means the account the API key itself is, which the options endpoint names —
  // so the commonest setting resolves to a real row rather than an unknown.
  const effectiveUserId =
    form.overseerrUserId || (seerrUsers.data?.default_user_id ?? -1);
  const chosenAccount = seerrUsers.data?.users.find(
    (u) => u.id === effectiveUserId,
  );
  // `null` = not knowable yet (list still loading, instance unreachable, or an account it will not
  // name). The summary then states what is certain and says nothing about approval, rather than
  // guessing at the one fact on this screen that is expensive to get wrong.
  // Three-valued, matching `accountEffect` — an account can auto-approve films and not shows, and
  // flattening that to a boolean made this summary contradict the card right above it.
  const seerrApproves: "all" | "none" | "partial" | null =
    !viaSeerr || !chosenAccount
      ? null
      : chosenAccount.auto_approve_movies && chosenAccount.auto_approve_tv
        ? "all"
        : !chosenAccount.auto_approve_movies && !chosenAccount.auto_approve_tv
          ? "none"
          : "partial";
  const flow = describeRequestFlow({
    viaSeerr,
    autoSend: form.autoSend,
    everythingAutoSends: autoSendBarsMatchGuardrails({
      autoMinDemand: form.autoMinDemand,
      autoMinRating: form.autoMinRating,
      minDemand: form.minDemand,
      minRating: form.minRating,
    }),
    seerrApproves,
    maxPerRun: form.maxPerRun,
  });

  const goToConnections = () =>
    document
      .getElementById("connections")
      ?.scrollIntoView({ behavior: "smooth" });

  // Auto-save: no Save button. Any change persists shortly after you stop (so text fields never
  // save mid-keystroke; toggles feel instant).
  const save = useAutosavedSettings(form, () => {
    const values: Settings = {
      "requests.enabled": form.enabled,
      "requests.target": form.target,
      "requests.overseerr.request_as_user_id": form.overseerrUserId,
      // Address + API key are owned by Settings → Connections now; this form only saves the
      // request-filing choices (quality profile + folder) and the policy below.
      "requests.radarr.quality_profile_id": form.radarr.qualityProfileId,
      "requests.radarr.root_folder": form.radarr.rootFolder,
      "requests.sonarr.quality_profile_id": form.sonarr.qualityProfileId,
      "requests.sonarr.root_folder": form.sonarr.rootFolder,
      "requests.sonarr.monitor": form.sonarrMonitor,
      "requests.rating_source": form.ratingSource,
      "requests.language_mode": form.languageMode,
      "requests.preferred_languages": form.preferredLanguages,
      "requests.min_rating_other": form.minRatingOther,
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
              Ask for titles that would have been good picks but aren&rsquo;t in
              your library &mdash; straight from Radarr and Sonarr, or as a
              request in Overseerr. You choose which go out on their own and
              which wait in{" "}
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
            Off &mdash; runs won&rsquo;t ask for anything, and nothing new
            reaches your Requests inbox. Turn it on to connect an app and set
            the rules.
          </p>
        )}

        {form.enabled && (
          <div className="space-y-5 border-t pt-5">
            <div className="space-y-2">
              <Segmented<RequestTarget>
                legend="Where requests go"
                value={form.target}
                onChange={(target) => set({ target })}
                options={[
                  { value: "arr", label: "Radarr & Sonarr" },
                  { value: "overseerr", label: "Overseerr / Jellyseerr" },
                ]}
              />
              <p className="text-sm text-muted-foreground">
                {viaSeerr
                  ? "Shortlist files a request in Overseerr instead of adding the title itself. Overseerr picks the quality and folder, and handles approval — so what Shortlist asks for shows up alongside everything your users request."
                  : "Shortlist adds the title to Radarr or Sonarr itself, using the quality and folder you pick below."}
              </p>
            </div>

            {viaSeerr && !overseerrConnected && (
              <div className="space-y-2 rounded-lg border border-primary/40 bg-primary/5 p-4">
                <p className="text-sm font-medium">
                  Connect Overseerr to start requesting
                </p>
                <p className="text-sm text-muted-foreground">
                  Add its address and API key in the Connections section, then
                  come back here to set the rules. Jellyseerr works the same
                  way.
                </p>
                <Button variant="outline" size="sm" onClick={goToConnections}>
                  Go to Connections
                </Button>
              </div>
            )}
            {!viaSeerr && !radarrConnected && !sonarrConnected && (
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

            {viaSeerr ? (
              <OverseerrCard
                userId={form.overseerrUserId}
                onUserChange={(overseerrUserId) => set({ overseerrUserId })}
                connected={overseerrConnected}
                onGoToConnections={goToConnections}
              />
            ) : (
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
                  monitor={form.sonarrMonitor}
                  onMonitorChange={(sonarrMonitor) => set({ sonarrMonitor })}
                />
              </div>
            )}

            {/* Both tag controls are Arr-only, and hidden rather than disabled on the *seerr route.
                Overseerr's POST /request body carries no tags field at all, so leaving them on
                screen would offer a setting that silently does nothing. The "request as" account on
                the card above is the attribution that replaces them. */}
            {!viaSeerr && (
              <>
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
                    Radarr/Sonarr &mdash; a &ldquo;tag&rdquo;, in their words,
                    which Shortlist creates there if it doesn&rsquo;t already
                    exist. It lets you spot or filter what Shortlist added.
                    Leave blank for no tag.
                  </p>
                </div>

                <div className="flex items-start justify-between gap-4">
                  <div className="space-y-1">
                    <Label htmlFor={autoUserTagId}>Also tag by person</Label>
                    <p className="text-sm text-muted-foreground">
                      Adds the name of whoever a title was picked for as a
                      second tag, so you can tell in Radarr/Sonarr who it was
                      added for &mdash; without setting a tag on every user by
                      hand. Someone with their own tag keeps it. Individual rows
                      can opt in or out in the row editor.
                    </p>
                  </div>
                  <Switch
                    id={autoUserTagId}
                    checked={form.autoUserTag}
                    onCheckedChange={(on) => set({ autoUserTag: on })}
                    aria-label="Also tag requests with the name of the person they're for"
                  />
                </div>
              </>
            )}

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
                  {/* The live answer to "what happens tonight?", instead of three settings the
                      reader has to combine in their head — see lib/request-flow.ts. */}
                  <p className="text-sm text-muted-foreground">
                    {flow.summary}
                  </p>
                </div>
                <Switch
                  checked={form.autoSend}
                  onCheckedChange={(autoSend) => set({ autoSend })}
                  aria-label="Send the strongest titles without asking"
                />
              </div>

              {/* Named, never blocked: approving twice is a legitimate choice, just almost never a
                  deliberate one. Says how to get to a single gate rather than only that there are two. */}
              {flow.doubleApproval && (
                <p className="rounded-md border border-primary/40 bg-primary/5 p-3 text-sm text-muted-foreground">
                  {flow.doubleApproval}
                </p>
              )}

              {form.autoSend && (
                <>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div className="space-y-2">
                      <Label htmlFor={autoDemandId}>
                        Send without asking when wanted by
                      </Label>
                      {/* The unit sits beside the box, so the control reads as the sentence its
                          label starts: "wanted by [4] people". Without it the label is a fragment
                          and the number could be anything. */}
                      <div className="flex items-center gap-2">
                        <Input
                          id={autoDemandId}
                          type="number"
                          min={1}
                          step={1}
                          value={form.autoMinDemand}
                          onChange={(e) =>
                            set({
                              autoMinDemand: Math.max(
                                1,
                                Number(e.target.value),
                              ),
                            })
                          }
                          className="w-24"
                        />
                        <span className="text-sm text-muted-foreground">
                          {form.autoMinDemand === 1 ? "person" : "people"}
                        </span>
                      </div>
                      <p className="text-sm text-muted-foreground">
                        Wanted by fewer than this? It waits in the inbox.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor={autoRatingId}>
                        Send without asking when rated
                      </Label>
                      {/* Names the source the number is measured on — it follows the rating source
                          above, so this reads "or higher on IMDb" when that is what is judging. */}
                      <div className="flex items-center gap-2">
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
                          className="w-24"
                        />
                        <span className="text-sm text-muted-foreground">
                          or higher on {ratingLabel}
                        </span>
                      </div>
                      <p className="text-sm text-muted-foreground">
                        Anything lower waits for your OK.
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

              <div className="space-y-2">
                <Segmented
                  legend="Language"
                  value={form.languageMode}
                  options={LANGUAGE_MODES.map((mode) => ({
                    value: mode,
                    label: LANGUAGE_MODE_LABELS[mode],
                  }))}
                  onChange={(languageMode) => set({ languageMode })}
                />
                <p className="text-sm text-muted-foreground">
                  {LANGUAGE_MODE_HINTS[form.languageMode]}
                </p>
                {form.languageMode !== "any" && (
                  <div className="space-y-2 pt-1">
                    <div className="flex flex-wrap items-center gap-2">
                      {form.preferredLanguages.map((code) => (
                        <span
                          key={code}
                          className="inline-flex items-center gap-1.5 rounded-full border border-primary/40 bg-primary/5 py-1 pl-3 pr-1 text-sm"
                        >
                          {languageName(code)}
                          <span className="font-mono text-xs text-muted-foreground">
                            {code}
                          </span>
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="h-6 w-6 rounded-full p-0"
                            aria-label={`Remove ${languageName(code)}`}
                            onClick={() =>
                              set({
                                preferredLanguages:
                                  form.preferredLanguages.filter(
                                    (c) => c !== code,
                                  ),
                              })
                            }
                          >
                            &times;
                          </Button>
                        </span>
                      ))}
                      <select
                        id={addLanguageId}
                        aria-label="Add a language"
                        className={`${selectBase} h-8 w-auto`}
                        value=""
                        onChange={(e) => {
                          const code = e.target.value;
                          if (!code) return;
                          set({
                            preferredLanguages: [
                              ...form.preferredLanguages,
                              code,
                            ],
                          });
                        }}
                      >
                        <option value="">Add a language…</option>
                        {COMMON_LANGUAGES.filter(
                          (c) => !form.preferredLanguages.includes(c),
                        ).map((c) => (
                          <option key={c} value={c}>
                            {languageName(c)} ({c})
                          </option>
                        ))}
                      </select>
                    </div>
                    {form.preferredLanguages.length === 0 && (
                      <p role="alert" className="text-sm text-destructive-text">
                        {form.languageMode === "only"
                          ? "With no languages listed, Shortlist will never ask for anything. Add at least one."
                          : "With no languages listed, every title Shortlist can identify a language for counts as another language and has to clear the higher bar."}
                      </p>
                    )}
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
                {form.languageMode === "prefer" && (
                  <div className="space-y-2">
                    <Label htmlFor={ratingOtherId}>
                      Minimum {ratingLabel} rating, other languages
                    </Label>
                    <Input
                      id={ratingOtherId}
                      type="number"
                      min={0}
                      max={10}
                      step={0.1}
                      value={otherLanguageBar(
                        form.minRating,
                        form.minRatingOther,
                      )}
                      onChange={(e) =>
                        // "" must become null, not 0. `Number("") === 0`, and 0 is a REAL bar here
                        // (nothing can fail it) — so clearing the box would silently turn "Prefer
                        // these" into "Any language" for auto-send. Clearing is the natural inverse
                        // of the hint's "Type a number to set it yourself", so it has to mean un-pin.
                        set({
                          minRatingOther:
                            e.target.value === ""
                              ? null
                              : Number(e.target.value),
                        })
                      }
                      className="w-28"
                    />
                    <p className="text-sm text-muted-foreground">
                      {form.minRatingOther === null
                        ? `Following your minimum rating, plus ${OTHER_LANGUAGE_BAR_GAP}. Type a number to set it yourself.`
                        : "What a title in another language has to score to be asked for on its own. Anything lower waits in your inbox."}
                    </p>
                    {form.minRatingOther !== null && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-auto p-0 text-sm font-medium text-primary underline underline-offset-2"
                        onClick={() => set({ minRatingOther: null })}
                      >
                        Follow my minimum rating again
                      </Button>
                    )}
                    {form.minRatingOther !== null &&
                      form.minRatingOther < form.minRating && (
                        <p
                          role="alert"
                          className="text-sm text-destructive-text"
                        >
                          This is below your minimum rating of {form.minRating},
                          so it never applies — a title under {form.minRating}{" "}
                          is already out.
                        </p>
                      )}
                  </div>
                )}
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
