import { describe, expect, it } from "vitest";

import { renderRowName } from "@/lib/format";
import {
  fillPlaceholders,
  PLACEHOLDER_EXACT,
  PLACEHOLDER_SPLIT,
  PLACEHOLDERS,
  THEME_PLACEHOLDERS,
  usesTheme,
} from "@/lib/placeholders";

describe("the theme placeholders (an AI row's name)", () => {
  it("are the engine's two, in the engine's spelling, and stay out of the list every row shows", () => {
    // shortlist/engine/placeholders.py: THEME_PLACEHOLDERS
    expect(THEME_PLACEHOLDERS.map((p) => p.token)).toEqual(["{theme}", "{theme_emoji}"]);
    expect(PLACEHOLDERS.map((p) => p.token)).not.toContain("{theme}");
  });

  it("are spotted either way round", () => {
    expect(usesTheme("{theme_emoji} picks")).toBe(true);
    expect(usesTheme("{theme} picks")).toBe(true);
    expect(usesTheme("{season} picks")).toBe(false);
  });

  it("split out as chips, and nothing that only looks like one", () => {
    const parts = "{theme_emoji} {theme} {Theme} {genre}".split(PLACEHOLDER_SPLIT);
    expect(parts.filter((part) => PLACEHOLDER_EXACT.test(part))).toEqual(["{theme_emoji}", "{theme}"]);
  });

  it("fill from the row's theme, or a sample when a preview has none", () => {
    const values = { topSeed: "Fargo", user: "Sarah", libraryName: "Movies", season: { name: "Christmas", emoji: "🎄" } };
    expect(fillPlaceholders("{theme_emoji} {theme}", { ...values, theme: { name: "Heists", emoji: "💎" } })).toBe(
      "💎 Heists",
    );
    expect(fillPlaceholders("{theme_emoji} {theme}", values)).toBe("🌀 Twist endings");
  });

  it("render in a row name preview", () => {
    expect(renderRowName("{theme_emoji} {theme}", "Fargo", "Sarah", "Movies", undefined, { name: "Heists", emoji: "💎" })).toBe(
      "💎 Heists",
    );
  });
});
