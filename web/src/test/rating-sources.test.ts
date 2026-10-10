import { describe, expect, it } from "vitest";
import {
  RATING_LABELS,
  RATING_SOURCES,
  asRatingSource,
  needsMdbList,
} from "@/lib/rating-sources";

describe("needsMdbList", () => {
  it("is false only for TMDB", () => {
    expect(needsMdbList("tmdb")).toBe(false);
    for (const source of RATING_SOURCES.filter((s) => s !== "tmdb")) {
      expect(needsMdbList(source)).toBe(true);
    }
  });
});

describe("asRatingSource", () => {
  it("keeps every known source", () => {
    for (const source of RATING_SOURCES) expect(asRatingSource(source)).toBe(source);
  });

  it("falls back to TMDB for anything else", () => {
    expect(asRatingSource("rottentomatoes")).toBe("tmdb");
    expect(asRatingSource("IMDB")).toBe("tmdb");
    expect(asRatingSource(null)).toBe("tmdb");
    expect(asRatingSource(undefined)).toBe("tmdb");
    expect(asRatingSource(7)).toBe("tmdb");
  });
});

describe("RATING_LABELS", () => {
  it("names every source with a distinct label", () => {
    const labels = RATING_SOURCES.map((s) => RATING_LABELS[s]);
    expect(labels.every(Boolean)).toBe(true);
    expect(new Set(labels).size).toBe(RATING_SOURCES.length);
  });
});
