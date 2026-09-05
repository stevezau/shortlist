import { describe, expect, it } from "vitest";

import { CORE_CATEGORIES, deriveHealthChips } from "@/lib/health";
import type { AppNotification } from "@/lib/types";

function alert(patch: Partial<AppNotification> = {}): AppNotification {
  return {
    id: "run-failed-42",
    severity: "error",
    title: "The last run failed",
    body: "Something went wrong.",
    action_url: "/runs/42",
    action_label: "See the run",
    dismissable: true,
    ...patch,
  };
}

function chip(chips: ReturnType<typeof deriveHealthChips>, category: string) {
  return chips.find((c) => c.category === category);
}

describe("deriveHealthChips", () => {
  it("reports every category as ok when nothing is firing", () => {
    const chips = deriveHealthChips([]);

    expect(chips).toHaveLength(CORE_CATEGORIES.length);
    expect(chips.every((c) => c.state === "ok")).toBe(true);
    expect(chips.every((c) => c.detail === undefined)).toBe(true);
    expect(chips.map((c) => c.label)).toEqual(
      CORE_CATEGORIES.map((c) => c.label),
    );
  });

  // Every id `notifications.py` can emit into a chip, and where it belongs. One row per builder,
  // because a typo in any single prefix files that alert under "Other" and the strip then points at
  // the wrong area — which is worse than not pointing at all.
  //
  // `rows-unnamed-` is `info` today and so cannot reach a chip; it is mapped (and asserted here with
  // a warning severity) so that raising its severity later lands it on the Rows chip, not "Other".
  it.each([
    ["run-failed-12", "runs"],
    ["run-partial-12", "runs"],
    ["runs-paused", "runs"],
    ["recent-errors-88", "runs"],
    ["unhideable-rows-12", "privacy"],
    ["filters-not-enforced-12", "privacy"],
    ["rows-unnamed-sarah,mike", "rows"],
    ["shelf-contention-2026-09-05", "rows"],
    ["mdblist-quota-2026-09-05", "requests"],
    ["requests-none-qualified-2026-09-05", "requests"],
    ["failed-jobs-7", "jobs"],
    ["playback-listener-down", "watch"],
  ])("files %s under the %s chip", (id, category) => {
    const chips = deriveHealthChips([alert({ id, severity: "warning" })]);

    expect(chip(chips, category)?.state).toBe("warning");
    expect(chip(chips, "other")).toBeUndefined();
    // Nothing else lights up: one alert must not redden the whole strip.
    expect(chips.filter((c) => c.state !== "ok")).toHaveLength(1);
  });

  it("keeps the worst alert in a category, whichever order they arrive in", () => {
    const warningFirst = deriveHealthChips([
      alert({ id: "run-partial-9", severity: "warning", title: "Partial" }),
      alert({ id: "run-failed-9", severity: "error", title: "Failed" }),
    ]);
    const errorFirst = deriveHealthChips([
      alert({ id: "run-failed-9", severity: "error", title: "Failed" }),
      alert({ id: "run-partial-9", severity: "warning", title: "Partial" }),
    ]);

    expect(chip(warningFirst, "runs")?.state).toBe("error");
    expect(chip(warningFirst, "runs")?.detail).toBe("Failed");
    expect(chip(errorFirst, "runs")?.state).toBe("error");
    expect(chip(errorFirst, "runs")?.detail).toBe("Failed");
  });

  it("ignores an info alert about the owner seeing every row", () => {
    // True by design on nearly every multi-user install. Amber for it would train the owner to
    // ignore the one chip that matters.
    const chips = deriveHealthChips([
      alert({ id: "owner-sees-all-rows", severity: "info" }),
    ]);

    expect(chip(chips, "privacy")?.state).toBe("ok");
    expect(chips).toHaveLength(CORE_CATEGORIES.length);
  });

  it("ignores an available update entirely, rather than inventing a chip for it", () => {
    const chips = deriveHealthChips([
      alert({
        id: "update-1.9.0",
        severity: "info",
        action_url: "https://github.com/stevezau/shortlist/releases",
      }),
    ]);

    expect(chips).toHaveLength(CORE_CATEGORIES.length);
    expect(chip(chips, "other")).toBeUndefined();
  });

  it("surfaces an id it does not recognise instead of dropping it", () => {
    // A 14th builder nobody teaches to this file must still reach the owner. `secrets-we-cannot-read`
    // is exactly that case today, and it is deliberately unmapped.
    const chips = deriveHealthChips([
      alert({
        id: "secrets-we-cannot-read",
        title: "Some saved credentials can no longer be read",
        action_url: "/settings",
      }),
    ]);

    expect(chip(chips, "other")?.state).toBe("error");
    expect(chip(chips, "other")?.detail).toBe(
      "Some saved credentials can no longer be read",
    );
    expect(chip(chips, "other")?.href).toBe("/settings");
  });

  it("shows only the worst unrecognised alert, not one chip each", () => {
    const chips = deriveHealthChips([
      alert({ id: "mystery-warning", severity: "warning", title: "Odd" }),
      alert({ id: "mystery-error", severity: "error", title: "Bad" }),
    ]);

    expect(chips.filter((c) => c.category === "other")).toHaveLength(1);
    expect(chip(chips, "other")?.detail).toBe("Bad");
  });

  it("takes the owner to the alert's own destination, and the category's when it is healthy", () => {
    const chips = deriveHealthChips([
      alert({ id: "run-failed-42", action_url: "/runs/42" }),
    ]);

    expect(chip(chips, "runs")?.href).toBe("/runs/42");
    expect(chip(chips, "jobs")?.href).toBe("/jobs");
  });

  it("never routes a chip at an external URL", () => {
    // `action_url` is a release page on some builders. A router `Link` would resolve that relative
    // to the app and produce a dead in-app path.
    const chips = deriveHealthChips([
      alert({
        id: "failed-jobs-7",
        action_url: "https://example.com/whatever",
      }),
    ]);

    expect(chip(chips, "jobs")?.href).toBe("/jobs");
    expect(chip(chips, "jobs")?.state).toBe("error");
  });
});
