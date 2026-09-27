import { REDACTED } from "@/components/ui/secret-input";
import { settingString } from "@/lib/format";
import type { CollectionInput, Settings } from "@/lib/types";

/** Where a request is filed, and whether Radarr/Sonarr are set up enough to use. */
export type RequestReadiness = {
  target: "arr" | "overseerr";
  radarrReady: boolean;
  sonarrReady: boolean;
};

/**
 * Derives request readiness from the saved settings, for the row editor to decide which per-row
 * Radarr/Sonarr controls can do anything.
 *
 * "Ready" mirrors the "configured" check Settings › Requests already uses
 * (`requests-settings.tsx:560-565`): a URL on file and a key that's actually SAVED (redacted, not a
 * value someone is mid-typing) — the server needs both to reach the app.
 */
export function requestReadiness(settings: Settings | undefined): RequestReadiness {
  const s = settings ?? {};
  return {
    target:
      settingString(s, "requests.target", "arr") === "overseerr"
        ? "overseerr"
        : "arr",
    radarrReady:
      Boolean(settingString(s, "requests.radarr.url")) &&
      settingString(s, "requests.radarr.apikey") === REDACTED,
    sonarrReady:
      Boolean(settingString(s, "requests.sonarr.url")) &&
      settingString(s, "requests.sonarr.apikey") === REDACTED,
  };
}

const INHERITED = " (global default)";

/**
 * What this row will ask for, in one line: its limit, whether it sends or waits, and its tag.
 *
 * The row editor's folded Requests group and the "What this row will do" panel both say this, so it
 * is worded once here and the two cannot disagree.
 *
 * @param shared A shared row: built from titles people already watched, it can never ask for one.
 */
export function requestsSummary(
  input: Pick<CollectionInput, "req_max_per_row" | "req_auto_send" | "request_tag">,
  settings: Settings | undefined,
  { shared }: { shared: boolean },
): string {
  if (shared) return "None — shared rows never ask for missing titles";
  if (!settings) return "Following Settings › Requests";
  if (settings["requests.enabled"] !== true) return "None — requests are off in Settings";
  if (input.req_max_per_row === 0) {
    return "Never on its own — its picks wait in Requests for you to approve";
  }

  const { target } = requestReadiness(settings);
  const parts: string[] = [];

  if (input.req_max_per_row !== null) {
    parts.push(`Up to ${input.req_max_per_row} a run`);
  } else {
    // Rows share the run's cap, so an inheriting row gets a share of it, never the whole number.
    const cap = settings["requests.max_per_run"];
    parts.push(`Its share of the run's ${typeof cap === "number" ? cap : "limit"}${INHERITED}`);
  }

  const globalAutoSend = settings["requests.auto_send"];
  const autoSend = input.req_auto_send ?? (typeof globalAutoSend === "boolean" ? globalAutoSend : null);
  if (autoSend !== null) {
    const destination = target === "overseerr" ? "Overseerr" : "Radarr/Sonarr";
    parts.push(
      (autoSend ? `sent to ${destination} automatically` : "held in Requests for your approval") +
        (input.req_auto_send === null ? INHERITED : ""),
    );
  }

  // Overseerr/Seerr files its own requests and ignores the tag (`requests.py`).
  const tag = input.request_tag.trim();
  if (tag && target !== "overseerr") parts.push(`tagged “${tag}”`);

  return parts.join(", ");
}
