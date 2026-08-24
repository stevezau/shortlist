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
});
