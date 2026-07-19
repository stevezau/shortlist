import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cronFromTime,
  formatDuration,
  formatHitRate,
  isPresetCron,
  renderRowName,
  runElapsedMs,
  settingBool,
  settingNumber,
  settingString,
  timeAgo,
  timeFromCron,
} from "@/lib/format";

describe("cronFromTime / timeFromCron", () => {
  it("round-trips a nightly time through cron and back", () => {
    const cron = cronFromTime("03:30", false);
    expect(cron).toBe("30 3 * * *");
    expect(timeFromCron(cron)).toEqual({ time: "03:30", weekly: false });
  });

  it("round-trips a weekly time, marking the weekly flag", () => {
    const cron = cronFromTime("22:05", true);
    expect(cron).toBe("5 22 * * 0");
    expect(timeFromCron(cron)).toEqual({ time: "22:05", weekly: true });
  });

  it("clamps an out-of-range or malformed time to the 03:30 default", () => {
    // Bad hours/minutes fall back per-field to 3 / 30.
    expect(cronFromTime("99:99")).toBe("30 3 * * *");
    expect(cronFromTime("not-a-time")).toBe("30 3 * * *");
  });

  it("recognises only the crons the presets truly round-trip (nightly + Sunday-weekly)", () => {
    expect(isPresetCron("30 3 * * *")).toBe(true); // nightly
    expect(isPresetCron("5 22 * * 0")).toBe(true); // weekly (Sunday — the only weekday presets emit)
  });

  it("treats anything the presets would flatten as a custom cron", () => {
    // A non-Sunday weekday can't round-trip: the presets only ever write dow 0, and timeFromCron
    // would relabel this as "weekly Sunday" and overwrite it — so it must stay Custom.
    expect(isPresetCron("0 4 * * 1")).toBe(false); // Mondays
    expect(isPresetCron("0 4 * * 6")).toBe(false); // Saturdays
    expect(isPresetCron("0 */6 * * *")).toBe(false); // step hours
    expect(isPresetCron("0 4 * * 1,3,5")).toBe(false); // day-of-week list
    expect(isPresetCron("0 4 1 * *")).toBe(false); // specific day of month
    expect(isPresetCron("0 4 * 6 *")).toBe(false); // specific month
    expect(isPresetCron("30 3 * *")).toBe(false); // too few fields
    expect(isPresetCron("")).toBe(false);
  });

  it("treats an empty string's hour as 0 (Number('') === 0), minute as the default", () => {
    // A JS gotcha worth pinning: "" splits to [""] so the hour parses to 0 (valid), while the
    // absent minute is NaN and falls back to 30.
    expect(cronFromTime("")).toBe("30 0 * * *");
  });

  it("keeps a valid hour when only the minute is malformed", () => {
    expect(cronFromTime("07:zz")).toBe("30 7 * * *");
  });

  it("falls back to 03:30 nightly for a cron it cannot parse", () => {
    expect(timeFromCron("garbage")).toEqual({ time: "03:30", weekly: false });
    expect(timeFromCron("60 25 * * *")).toEqual({
      time: "03:30",
      weekly: false,
    });
    expect(timeFromCron("30 3 * *")).toEqual({ time: "03:30", weekly: false });
  });
});

describe("timeAgo", () => {
  afterEach(() => vi.useRealTimers());

  function atNow(now: string) {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(now));
  }

  it("returns 'never' for null and 'unknown' for an unparseable value", () => {
    expect(timeAgo(null)).toBe("never");
    expect(timeAgo("not-a-date")).toBe("unknown");
  });

  it("buckets recent times into just now / minutes / hours / days", () => {
    atNow("2026-07-14T12:00:00Z");
    expect(timeAgo("2026-07-14T11:59:30Z")).toBe("just now");
    expect(timeAgo("2026-07-14T11:30:00Z")).toBe("30m ago");
    expect(timeAgo("2026-07-14T06:00:00Z")).toBe("6h ago");
    expect(timeAgo("2026-07-12T12:00:00Z")).toBe("2d ago");
  });

  it("floors a future timestamp to 'just now' rather than a negative delta", () => {
    atNow("2026-07-14T12:00:00Z");
    expect(timeAgo("2026-07-14T12:05:00Z")).toBe("just now");
  });
});

describe("setting narrowers", () => {
  const settings = {
    str: "hello",
    num: 15,
    bool: true,
    notFinite: Number.NaN,
    wrong: ["array"],
  };

  it("settingString returns strings and falls back otherwise", () => {
    expect(settingString(settings, "str")).toBe("hello");
    expect(settingString(settings, "num")).toBe("");
    expect(settingString(settings, "missing", "fallback")).toBe("fallback");
  });

  it("settingNumber returns finite numbers and falls back otherwise", () => {
    expect(settingNumber(settings, "num", 0)).toBe(15);
    expect(settingNumber(settings, "notFinite", 7)).toBe(7);
    expect(settingNumber(settings, "str", 7)).toBe(7);
    expect(settingNumber(settings, "missing", 3)).toBe(3);
  });

  it("settingBool returns booleans and falls back otherwise", () => {
    expect(settingBool(settings, "bool")).toBe(true);
    expect(settingBool(settings, "str")).toBe(false);
    expect(settingBool(settings, "missing", true)).toBe(true);
  });
});

describe("small formatters", () => {
  it("formatHitRate renders a percent or an em dash before first measurement", () => {
    expect(formatHitRate(null)).toBe("—");
    expect(formatHitRate(0.314)).toBe("31%");
    expect(formatHitRate(1)).toBe("100%");
  });

  it("runElapsedMs measures finished − started, and is null while running or reversed", () => {
    const start = "2026-07-19T03:30:00Z";
    expect(runElapsedMs(start, "2026-07-19T03:52:30Z")).toBe(22.5 * 60 * 1000);
    expect(runElapsedMs(start, null)).toBeNull(); // still running
    expect(runElapsedMs(start, "2026-07-19T03:29:00Z")).toBeNull(); // clock skew / bad data
    expect(runElapsedMs(start, "not-a-date")).toBeNull();
  });

  it("formatDuration reads in ms, seconds, then minutes+seconds", () => {
    expect(formatDuration(450)).toBe("450ms");
    expect(formatDuration(2500)).toBe("2.5s");
    expect(formatDuration(22.5 * 60 * 1000)).toBe("22m 30s");
  });

  it("renderRowName substitutes every {top_seed}", () => {
    expect(renderRowName("Because you watched {top_seed}", "Fargo")).toBe(
      "Because you watched Fargo",
    );
    expect(renderRowName("✨ Picked for You")).toBe("✨ Picked for You");
  });

  it("renderRowName also fills {user} with a sample name for the preview", () => {
    expect(renderRowName("✨ Picked for {user}")).toBe("✨ Picked for Sarah");
    expect(renderRowName("{user}: because you watched {top_seed}")).toBe(
      "Sarah: because you watched Fargo",
    );
  });

  it("renderRowName fills {library_name} with a sample library and collapses an empty one", () => {
    expect(renderRowName("✨ {library_name} Picked for You")).toBe(
      "✨ Movies Picked for You",
    );
    expect(
      renderRowName(
        "✨ {library_name} Picked for You",
        "Fargo",
        "Sarah",
        "TV Shows",
      ),
    ).toBe("✨ TV Shows Picked for You");
    // An empty library collapses the gap so the preview never shows a double space.
    expect(
      renderRowName("✨ {library_name} Picked for You", "Fargo", "Sarah", ""),
    ).toBe("✨ Picked for You");
  });
});
