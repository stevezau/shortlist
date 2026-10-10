import { describe, expect, it } from "vitest";

import { groupCollections, groupTitles } from "@/lib/group-titles";

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

describe("groupCollections", () => {
  it("groups library, then person, then rows, in first-seen order", () => {
    const row = (library: string, person: string, title: string) => ({ library, person, title });
    expect(
      groupCollections([
        row("Movies", "sarah", "Picked"),
        row("TV", "sarah", "Picked"),
        row("Movies", "mike", "Picked"),
        row("Movies", "sarah", "Picked"),
      ]),
    ).toEqual([
      {
        library: "Movies",
        people: [
          { person: "sarah", titles: ["Picked ×2"] },
          { person: "mike", titles: ["Picked"] },
        ],
      },
      { library: "TV", people: [{ person: "sarah", titles: ["Picked"] }] },
    ]);
  });
});
