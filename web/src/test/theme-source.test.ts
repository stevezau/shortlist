import { describe, expect, it } from "vitest";

import { provenanceLabel, sourceLabel } from "@/lib/pick-provenance";
import { sourceRole } from "@/lib/trace";
import type { Pick } from "@/lib/types";

describe("the theme source (an AI row's list)", () => {
  it("is explained in a theme's words, not a season's", () => {
    expect(sourceRole("theme")).toMatch(/theme's titles in your libraries/);
    expect(sourceRole("theme")).not.toMatch(/season/);
  });

  it("is named for what it is on a pick", () => {
    expect(sourceLabel("theme")).toBe("AI row's theme");
    const pick = { sources: ["theme"], affinity: 1 } as unknown as Pick;
    expect(provenanceLabel(pick)).toMatch(/AI row's theme/);
  });
});
