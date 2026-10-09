import { describe, expect, it } from "vitest";

import { plainTestMessage } from "@/lib/plain-test-error";

const fail = (message: string) => ({ ok: false, message });

describe("plainTestMessage", () => {
  it("says nothing answered at the address, and what to do", () => {
    expect(plainTestMessage(fail("ConnectError: All connection attempts failed"), "Radarr", "http://radarr:7878")).toBe(
      "Can’t reach Radarr at http://radarr:7878 — check the address, then Test",
    );
  });

  it("says a refused key, and a slow answer", () => {
    expect(plainTestMessage(fail("HTTPStatusError: 401 Unauthorized"), "Sonarr")).toBe(
      "Sonarr refused the key — check it, then Test",
    );
    expect(plainTestMessage(fail("ReadTimeout: timed out"), "Plex")).toBe(
      "Plex did not answer in time — check the address, then Test",
    );
  });

  it("leaves a success and an unfamiliar failure exactly as the server wrote them", () => {
    expect(plainTestMessage({ ok: true, message: "TMDB key works" }, "TMDB")).toBe("TMDB key works");
    expect(plainTestMessage(fail("ValueError: weird"), "TMDB")).toBe("ValueError: weird");
  });
});
