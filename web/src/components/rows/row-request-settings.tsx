/**
 * A row's own Sonarr/Radarr request settings, in the row editor.
 *
 * Its own component rather than another 250 lines in `row-editor.tsx`, which is already long enough
 * that a reader has to scroll past four unrelated sections to find anything.
 *
 * Two rules shape what is offered here, and both are enforced on the server too — this UI is the
 * explanation, never the enforcement:
 *
 *  - **Ceilings are not overridable.** `max_per_run` and the rating source belong to the run and to
 *    the one MDBList account. A row may set `req_max_per_row` to take LESS of the run's cap; it can
 *    never take more, so the caption says what the run allows rather than offering to raise it.
 *  - **Only the filing choices are per row** — profile, root folder, and how much of a show Sonarr
 *    takes. URL and API key stay global: the case this serves is one Radarr filing a kids row into
 *    /data/Kids at a lower profile, not a second Radarr.
 *
 * Not rendered at all for a shared row. A shared row is built from titles people have already
 * WATCHED, which are by definition already on the server, so it can never surface a missing title to
 * request — controls here would be offered and then silently ignored.
 */
import type { ReactNode } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  requestAutoSendGlobal,
  requestAutoUserTagGlobal,
  requestDemandGlobal,
  requestMaxPerRunGlobal,
  requestRatingGlobal,
  requestRootFolderGlobal,
  requestSonarrMonitorGlobal,
  requestYearGlobal,
} from "@/lib/row-globals";
import type { RowSonarrMonitor } from "@/lib/sonarr-monitor";
import {
  asSonarrMonitor,
  SONARR_MONITOR_HINTS,
  SONARR_MONITOR_LABELS,
  SONARR_MONITOR_MODES,
} from "@/lib/sonarr-monitor";
import type { Settings } from "@/lib/types";

/** The subset of the row draft this section reads and writes. */
export type RowRequestInput = {
  req_min_rating: number | null;
  req_min_demand: number | null;
  req_min_year: number | null;
  req_max_year: number | null;
  req_auto_send: boolean | null;
  req_auto_user_tag: boolean | null;
  req_max_per_row: number | null;
  req_radarr_root_folder: string | null;
  req_radarr_quality_profile_id: number | null;
  req_sonarr_root_folder: string | null;
  req_sonarr_quality_profile_id: number | null;
  req_sonarr_monitor: RowSonarrMonitor;
};

function Field({
  label,
  labelFor,
  description,
  inheriting,
  globalValue,
  onToggle,
  ariaLabel,
  children,
}: {
  label: string;
  labelFor?: string;
  description: ReactNode;
  inheriting: boolean;
  globalValue: string | null;
  onToggle: (usesGlobal: boolean) => void;
  ariaLabel: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-3 border-t pt-4">
      {labelFor ? (
        <Label htmlFor={labelFor}>{label}</Label>
      ) : (
        <p className="text-sm font-medium">{label}</p>
      )}
      <p className="text-sm text-muted-foreground">{description}</p>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={inheriting}
          aria-label={ariaLabel}
          onChange={(e) => onToggle(e.target.checked)}
          className="h-4 w-4"
        />
        <span className="text-muted-foreground">
          Use the setting from Settings &rsaquo; Requests
          {globalValue ? ` (${globalValue})` : ""}
        </span>
      </label>
      {!inheriting && children}
    </div>
  );
}

