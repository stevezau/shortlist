import { describe, expect, it } from "vitest";

import {
  DAY_CHIPS,
  showDaysSummary,
  showDaysSentence,
} from "@/lib/show-days";

describe("showDaysSummary", () => {
  it("says Every day when no days are picked", () => {
    expect(showDaysSummary([])).toBe("Every day");
  });

  it("says Every day when all seven are picked, because that is what it means", () => {
    expect(showDaysSummary([1, 2, 3, 4, 5, 6, 7])).toBe("Every day");
  });

  it("lists the chosen days in week order, not the order they were clicked", () => {
    expect(showDaysSummary([5, 1, 3])).toBe("Mon, Wed, Fri");
  });
});

describe("showDaysSentence", () => {
  it("says nothing extra for a row that is always on", () => {
    expect(showDaysSentence([])).toBe("");
  });

  it("names the days it is hidden, because that is what people are checking", () => {
    expect(showDaysSentence([1, 3, 5])).toBe(
      "Shows on Monday, Wednesday and Friday. Hidden on Tuesday, Thursday, Saturday and Sunday — it keeps its titles, so it comes straight back.",
    );
  });

  it("reads naturally with a single day", () => {
    expect(showDaysSentence([6])).toContain("Shows on Saturday.");
  });
});

describe("DAY_CHIPS", () => {
  it("starts the week on Monday, the way the editor reads", () => {
    expect(DAY_CHIPS.map((chip) => chip.iso)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(DAY_CHIPS[0]?.short).toBe("Mon");
    expect(DAY_CHIPS[6]?.short).toBe("Sun");
  });
});
