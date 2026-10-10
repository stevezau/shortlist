import { REDACTED } from "@/components/ui/secret-input";
import { settingString } from "@/lib/format";
import type { Settings } from "@/lib/types";

/** Where a request is filed, and whether Radarr/Sonarr are set up enough to use. */
type RequestReadiness = {
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
