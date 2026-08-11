import { describe, expect, it } from "vitest";

import {
  RECENCY_HALF_LIFE_YEARS,
  recencyBadgeLabel,
  recencyDescription,
  recencyEras,
  recencyWeight,
} from "@/lib/constants";
import { recencyGlobal, recencySeed } from "@/lib/row-globals";

/** Every case states the year, because the point of the feature is that nothing is hardcoded to a
 *  year — a test that read the clock would pass in 2026 and start lying in 2027. */
const NOW = 2026;

describe("recencyWeight", () => {
  it("is exactly 1 when the setting is off, so the strip shows full bars", () => {
    expect(recencyWeight(30, 0)).toBe(1);
  });

  it("is 1 for a title from this year", () => {
    expect(recencyWeight(0, 100)).toBe(1);
  });

  it("halves after one half-life at full strength", () => {
    expect(recencyWeight(RECENCY_HALF_LIFE_YEARS, 100)).toBeCloseTo(0.5, 10);
  });

  it("stretches the half-life as the slider comes down", () => {
    expect(recencyWeight(RECENCY_HALF_LIFE_YEARS * 2, 50)).toBeCloseTo(0.5, 10);
  });

  it("never rewards an unreleased title", () => {
    expect(recencyWeight(-3, 100)).toBe(1);
  });

  /**
   * The parity guard. This mirrors `recency_factor` in shortlist/engine/ranking.py — the strip
   * under the slider advertises a trade-off, and if the two curves drift the UI describes ranking
   * the engine is not doing. These are the engine's own numbers at 8-year half-life, full strength.
   */
  it("matches the engine's curve at full strength", () => {
    expect(RECENCY_HALF_LIFE_YEARS).toBe(8);
    expect(recencyWeight(10, 100)).toBeCloseTo(0.42044820762685725, 12);
    expect(recencyWeight(20, 100)).toBeCloseTo(0.1767766952966369, 12);
    expect(recencyWeight(30, 100)).toBeCloseTo(0.07432544468767006, 12);
  });

  it("matches the engine's curve at half strength", () => {
    expect(recencyWeight(20, 50)).toBeCloseTo(0.42044820762685725, 12);
    expect(recencyWeight(30, 50)).toBeCloseTo(0.2726269331663144, 12);
  });
});

describe("recencyEras", () => {
  it("labels the bars with real years counted back from today", () => {
    expect(recencyEras(50, NOW).map((e) => e.year)).toEqual([
      2026, 2016, 2006, 1996, 1986,
    ]);
  });

  it("rolls the labels forward with the calendar", () => {
    expect(recencyEras(50, 2031).map((e) => e.year)).toEqual([
      2031, 2021, 2011, 2001, 1991,
    ]);
  });

  it("shows every bar full when the setting is off", () => {
    expect(recencyEras(0, NOW).every((e) => e.weight === 1)).toBe(true);
  });

  it("falls away from newest to oldest once it is on", () => {
    const weights = recencyEras(100, NOW).map((e) => e.weight);
    expect(weights).toEqual([...weights].sort((a, b) => b - a));
    expect(weights[0]).toBe(1);
    expect(weights[weights.length - 1]).toBeLessThan(0.1);
  });
});

describe("recencyDescription", () => {
  it("says the setting is off rather than quoting a meaningless percentage", () => {
    expect(recencyDescription(0, NOW)).toMatch(/ignored/i);
  });

  it("names a real year the reader can judge, not an abstract weight", () => {
    // 50% stretches the 8-year half-life to 16, so the half-weight year is 2026 - 16.
    expect(recencyDescription(50, NOW)).toContain("2010");
  });

  it("keeps the promise that old titles are not banned", () => {
    expect(recencyDescription(50, NOW)).toMatch(
      /still reach|earn it|better match/i,
    );
  });

  it("does not claim a half-weight year the era strip contradicts", () => {
    // At 5% a 40-year-old title still ranks at 84%, and the strip renders that number right above
    // this sentence. Naming 1986 as "about half" is a claim the same view disproves.
    for (const pct of [5, 10, 15]) {
      const sentence = recencyDescription(pct, NOW);
      expect(sentence).not.toMatch(/half as strongly/i);
      expect(sentence).toMatch(
        new RegExp(`${Math.round(recencyWeight(40, pct) * 100)}%`),
      );
    }
  });

  it("still names the half-weight year once that year is inside the strip", () => {
    expect(recencyDescription(20, NOW)).toMatch(/half as strongly/i);
  });

  it("moves the year it talks about as the calendar moves", () => {
    expect(recencyDescription(50, 2031)).toContain("2015");
    expect(recencyDescription(50, 2031)).not.toContain("2010");
  });
});

describe("recencyBadgeLabel", () => {
  it("reads as a release-date preference, never as freshness", () => {
    // The two sit two blocks apart in the row editor; a badge saying "Fresh" would collapse them.
    expect(recencyBadgeLabel(0.8)).toMatch(/recent/i);
    expect(recencyBadgeLabel(0.8)).not.toMatch(/fresh/i);
  });

  it("calls an explicit zero off, not 0%", () => {
    expect(recencyBadgeLabel(0)).toMatch(/any era|off/i);
  });
});

describe("row-globals", () => {
  it("phrases the global for the row editor's inherit caption", () => {
    expect(recencyGlobal({ "recommendations.recency": 0.5 })).toContain("50%");
  });

  it("says nothing at all while settings are still loading", () => {
    expect(recencyGlobal(undefined)).toBeNull();
  });

  it("calls a global of 0 off rather than showing a bare 0%", () => {
    expect(recencyGlobal({ "recommendations.recency": 0 })).toMatch(
      /any era|off/i,
    );
  });

  it("seeds a row that stops inheriting with the global, so nothing silently changes", () => {
    expect(recencySeed({ "recommendations.recency": 0.7 })).toBeCloseTo(
      0.7,
      10,
    );
  });

  it("seeds from the shipped default when settings are missing", () => {
    // Mirrors the server's DEFAULTS. Only reached before settings load — a real server always
    // answers, with its stored value or this same default.
    expect(recencySeed(undefined)).toBeCloseTo(0.5, 10);
  });
});
