import { describe, expect, it } from "vitest";

import { rowOverrides } from "@/lib/collections";
import type { Collection } from "@/lib/types";

/** Only what `rowOverrides` reads; every other field of a row is irrelevant to its badges. */
function row(patch: Partial<Collection> = {}): Collection {
  return {
    candidate_sources: [],
    library_keys: [],
    watched_pct: null,
    rewatch: false,
    unstarted_only: false,
    refresh_days: null,
    recency: null,
    max_seeds: null,
    recent_count: null,
    ai_instructions: { mode: "default", text: "" },
    max_runtime: null,
    min_year: null,
    max_year: null,
    min_rating: null,
    cold_start: null,
    seed_window: 1,
    show_days: [],
    placement: "both",
    placement_friends: "both",
    pin_top: false,
    hub_anchor: {},
    seasons: [],
    theme_id: null,
    ai_paused: false,
    ai_tokens: 0,
    ...patch,
  } as unknown as Collection;
}

describe("rowOverrides on an AI row", () => {
  const own = { mode: "own", text: "Only films before 2000." } as const;

  it("never badges web-search instructions: an AI row is filled from its theme and searches nothing", () => {
    const parts = rowOverrides(row({ theme_id: 3, candidate_sources: ["llm_web"], ai_instructions: own }), null, {});

    expect(parts.some((part) => part.startsWith("AI instructions"))).toBe(false);
  });

  it("badges the row's own prompt guidance in its own words", () => {
    expect(rowOverrides(row({ theme_id: 3, ai_instructions: own }), null)).toContain("AI prompt: own");
    expect(
      rowOverrides(row({ theme_id: 3, ai_instructions: { mode: "add", text: "No horror." } }), null),
    ).toContain("AI prompt: adds to the default");
    expect(rowOverrides(row({ theme_id: 3 }), null).some((part) => part.startsWith("AI prompt"))).toBe(false);
  });

  it("says when the row's AI is paused", () => {
    expect(rowOverrides(row({ theme_id: 3, ai_paused: true }), null)).toContain("AI paused");
    expect(rowOverrides(row({ theme_id: 3, ai_paused: false }), null)).not.toContain("AI paused");
  });

  it("leaves an ordinary row's badges as they were", () => {
    const parts = rowOverrides(row({ candidate_sources: ["llm_web"], ai_instructions: own }), null, {});

    expect(parts).toContain("AI instructions: own");
    expect(parts).not.toContain("AI prompt: own");
  });
});
