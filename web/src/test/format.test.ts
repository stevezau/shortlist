import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildLabel,
  cronFromTime,
  formatDuration,
  formatHitRate,
  isPresetCron,
  renderRowName,
  runElapsedMs,
  settingBool,
  settingNumber,
  settingString,
  formatDate,
  timeAgo,
  timeFromCron,
  weekStarting,
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

describe("weekStarting", () => {
  // Every expectation below was taken from SQLite itself, which is what cuts these buckets:
  //   select strftime('%Y-%W', '2026-07-13')  ->  2026-28
  // Asserted as day + month separately rather than as one string: `toLocaleDateString` orders them
  // by the runner's locale ("13 Jul" here, "Jul 13" under en-US in CI), and the ORDER is not the
  // thing under test.
  const on = (week: string) => weekStarting(week);

  it("names the Monday that starts a week bucket", () => {
    // 2026's first Monday is 5 Jan (1 Jan is a Thursday), so week 01 starts there.
    expect(on("2026-01")).toMatch(/\b5\b/);
    expect(on("2026-01")).toMatch(/Jan/);
    expect(on("2026-28")).toMatch(/\b13\b/);
    expect(on("2026-28")).toMatch(/Jul/);
    expect(on("2026-32")).toMatch(/\b10\b/);
    expect(on("2026-32")).toMatch(/Aug/);
  });

  it("counts from each year's own first Monday, not a fixed offset", () => {
    // 2025 starts on a Wednesday, so its first Monday is 6 Jan — a different offset to 2026's.
    expect(on("2025-01")).toMatch(/\b6\b/);
    expect(on("2025-01")).toMatch(/Jan/);
  });

  it("treats week 00 as the part-week before the first Monday", () => {
    expect(on("2026-00")).toMatch(/\b1\b/);
    expect(on("2026-00")).toMatch(/Jan/);
  });

  it("returns an unparseable bucket verbatim rather than guessing", () => {
    expect(on("")).toBe("");
    expect(on("not-a-week")).toBe("not-a-week");
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

describe("buildLabel", () => {
  it("names the commit and drops the version on a pre-release build", () => {
    // `current_version` is the last RELEASED version, so on :dev it names the release this build
    // came after — printing it claims a version the running code is not. The commit is the identity
    // there anyway: every push between two releases reports the same version number.
    expect(
      buildLabel({
        current_version: "1.4.0",
        git_branch: "dev",
        git_sha: "2ee14f8c43954588eb720d4b0d1fab4fa50f7013",
      }),
    ).toBe("Shortlist · dev · 2ee14f8");
  });

  it("shows the version alone on a release build", () => {
    // CI passes the TAG as the branch on a release, and there the tag IS the version — so the sha
    // would be noise rather than information.
    expect(
      buildLabel({
        current_version: "1.4.1",
        git_branch: "v1.4.1",
        git_sha: "abcdef1234567890",
      }),
    ).toBe("Shortlist · 1.4.1");
  });

  it("falls back cleanly on a source checkout", () => {
    // A checkout has no build args at all. Empty strings must drop out entirely rather than render
    // as trailing separators against nothing.
    expect(
      buildLabel({ current_version: "1.4.0", git_branch: "", git_sha: "" }),
    ).toBe("Shortlist · 1.4.0");
    expect(buildLabel(undefined)).toBe("Shortlist");
  });
});


describe("formatDate", () => {
  // Nothing imported this function. Every mutation to it survived — both sentinels, the year and
  // month formats, and inverting `dateOnly`, which is the whole reason the option exists.
  const ISO = "2026-08-23T16:30:00Z";

  it("says nothing rather than something wrong when there is no date", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate("")).toBe("—");
  });

  it("says nothing rather than 'Invalid Date' on a string it cannot parse", () => {
    expect(formatDate("not-a-date")).toBe("—");
  });

  it("gives a full year and a short month, not 26 and not August", () => {
    const out = formatDate(ISO);
    expect(out).toMatch(/2026/);
    expect(out).not.toMatch(/\b26\b/);
    expect(out).toMatch(/Aug/);
    expect(out).not.toMatch(/August/);
  });

  it("drops the time on dateOnly, and keeps it otherwise", () => {
    // The distinction the option exists for: "around 23 Aug 2026" reads as an estimate, while
    // "23 Aug 2026, 16:30" reads as a deadline. Inverting the flag swapped the two silently.
    expect(formatDate(ISO, { dateOnly: true })).not.toMatch(/\d{1,2}:\d{2}/);
    expect(formatDate(ISO)).toMatch(/\d{1,2}:\d{2}/);
  });
});

describe("timeAgo — the bucket boundaries", () => {
  // Every boundary survived being moved by one: the fixtures used 30s / 30m / 6h / 2d, none of which
  // sits on an edge, so "exactly 60 seconds" could read "just now" and "exactly 24h" could read
  // "24h ago".
  const at = (secondsAgo: number) =>
    timeAgo(new Date(Date.UTC(2026, 0, 2, 0, 0, 0) - secondsAgo * 1000).toISOString(), Date.UTC(2026, 0, 2));

  it("flips from 'just now' to minutes at exactly one minute", () => {
    expect(at(59)).toBe("just now");
    expect(at(60)).toBe("1m ago");
  });

  it("flips from minutes to hours at exactly one hour", () => {
    expect(at(59 * 60)).toBe("59m ago");
    expect(at(60 * 60)).toBe("1h ago");
  });

  it("flips from hours to days at exactly one day", () => {
    expect(at(23 * 3600)).toBe("23h ago");
    expect(at(24 * 3600)).toBe("1d ago");
  });
});
