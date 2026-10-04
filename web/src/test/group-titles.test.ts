import { describe, expect, it } from "vitest";

import { groupTitles } from "@/lib/group-titles";

describe("groupTitles", () => {
  it("counts identical titles and keeps first-seen order", () => {
    expect(
      groupTitles(["✨ Movies Picked for You", "✨ TV Picked for You", "✨ Movies Picked for You", "✨ Movies Picked for You"]),
    ).toEqual(["✨ Movies Picked for You ×3", "✨ TV Picked for You"]);
  });

  it("leaves unique titles alone and handles an empty list", () => {
    expect(groupTitles(["A", "B"])).toEqual(["A", "B"]);
    expect(groupTitles([])).toEqual([]);
  });
});
