import { describe, expect, it } from "vitest";

import { REDACTED } from "@/components/ui/secret-input";
import { requestReadiness } from "@/lib/requests";
import type { Settings } from "@/lib/types";

function settings(overrides: Record<string, unknown>): Settings {
  return overrides as unknown as Settings;
}

describe("requestReadiness", () => {
  it("defaults to arr with nothing set up when settings haven't loaded", () => {
    expect(requestReadiness(undefined)).toEqual({
      target: "arr",
      radarrReady: false,
      sonarrReady: false,
    });
  });

  it("reads overseerr as the target only when it's exactly that value", () => {
    expect(requestReadiness(settings({ "requests.target": "overseerr" })).target).toBe(
      "overseerr",
    );
    expect(requestReadiness(settings({ "requests.target": "arr" })).target).toBe("arr");
  });

  it("is not ready with a URL but no saved key", () => {
    expect(
      requestReadiness(
        settings({
          "requests.radarr.url": "http://radarr.local",
          "requests.radarr.apikey": "just-typed-not-saved",
        }),
      ).radarrReady,
    ).toBe(false);
  });

  it("is not ready with a saved key but no URL", () => {
    expect(
      requestReadiness(settings({ "requests.sonarr.apikey": REDACTED })).sonarrReady,
    ).toBe(false);
  });

  it("is ready with both a URL and a saved (redacted) key", () => {
    const readiness = requestReadiness(
      settings({
        "requests.radarr.url": "http://radarr.local",
        "requests.radarr.apikey": REDACTED,
        "requests.sonarr.url": "http://sonarr.local",
        "requests.sonarr.apikey": REDACTED,
      }),
    );
    expect(readiness.radarrReady).toBe(true);
    expect(readiness.sonarrReady).toBe(true);
  });
});