export function RowRequestSettings({
  input,
  set,
  settings,
  requestsEnabled,
}: {
  input: RowRequestInput;
  set: (patch: Partial<RowRequestInput>) => void;
  settings: Settings | undefined;
  /** Whether Sonarr/Radarr requests are on at all. Off means these controls cannot do anything. */
  requestsEnabled: boolean;
}) {
  return (
    <div className="space-y-4">
      {!requestsEnabled && (
        <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
          Requests are turned off, so nothing here will be used yet. Turn them
          on in Settings &rsaquo; Requests and these become live.
        </p>
      )}

      <Field
        label="How many this row may ask for"
        labelFor="row-req-max"
        description={
          <>
            Rows share the run&rsquo;s limit evenly, and a row that can&rsquo;t
            fill its share passes it to the others. Set a lower number here to
            hold this row back; it can never take more than the run allows.
          </>
        }
        ariaLabel="Use the global limit for how many this row may request"
        inheriting={input.req_max_per_row === null}
        globalValue={requestMaxPerRunGlobal(settings)}
        onToggle={(on) => set({ req_max_per_row: on ? null : 1 })}
      >
        <Input
          id="row-req-max"
          type="number"
          min={0}
          max={100}
          value={input.req_max_per_row ?? 0}
          onChange={(e) =>
            set({ req_max_per_row: Number(e.target.value) || 0 })
          }
        />
        <p className="text-sm text-muted-foreground">
          {input.req_max_per_row === 0
            ? "This row never asks for anything on its own — its picks still wait in Requests for you to approve."
            : `At most ${input.req_max_per_row} per run from this row.`}
        </p>
      </Field>

      <Field
        label="Minimum rating"
        labelFor="row-req-rating"
        description="How well-reviewed a title must be before this row will ask for it."
        ariaLabel="Use the global minimum rating for this row"
        inheriting={input.req_min_rating === null}
        globalValue={requestRatingGlobal(settings)}
        onToggle={(on) => set({ req_min_rating: on ? null : 7 })}
      >
        <Input
          id="row-req-rating"
          type="number"
          min={0}
          max={10}
          step={0.1}
          value={input.req_min_rating ?? 7}
          onChange={(e) => set({ req_min_rating: Number(e.target.value) })}
        />
      </Field>

      <Field
        label="How many people must want it"
        labelFor="row-req-demand"
        description="Counted within this row only — someone who wants a title in a different row doesn't count towards this one."
        ariaLabel="Use the global demand threshold for this row"
        inheriting={input.req_min_demand === null}
        globalValue={requestDemandGlobal(settings)}
        onToggle={(on) => set({ req_min_demand: on ? null : 1 })}
      >
        <Input
          id="row-req-demand"
          type="number"
          min={1}
          value={input.req_min_demand ?? 1}
          onChange={(e) =>
            set({ req_min_demand: Math.max(1, Number(e.target.value)) })
          }
        />
      </Field>

      <Field
        label="Release years"
        description="Only ask for titles released in this range. Leave a box at 0 for no limit at that end."
        ariaLabel="Use the global release-year range for this row"
        inheriting={input.req_min_year === null && input.req_max_year === null}
        globalValue={requestYearGlobal(settings)}
        onToggle={(on) =>
          set(
            on
              ? { req_min_year: null, req_max_year: null }
              : { req_min_year: 0, req_max_year: 0 },
          )
        }
      >
        <div className="flex items-center gap-2">
          <Input
            aria-label="Earliest release year"
            type="number"
            min={0}
            max={2999}
            value={input.req_min_year ?? 0}
            onChange={(e) => set({ req_min_year: Number(e.target.value) || 0 })}
          />
          <span className="text-sm text-muted-foreground">to</span>
          <Input
            aria-label="Latest release year"
            type="number"
            min={0}
            max={2999}
            value={input.req_max_year ?? 0}
            onChange={(e) => set({ req_max_year: Number(e.target.value) || 0 })}
          />
        </div>
      </Field>

      <Field
        label="Ask automatically, or wait for you"
        description="Automatic means this row's strongest picks go straight to Sonarr/Radarr. Waiting puts them in Requests for you to approve."
        ariaLabel="Use the global auto-send setting for this row"
        inheriting={input.req_auto_send === null}
        globalValue={requestAutoSendGlobal(settings)}
        onToggle={(on) => set({ req_auto_send: on ? null : false })}
      >
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={input.req_auto_send ?? false}
            aria-label="Ask automatically for this row"
            onChange={(e) => set({ req_auto_send: e.target.checked })}
            className="h-4 w-4"
          />
          <span>Ask automatically</span>
        </label>
      </Field>

      <Field
        label="Tag requests with who they're for"
        description="Adds each person's name as a Sonarr/Radarr tag, so you can tell at a glance in there who a title was added for. Someone with their own tag set on their user page keeps that instead."
        ariaLabel="Use the global tag-by-person setting for this row"
        inheriting={input.req_auto_user_tag === null}
        globalValue={requestAutoUserTagGlobal(settings)}
        onToggle={(on) => set({ req_auto_user_tag: on ? null : false })}
      >
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={input.req_auto_user_tag ?? false}
            aria-label="Tag this row's requests by person"
            onChange={(e) => set({ req_auto_user_tag: e.target.checked })}
            className="h-4 w-4"
          />
          <span>Tag by person</span>
        </label>
      </Field>

      <Field
        label="Where films from this row land"
        labelFor="row-req-radarr-folder"
        description="The Radarr root folder and quality profile for films this row asks for. Everything else about the connection stays as set in Settings."
        ariaLabel="Use the global Radarr folder for this row"
        inheriting={
          input.req_radarr_root_folder === null &&
          input.req_radarr_quality_profile_id === null
        }
        globalValue={requestRootFolderGlobal(settings, "radarr")}
        onToggle={(on) =>
          set(
            on
              ? {
                  req_radarr_root_folder: null,
                  req_radarr_quality_profile_id: null,
                }
              : { req_radarr_root_folder: "" },
          )
        }
      >
        <Input
          id="row-req-radarr-folder"
          placeholder="/data/Kids Movies"
          value={input.req_radarr_root_folder ?? ""}
          onChange={(e) =>
            set({ req_radarr_root_folder: e.target.value || null })
          }
        />
        <Input
          aria-label="Radarr quality profile id"
          type="number"
          min={1}
          placeholder="Quality profile id (leave blank to keep the global)"
          value={input.req_radarr_quality_profile_id ?? ""}
          onChange={(e) =>
            set({
              req_radarr_quality_profile_id: e.target.value
                ? Number(e.target.value)
                : null,
            })
          }
        />
      </Field>

      <Field
        label="Where shows from this row land"
        labelFor="row-req-sonarr-folder"
        description="The Sonarr root folder and quality profile for shows this row asks for."
        ariaLabel="Use the global Sonarr folder for this row"
        inheriting={
          input.req_sonarr_root_folder === null &&
          input.req_sonarr_quality_profile_id === null
        }
        globalValue={requestRootFolderGlobal(settings, "sonarr")}
        onToggle={(on) =>
          set(
            on
              ? {
                  req_sonarr_root_folder: null,
                  req_sonarr_quality_profile_id: null,
                }
              : { req_sonarr_root_folder: "" },
          )
        }
      >
        <Input
          id="row-req-sonarr-folder"
          placeholder="/data/Kids TV"
          value={input.req_sonarr_root_folder ?? ""}
          onChange={(e) =>
            set({ req_sonarr_root_folder: e.target.value || null })
          }
        />
        <Input
          aria-label="Sonarr quality profile id"
          type="number"
          min={1}
          placeholder="Quality profile id (leave blank to keep the global)"
          value={input.req_sonarr_quality_profile_id ?? ""}
          onChange={(e) =>
            set({
              req_sonarr_quality_profile_id: e.target.value
                ? Number(e.target.value)
                : null,
            })
          }
        />
      </Field>

      <Field
        label="How much of a show this row grabs"
        labelFor="row-req-sonarr-monitor"
        description="Sonarr downloads what it monitors, so a long-running show normally arrives whole. A row that's meant as a taster can take the first season and no more."
        ariaLabel="Use the global amount-of-a-show setting for this row"
        inheriting={input.req_sonarr_monitor === null}
        globalValue={requestSonarrMonitorGlobal(settings)}
        onToggle={(on) =>
          set({ req_sonarr_monitor: on ? null : "firstSeason" })
        }
      >
        <select
          id="row-req-sonarr-monitor"
          className="h-9 w-full rounded-md border bg-elevated px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          value={input.req_sonarr_monitor ?? "all"}
          onChange={(e) =>
            set({ req_sonarr_monitor: asSonarrMonitor(e.target.value) })
          }
        >
          {SONARR_MONITOR_MODES.map((mode) => (
            <option key={mode} value={mode}>
              {SONARR_MONITOR_LABELS[mode]}
            </option>
          ))}
        </select>
        <p className="text-sm text-muted-foreground">
          {SONARR_MONITOR_HINTS[input.req_sonarr_monitor ?? "all"]}
        </p>
      </Field>
    </div>
  );
}
