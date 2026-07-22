import { describe, expect, it } from "vitest";

import { matchStrength, provenanceLabel } from "@/lib/pick-provenance";
import type { Pick } from "@/lib/types";

function pick(over: Partial<Pick> = {}): Pick {
  return {
    rank: 1,
    title: "The Sandman",
    reason: "Because you watched The Pitt",
    ...over,
  } as Pick;
}

describe("provenanceLabel", () => {
  it("names the source and how strong the match was", () => {
    expect(provenanceLabel(pick({ sources: ["tmdb_similar"], affinity: 1.0 }))).toBe(
      "suggested by TMDB · close match",
    );
  });

  it("admits when a pick was only loosely related", () => {
    // The reported bug: The Sandman sat near the bottom of TMDB's list for a medical drama and was
    // presented exactly like a top match. Now it says so.
    expect(provenanceLabel(pick({ sources: ["tmdb_similar"], affinity: 0.4 }))).toBe(
      "suggested by TMDB · loosely related",
    );
  });

  it("claims no strength for a source that does not rank its suggestions", () => {
    // Trakt and the AI sources report the neutral 1.0 because they have no list position to give —
    // rendering that as "close match" would be inventing a measurement.
    expect(provenanceLabel(pick({ sources: ["llm_web"], affinity: 1.0 }))).toBe(
      "suggested by AI web search",
    );
    expect(provenanceLabel(pick({ sources: ["trakt"], affinity: 1.0 }))).toBe("suggested by Trakt");
  });

  it("claims no strength for tmdb_discover, which never measured one", () => {
    // "popular in genres you like" is permanently 1.0 — matching on the `tmdb_` prefix stamped
    // "close match" on every discover pick forever.
    expect(provenanceLabel(pick({ sources: ["tmdb_discover"], affinity: 1.0 }))).toBe(
      "suggested by TMDB (your genres)",
    );
    expect(provenanceLabel(pick({ sources: ["cold_start"], affinity: 1.0 }))).toBe(
      "suggested by Popular on this server",
    );
  });

  it("reads sensibly when both TMDB sources found it", () => {
    expect(provenanceLabel(pick({ sources: ["tmdb_discover", "tmdb_similar"], affinity: 0.9 }))).toBe(
      "suggested by TMDB (similar + your genres) · close match",
    );
  });

  it("lists every source when more than one found it", () => {
    expect(provenanceLabel(pick({ sources: ["llm_library", "trakt"] }))).toBe(
      "suggested by AI, from your library + Trakt",
    );
  });

  it("says nothing for picks made before provenance was recorded", () => {
    expect(provenanceLabel(pick())).toBe("");
    expect(provenanceLabel(pick({ sources: [] }))).toBe("");
  });
});

describe("matchStrength", () => {
  it("splits the range at the boundaries the copy promises", () => {
    expect(matchStrength(1.0)).toBe("close");
    expect(matchStrength(0.8)).toBe("close");
    expect(matchStrength(0.79)).toBe("related");
    expect(matchStrength(0.5)).toBe("related");
    expect(matchStrength(0.49)).toBe("loose");
  });
});
