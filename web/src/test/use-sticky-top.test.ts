import { describe, expect, it } from "vitest";

import { stickyTop } from "@/lib/use-sticky-top";

describe("stickyTop", () => {
  it("pins a panel that fits the screen at the gap from the top", () => {
    expect(stickyTop(500, 900, 24)).toBe(24);
  });

  it("pins a panel taller than the screen by its bottom, so every line is reachable by scrolling the page", () => {
    // 1400px panel on a 900px screen: it scrolls with the page until its last line is 24px above the
    // bottom edge, then stays — and scrolling back to the top of the page brings its heading back.
    expect(stickyTop(1400, 900, 24)).toBe(900 - 1400 - 24);
  });

  it("switches exactly where the panel stops fitting", () => {
    expect(stickyTop(900 - 48, 900, 24)).toBe(24);
    expect(stickyTop(900 - 47, 900, 24)).toBe(23);
  });

  it("stops a tall panel above a bar pinned to the bottom of the screen", () => {
    // The row editor's Save bar is ~61px tall; the panel's last line must sit above it, not under it.
    expect(stickyTop(1400, 900, 24, 80)).toBe(900 - 1400 - 80);
    expect(stickyTop(500, 900, 24, 80)).toBe(24);
  });
});
