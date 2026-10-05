import { describe, expect, it } from "vitest";

import { seasonVerdict, titleNoun, verdictInBrief } from "@/lib/season-verdict";

const films = (size: number, perPerson: boolean) => ({ size, perPerson, media: "movie" as const });
const both = (size: number) => ({ size, perPerson: false, media: "both" as const });

describe("seasonVerdict", () => {
  it.each([
    [10, 15, true, "few"],
    [40, 15, true, "alike"],
    [40, 15, false, "ok"],
    [150, 15, true, "ok"],
    [15, 15, false, "ok"],
  ])("verdict(%i, %i, %s) is %s", (total, size, perPerson, level) => {
    expect(seasonVerdict({ total }, films(size, perPerson)).level).toBe(level);
  });

  it("says how many a row that can't be filled is short by", () => {
    expect(seasonVerdict({ total: 10 }, films(15, true)).text).toBe("Too few films to fill this row (10 of 15)");
  });

  it("says a per-person row will look alike, and where the season works best", () => {
    expect(seasonVerdict({ total: 40 }, films(15, true)).text).toBe(
      "People's rows will be much alike. Choose Shared in the row editor to use the season's most-watched titles.",
    );
  });

  it("says the match count is before the row's filters", () => {
    expect(seasonVerdict({ total: 40 }, films(15, false)).text).toBe("Enough matches before row filters");
  });

  it.each([
    ["show", "Too few shows to fill this row (3 of 15)"],
    ["both", "Too few titles to fill this row (3 of 15)"],
  ] as const)("names a %s row's titles as it draws them", (media, text) => {
    expect(seasonVerdict({ total: 3 }, { size: 15, perPerson: false, media }).text).toBe(text);
  });

  describe("a row of both, which fills each library from its own type", () => {
    it("is short when its shows are, however many films there are", () => {
      expect(seasonVerdict({ total: 40, movies: 40, shows: 0 }, both(15))).toEqual({
        level: "few",
        text: "Too few shows to fill this row's TV library (0 of 15)",
      });
    });

    it("is short when its films are", () => {
      expect(seasonVerdict({ total: 52, movies: 12, shows: 40 }, both(15)).text).toBe(
        "Too few films to fill this row's film library (12 of 15)",
      );
    });

    it("names both halves when both are short", () => {
      expect(seasonVerdict({ total: 13, movies: 12, shows: 1 }, both(15)).text).toBe(
        "Too few films or shows to fill this row's libraries (12 films and 1 show, of 15 each)",
      );
    });

    it("is enough when each half is", () => {
      expect(seasonVerdict({ total: 40, movies: 25, shows: 15 }, both(15)).text).toBe("Enough matches before row filters");
    });

    it("ignores a type it builds in no library of", () => {
      expect(seasonVerdict({ total: 40, movies: 40, shows: null }, both(15)).level).toBe("ok");
    });
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

describe("verdictInBrief", () => {
  it.each([
    [10, true, "Too few for this row"],
    [40, true, "People's rows will be much alike"],
    [150, true, "Enough before row filters"],
  ] as const)("a count of %i reads “%s”", (total, perPerson, brief) => {
    expect(verdictInBrief(seasonVerdict({ total }, films(15, perPerson)))).toBe(brief);
  });
});
