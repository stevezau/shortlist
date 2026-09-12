import { describe, expect, it } from "vitest";

import { describeCounts } from "@/lib/run-stages";

describe("describeCounts", () => {
  it("says what an in-place update is about to add and remove, in which library", () => {
    expect(
      describeCounts({
        row: "✨ TV Shows Picked for You",
        library: "TV Shows",
        adding: 10,
        removing: 1,
      }),
    ).toBe("✨ TV Shows Picked for You · TV Shows · adding 10 titles · removing 1 title");
  });

  it("leaves out the side of an update that is not changing", () => {
    expect(
      describeCounts({ row: "Picked", library: "Movies", adding: 0, removing: 3 }),
    ).toBe("Picked · Movies · removing 3 titles");
  });

  it("says a row is being created, not updated", () => {
    expect(
      describeCounts({ row: "Picked", library: "Movies", creating: 30 }),
    ).toBe("Picked · Movies · new row, 30 titles");
  });

  it("reads a counted phase as progress rather than as two numbers", () => {
    expect(describeCounts({ done: 3, total: 5 })).toBe("3/5");
  });
});
