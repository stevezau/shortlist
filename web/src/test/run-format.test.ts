import { describe, expect, it } from "vitest";

import {
  currentPhase,
  errorBucket,
  exaSummary,
  friendlyError,
  rankClass,
  tokenStepBreakdown,
} from "@/lib/run-format";
import type { RunLogEntry } from "@/lib/types";

describe("friendlyError", () => {
  it("recognises a Plex 500, a timeout and a 429 with their own sentence", () => {
    expect(friendlyError("HTTP 500 internal_server_error")).toMatch(
      /server error \(500\)/,
    );
    expect(friendlyError("request timed out after 30s")).toMatch(/timed out/);
    expect(friendlyError("429 too many requests")).toMatch(/rate-limiting/);
  });

  it("falls back to one generic sentence for anything unrecognised", () => {
    expect(friendlyError("KeyError: 'ratingKey'")).toBe(
      "Something went wrong building this person’s row.",
    );
    expect(friendlyError("connection refused to 10.0.0.5:32400")).toBe(
      "Something went wrong building this person’s row.",
    );
  });
});

describe("errorBucket — only the three recognised classes count as 'the same problem'", () => {
  it("groups two different raw 500s into the same bucket", () => {
    expect(errorBucket("HTTP 500 while updating collection 123")).toBe(
      errorBucket("HTTP 500 while updating collection 456"),
    );
  });

  it("does not group unrelated unrecognised errors — the bug this fixes", () => {
    // Both fell through to friendlyError's one generic sentence before this fix, so bucketing on
    // that return value (the old errorBucket) merged them into a false "same problem" claim.
    const a = errorBucket("KeyError: 'ratingKey'");
    const b = errorBucket("connection refused to 10.0.0.5:32400");
    const c = errorBucket("AttributeError: NoneType has no attribute 'guid'");
    expect(a).toBeNull();
    expect(b).toBeNull();
    expect(c).toBeNull();
    // null !== null is false in a Map key sense (Map treats null as one key), so the CALLER must
    // treat a null bucket as "never counted" rather than grouping every unrecognised error together.
    // errorBucket itself only promises: recognised errors bucket by class, everything else is null.
  });

  it("keeps the three recognised classes distinct from one another", () => {
    const classes = new Set([
      errorBucket("HTTP 500"),
      errorBucket("timed out"),
      errorBucket("429 too many requests"),
    ]);
    expect(classes.size).toBe(3);
  });
});

describe("rankClass", () => {
  it("tiers 1-3, 4-10, and the rest", () => {
    expect(rankClass(1)).toContain("amber");
    expect(rankClass(3)).toContain("amber");
    expect(rankClass(4)).not.toContain("amber");
    expect(rankClass(10)).not.toContain("muted");
    expect(rankClass(11)).toContain("muted");
  });
});

describe("tokenStepBreakdown", () => {
  it("formats the by-step map, biggest first, dropping zeros", () => {
    expect(
      tokenStepBreakdown({ curate: 251295, llm_web: 126133, llm_library: 0 }),
    ).toBe("final picks 251,295 · web search 126,133");
  });

  it("returns '' for undefined — the single shape both callers (parens or not) build on", () => {
    expect(tokenStepBreakdown(undefined)).toBe("");
  });
});

describe("exaSummary", () => {
  it("says nothing when zero or undefined", () => {
    expect(exaSummary(undefined)).toBe("");
    expect(exaSummary(0)).toBe("");
  });

  it("pluralises correctly", () => {
    expect(exaSummary(1)).toBe(" · 1 Exa search");
    expect(exaSummary(2)).toBe(" · 2 Exa searches");
  });
});

function entry(patch: Partial<RunLogEntry>): RunLogEntry {
  return {
    seq: 0,
    ts: "2026-07-15T04:18:00Z",
    run_id: 2,
    user: "sarah",
    stage: "history",
    counts: {},
    ...patch,
  };
}

describe("currentPhase", () => {
  it("names the latest server-wide stage, not a per-person one", () => {
    const phase = currentPhase([
      entry({ user: "sarah", stage: "curating" }),
      entry({ user: "Shortlist", stage: "converging" }),
    ]);
    expect(phase).toMatch(/stranded/i);
  });

  it("returns null once the server-wide finished marker is the latest", () => {
    const phase = currentPhase([
      entry({ user: "Shortlist", stage: "converging" }),
      entry({ user: "Shortlist", stage: "finished" }),
    ]);
    expect(phase).toBeNull();
  });

  it("returns null with no server-wide stage recorded yet", () => {
    expect(
      currentPhase([entry({ user: "sarah", stage: "curating" })]),
    ).toBeNull();
  });
});
