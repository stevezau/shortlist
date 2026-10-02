import { describe, expect, it } from "vitest";

import { seasonVerdict } from "@/lib/season-verdict";

describe("seasonVerdict", () => {
  it.each([
    [10, 15, true, "few"],
    [40, 15, true, "alike"],
    [40, 15, false, "ok"],
    [150, 15, true, "ok"],
    [15, 15, false, "ok"],
  ])("verdict(%i, %i, %s) is %s", (total, size, perPerson, level) => {
    expect(seasonVerdict(total, size, perPerson).level).toBe(level);
  });

  it("says how many a row that can't be filled is short by", () => {
    expect(seasonVerdict(10, 15, true).text).toBe("Too few to fill this row (10 of 15)");
  });

  it("says a per-person row will look alike, and where the season works best", () => {
    expect(seasonVerdict(40, 15, true).text).toBe("People's rows will be much alike — works best in a shared row");
  });

  it("says when there are enough", () => {
    expect(seasonVerdict(40, 15, false).text).toBe("Enough for this row");
  });
});
