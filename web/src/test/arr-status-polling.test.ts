import { describe, expect, it } from "vitest";

import { arrStatusInterval, queryKeys } from "@/lib/queries";

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
describe("arr status polling", () => {
  it("polls while a title is still moving", () => {
    expect(arrStatusInterval({ "1": "downloading" })).toBe(10_000);
    expect(arrStatusInterval({ "1": "downloaded", "2": "queued" })).toBe(10_000);
  });

  it("stops once everything has settled", () => {
    // The load rule. `downloaded` and `unmonitored` are terminal — nothing about them will change
    // on its own, so re-reading both Arr libraries on a timer buys nothing.
    expect(arrStatusInterval({ "1": "downloaded" })).toBe(false);
    expect(arrStatusInterval({ "1": "unmonitored", "2": "downloaded" })).toBe(false);
  });

  it("stops when nothing is tracked at all", () => {
    // The commonest inbox: titles waiting for approval, none of them in an Arr yet. Sending one
    // invalidates the key outright, and returning to the tab refetches on focus — neither needs a
    // timer running in the meantime.
    expect(arrStatusInterval({})).toBe(false);
    expect(arrStatusInterval({ "1": null })).toBe(false);
  });

  it("keeps the send mutation and the badge on the same cache key", () => {
    // If these ever drift, sending a title stops refreshing its badge and the missing timer becomes
    // a real gap rather than a saving.
    expect(queryKeys.arrStatus).toEqual(["arrStatus"]);
  });
});
