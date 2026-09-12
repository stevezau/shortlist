import { describe, expect, it } from "vitest";

import { describeCounts } from "@/lib/run-stages";

describe("describeCounts", () => {
  it("says what an in-place update is about to add and remove, in which library", () => {
    expect(
      describeCounts({
        row: "Because you watched Fargo",
        library: "TV Shows",
        adding: 10,
        removing: 1,
      }),
    ).toBe("Because you watched Fargo · TV Shows · adding 10 titles · removing 1 title");
  });

  it("leaves out the side of an update that is not changing", () => {
    expect(
      describeCounts({ row: "Picked", library: "Movies", adding: 0, removing: 3 }),
    ).toBe("Picked · Movies · removing 3 titles");
  });

  it("leaves out a removal count of zero too", () => {
    expect(
      describeCounts({ row: "Picked", library: "Movies", adding: 4, removing: 0 }),
    ).toBe("Picked · Movies · adding 4 titles");
  });

  it("always names the library, so one row's lines in two libraries can be told apart", () => {
    // A fixed row name can contain a library's name ("Weekend Movies" in "Movies" and "4K Movies"),
    // so dropping the library when the name mentions it would leave two indistinguishable lines.
    expect(
      describeCounts({ row: "Weekend Movies", library: "Movies", creating: 30 }),
    ).toBe("Weekend Movies · Movies · new row, 30 titles");
  });

  it("reads a counted phase as progress rather than as two numbers", () => {
    expect(describeCounts({ done: 3, total: 5 })).toBe("3/5");
  });
});
