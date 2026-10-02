import { describe, expect, it } from "vitest";

import { seasonVerdict, titleNoun } from "@/lib/season-verdict";

const films = (size: number, perPerson: boolean) => ({ size, perPerson, media: "movie" as const });

describe("seasonVerdict", () => {
  it.each([
    [10, 15, true, "few"],
    [40, 15, true, "alike"],
    [40, 15, false, "ok"],
    [150, 15, true, "ok"],
    [15, 15, false, "ok"],
  ])("verdict(%i, %i, %s) is %s", (total, size, perPerson, level) => {
    expect(seasonVerdict(total, films(size, perPerson)).level).toBe(level);
  });

  it("says how many a row that can't be filled is short by", () => {
    expect(seasonVerdict(10, films(15, true)).text).toBe("Too few films to fill this row (10 of 15)");
  });

  it("says a per-person row will look alike, and where the season works best", () => {
    expect(seasonVerdict(40, films(15, true)).text).toBe(
      "People's rows will be much alike — works best in a shared row",
    );
  });

  it("says when there are enough", () => {
    expect(seasonVerdict(40, films(15, false)).text).toBe("Enough films for this row");
  });

  it.each([
    ["show", "Too few shows to fill this row (3 of 15)"],
    ["both", "Too few titles to fill this row (3 of 15)"],
  ] as const)("names a %s row's titles as it draws them", (media, text) => {
    expect(seasonVerdict(3, { size: 15, perPerson: false, media }).text).toBe(text);
  });
});

describe("titleNoun", () => {
  it.each([
    ["movie", 1, "film"],
    ["movie", 2, "films"],
    ["show", 1, "show"],
    ["show", 0, "shows"],
    ["both", 1, "title"],
    ["both", 72, "titles"],
  ] as const)("calls %s × %i “%s”", (media, count, noun) => {
    expect(titleNoun(media, count)).toBe(noun);
  });
});
