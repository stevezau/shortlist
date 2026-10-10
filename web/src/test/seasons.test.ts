import { describe, expect, it } from "vitest";

import {
  addDays,
  isNightly,
  ruleLabel,
  seasonDate,
  seasonOverlaps,
  seasonStatusLine,
  seasonTiming,
  seasonWindowLabel,
  seasonWindows,
  timingLabel,
} from "@/lib/seasons";

import { CHRISTMAS, FEBRUARY_SPOTLIGHT, HALLOWEEN, THANKSGIVING, VALENTINES } from "./season-fixtures";

describe("seasonWindowLabel", () => {
  it("runs from its lead through its next day", () => {
    expect(seasonWindowLabel(HALLOWEEN, 30, 0)).toBe(
      `${seasonDate("2026-10-01")} – ${seasonDate("2026-10-31")}`,
    );
  });

  it("stays up for the days after", () => {
    expect(seasonWindowLabel(CHRISTMAS, 0, 1)).toBe(
      `${seasonDate("2026-12-25")} – ${seasonDate("2026-12-26")}`,
    );
  });

  it("starts in December when a long lead crosses New Year", () => {
    expect(seasonWindowLabel(VALENTINES, 60, 0)).toBe(
      `${seasonDate("2026-12-16")} – ${seasonDate("2027-02-14")}`,
    );
  });

  it("follows the server's next date, so a moving holiday is never worked out here", () => {
    expect(seasonWindowLabel(THANKSGIVING, 14, 0)).toBe(
      `${seasonDate("2026-11-12")} – ${seasonDate("2026-11-26")}`,
    );
  });

  it("uses the server's full-month span without adding the row's built-in timing", () => {
    expect(seasonWindowLabel(FEBRUARY_SPOTLIGHT, 30, 2)).toBe(
      `${seasonDate("2027-02-01")} – ${seasonDate("2027-02-28")}`,
    );
    expect(seasonWindows(FEBRUARY_SPOTLIGHT, 30, 2)).toEqual([
      { start: "2027-02-01", end: "2027-02-28" },
      { start: "2028-02-01", end: "2028-02-29" },
    ]);
  });
});

describe("seasonTiming", () => {
  it("gives a built-in the row's timing", () => {
    expect(seasonTiming(HALLOWEEN, 30, 2)).toEqual({ lead: 30, after: 2 });
  });

  it("gives a season of the owner's its own", () => {
    expect(seasonTiming(THANKSGIVING, 30, 2)).toEqual({ lead: 14, after: 0 });
  });
});

describe("timingLabel", () => {
  it.each([
    [30, 0, "from 30 days before"],
    [1, 0, "from 1 day before"],
    [7, 1, "from 7 days before to 1 day after"],
    [0, 3, "the day and 3 days after"],
    [0, 0, "on the day only"],
  ])("lead %i, after %i reads %s", (lead, after, text) => {
    expect(timingLabel(lead, after)).toBe(text);
  });
});

describe("ruleLabel", () => {
  it.each([
    [{ kind: "fixed", month: 3, day: 17, nth: 1, weekday: 0, offset: 0 }, "17 March"],
    [{ kind: "month", month: 2, day: 1, nth: 1, weekday: 0, offset: 0 }, "All of February"],
    [{ kind: "nth", month: 11, day: 1, nth: 4, weekday: 3, offset: 0 }, "4th Thursday of November"],
    [{ kind: "nth", month: 5, day: 1, nth: -1, weekday: 0, offset: 0 }, "Last Monday of May"],
    [{ kind: "easter", month: 1, day: 1, nth: 1, weekday: 0, offset: 0 }, "Easter Sunday"],
    [{ kind: "easter", month: 1, day: 1, nth: 1, weekday: 0, offset: -21 }, "21 days before Easter"],
    [{ kind: "easter", month: 1, day: 1, nth: 1, weekday: 0, offset: 1 }, "1 day after Easter"],
  ] as const)("names %o as the server does", (rule, label) => {
    expect(ruleLabel(rule)).toBe(label);
  });
});

describe("addDays", () => {
  it("crosses a month and a year", () => {
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
  });
});

describe("seasonOverlaps", () => {
  it("names two seasons whose windows share days, and the days", () => {
    expect(seasonOverlaps([THANKSGIVING, CHRISTMAS], 30, 0)).toEqual([
      { first: THANKSGIVING, second: CHRISTMAS, start: "2026-11-25", end: "2026-11-26" },
    ]);
  });

  it("finds an overlap a year out, when today sits between the two", () => {
    // Today is after Christmas's window has started but Thanksgiving's has passed: their NEXT windows
    // are a year apart, but they still overlap every year.
    const thanksgivingNextYear = { ...THANKSGIVING, next_dates: ["2027-11-25", "2028-11-23"] };
    const christmasNow = { ...CHRISTMAS, next_dates: ["2026-12-25", "2027-12-25"] };
    expect(seasonOverlaps([thanksgivingNextYear, christmasNow], 30, 0)).toEqual([
      { first: thanksgivingNextYear, second: christmasNow, start: "2027-11-25", end: "2027-11-25" },
    ]);
  });

  it("says nothing when no windows meet", () => {
    expect(seasonOverlaps([HALLOWEEN, CHRISTMAS], 30, 0)).toEqual([]);
  });

  it("compares a whole-month span with a holiday window inside that month", () => {
    expect(seasonOverlaps([FEBRUARY_SPOTLIGHT, VALENTINES], 7, 0)).toEqual([
      { first: FEBRUARY_SPOTLIGHT, second: VALENTINES, start: "2027-02-07", end: "2027-02-14" },
    ]);
  });
});

describe("seasonStatusLine", () => {
  const halloween = {
    slug: "halloween",
    name: "Halloween",
    emoji: "🎃",
    starts: "2026-10-01",
    ends: "2026-10-31",
  };
  const christmas = {
    slug: "christmas",
    name: "Christmas",
    emoji: "🎄",
    starts: "2026-11-25",
    ends: "2026-12-25",
  };

  it("names the season on screen and when it comes down", () => {
    expect(seasonStatusLine({ showing: halloween, next: christmas })).toBe(
      `Showing 🎃 Halloween until ${seasonDate("2026-10-31")}`,
    );
  });

  it("names what a hidden row is waiting for", () => {
    expect(seasonStatusLine({ showing: null, next: christmas })).toBe(
      `Hidden until 🎄 Christmas starts on ${seasonDate("2026-11-25")}`,
    );
  });

  it("says nothing for a row that follows no season", () => {
    expect(seasonStatusLine(null)).toBe("");
  });
});


describe("isNightly", () => {
  it("is true only for a schedule that runs every day", () => {
    expect(isNightly("30 3 * * *")).toBe(true);
    expect(isNightly("30 3 * * 0")).toBe(false);
    expect(isNightly("")).toBe(false);
  });
});
