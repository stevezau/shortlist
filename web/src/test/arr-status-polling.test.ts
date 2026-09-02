import { describe, expect, it } from "vitest";

import { arrStatusInterval, queryKeys } from "@/lib/queries";
import type { ArrStatus } from "@/lib/types";

/**
 * The polling rule for the Requests inbox's Arr badge, pinned.
 *
 * One fetch of this endpoint is a WHOLE-LIBRARY read from each Arr — `RadarrClient.status_by_tmdb`
 * pulls `/api/v3/movie` entire. That is the right shape for asking about a whole inbox at once and
 * the wrong thing to put on a forever-timer: an inbox where everything has already downloaded has
 * no reason to re-read the library every half minute for as long as a tab is open.
 *
 * The exported rule itself is asserted — not a copy of it — so a change to the real hook fails
 * here rather than passing against a duplicate.
 */
function state(patch: Partial<ArrStatus> = {}): ArrStatus {
  return { statuses: {}, radarr: "ok", sonarr: "ok", ...patch } as ArrStatus;
}

describe("arr status polling", () => {
  it("polls fast while a title is actually mid-transfer", () => {
    expect(arrStatusInterval(state({ statuses: { "1": "downloading" } }))).toBe(
      10_000,
    );
  });

  it("does NOT poll for a queued title, which can sit there for months", () => {
    // `queued` reads like a transient and is not: `_status_for` returns it for "monitored, nothing
    // on disk, nothing in the queue" — the resting state of a title that is unreleased or simply
    // unfindable. Treating it as in-flight held a 10s whole-library poll open indefinitely.
    expect(arrStatusInterval(state({ statuses: { "1": "queued" } }))).toBe(
      false,
    );
    expect(
      arrStatusInterval(
        state({ statuses: { "1": "queued", "2": "downloaded" } }),
      ),
    ).toBe(false);
  });

  it("keeps checking an app it could not reach, so the badge clears itself", () => {
    // The one case that cannot recover on its own: a failed lookup returns NO statuses, so keying
    // the timer off the titles left "Can't reach Radarr" on screen until the operator happened to
    // refocus the tab.
    expect(arrStatusInterval(state({ radarr: "unreachable" }))).toBe(30_000);
    expect(arrStatusInterval(state({ sonarr: "unreachable" }))).toBe(30_000);
  });

  it("stops once everything has settled", () => {
    expect(arrStatusInterval(state({ statuses: { "1": "downloaded" } }))).toBe(
      false,
    );
    expect(arrStatusInterval(state({ statuses: { "1": "unmonitored" } }))).toBe(
      false,
    );
    expect(arrStatusInterval(state({ radarr: "off", sonarr: "off" }))).toBe(
      false,
    );
  });

  it("does not poll before the first answer arrives", () => {
    expect(arrStatusInterval(undefined)).toBe(false);
  });

  it("keeps the send mutation and the badge on the same cache key", () => {
    // If these drift, sending a title stops refreshing its badge and the absent timer becomes a
    // real gap rather than a saving.
    expect(queryKeys.arrStatus).toEqual(["arrStatus"]);
  });

  describe("on the Overseerr route", () => {
    it("never takes the fast pace, however many titles read as downloading", () => {
      // Overseerr's enum has no "downloading right now": PROCESSING is the resting state of an
      // approved-but-unreleased film and PARTIALLY_AVAILABLE that of every airing series, so this
      // would poll every 10s for ever — and one poll walks the whole library, not one endpoint.
      expect(
        arrStatusInterval({
          statuses: { "1": "downloading", "2": "downloading" },
          radarr: "off",
          sonarr: "off",
          overseerr: "ok",
        }),
      ).toBe(false);
    });

    it("still earns the recovery timer when it cannot be reached", () => {
      // The one state that cannot clear itself: a failed lookup returns no statuses at all.
      expect(
        arrStatusInterval({
          statuses: {},
          radarr: "off",
          sonarr: "off",
          overseerr: "unreachable",
        }),
      ).toBe(30_000);
    });

    it("leaves the Arr route's fast pace alone when the field is absent", () => {
      // Cast deliberately: the generated type makes `overseerr` required, so this shape cannot come
      // from the current API — but a browser holding a cached response from before the field
      // existed still produces it at runtime. Reading "is Overseerr in use?" as `!== "off"` made
      // `undefined` mean YES, which would silently stop the fast poll on every Arr install. The
      // same misreading shipped once already in requests.tsx, where it blanked the badges.
      const legacy = { statuses: { "1": "downloading" }, radarr: "ok", sonarr: "ok" };
      expect(arrStatusInterval(legacy as unknown as ArrStatus)).toBe(10_000);
    });
  });
});
