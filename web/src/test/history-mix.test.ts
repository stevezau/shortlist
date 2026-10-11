import { describe, expect, it } from "vitest";

import { mixModeLine, mixName, presetFor } from "@/lib/history-mix";

describe("history mix presets", () => {
  it("derives the preset from the two counts", () => {
    expect(presetFor(0, 0)?.id).toBe("recent_only");
    expect(presetFor(3, 3)?.id).toBe("little");
    expect(presetFor(6, 6)?.id).toBe("balanced");
    expect(presetFor(10, 10)?.id).toBe("deep");
  });

  it("is Custom when the counts match no preset", () => {
    expect(presetFor(6, 3)).toBeNull();
    expect(mixName(6, 3)).toBe("Custom");
    expect(mixName(6, 6)).toBe("Balanced");
  });
});

describe("mixModeLine", () => {
  it("says nothing while settings load", () => {
    expect(mixModeLine(undefined)).toBeNull();
  });

  it("names the native setup", () => {
    expect(mixModeLine({ "curator.provider": "claude" })).toMatch(/runs no extra searches/);
  });

  it("tells Exa with and without an AI apart", () => {
    const exa = { "llm_web.search_provider": "exa" };
    expect(mixModeLine({ ...exa, "curator.provider": "claude" })).toMatch(/fair share/);
    const bare = mixModeLine({ ...exa, "curator.provider": "none" });
    expect(bare).toMatch(/Exa search, cached a week\.$/);
  });

  it("names SearXNG", () => {
    expect(mixModeLine({ "llm_web.search_provider": "searxng" })).toMatch(/SearXNG server/);
  });
});
