import { describe, expect, it } from "vitest";
import {
  COMMON_LANGUAGES,
  LANGUAGE_MODES,
  LANGUAGE_MODE_HINTS,
  LANGUAGE_MODE_LABELS,
  asLanguageMode,
  languageName,
  otherLanguageBar,
} from "@/lib/request-language";

describe("asLanguageMode", () => {
  it("keeps a known mode", () => {
    expect(asLanguageMode("prefer")).toBe("prefer");
    expect(asLanguageMode("only")).toBe("only");
  });

  it("falls back to any for unknown or non-string values", () => {
    expect(asLanguageMode("everything")).toBe("any");
    expect(asLanguageMode(null)).toBe("any");
    expect(asLanguageMode(undefined)).toBe("any");
    expect(asLanguageMode(3)).toBe("any");
  });

  it("has a label and a hint for every mode", () => {
    for (const mode of LANGUAGE_MODES) {
      expect(LANGUAGE_MODE_LABELS[mode]).toBeTruthy();
      expect(LANGUAGE_MODE_HINTS[mode]).toBeTruthy();
    }
  });
});

describe("otherLanguageBar", () => {
  it("uses the typed value as is", () => {
    expect(otherLanguageBar(6, 8.2)).toBe(8.2);
    expect(otherLanguageBar(6, 0)).toBe(0);
  });

  it("sits 1.5 above the minimum rating when nothing is typed", () => {
    expect(otherLanguageBar(6, null)).toBe(7.5);
  });

  it("rounds away binary floating point noise", () => {
    expect(otherLanguageBar(6.1, null)).toBe(7.6);
  });

  it("never previews a bar above 10", () => {
    expect(otherLanguageBar(9.5, null)).toBe(10);
  });
});

describe("languageName", () => {
  it("names a known code", () => {
    expect(languageName("ko").toLowerCase()).toContain("korean");
  });

  it("ignores case and surrounding whitespace", () => {
    expect(languageName("  JA ").toLowerCase()).toContain("japanese");
  });

  it("returns an empty string for blank input", () => {
    expect(languageName("")).toBe("");
    expect(languageName("   ")).toBe("");
  });

  it("falls back to the uppercased code for something the browser cannot name", () => {
    expect(languageName("!!")).toBe("!!");
    expect(languageName("zzzz9")).toMatch(/^(ZZZZ9|zzzz9)$/i);
  });
});

describe("COMMON_LANGUAGES", () => {
  it("has no duplicates and only lowercase two-letter codes", () => {
    expect(new Set(COMMON_LANGUAGES).size).toBe(COMMON_LANGUAGES.length);
    for (const code of COMMON_LANGUAGES) expect(code).toMatch(/^[a-z]{2}$/);
  });
});
