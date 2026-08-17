import { describe, expect, it } from "vitest";

import {
  encode,
  hasHome,
  hasLibrary,
  joinPhrases,
  othersCount,
  ownerName,
  placementLabel,
  placementSummary,
} from "@/lib/placement";
import type { User } from "@/lib/types";

describe("placementLabel — every value of the enum", () => {
  it("names all four, including the one that was missing", () => {
    // `Placement` is a FOUR-value union and only three had a branch, so a row hidden from every
    // shelf fell through to the default and was badged "Shows on: Home & Library" — a confident,
    // specific, false claim about who can see it. One row per enum value, so a fifth can't slip in
    // silently either.
    expect(placementLabel("both")).toBe("Home & Library");
    expect(placementLabel("home")).toBe("Home");
    expect(placementLabel("library")).toBe("Library");
    expect(placementLabel("off")).toBe("Nowhere");
  });
});

describe("hasLibrary / hasHome / encode", () => {
  it("decodes each placement into its two independent flags", () => {
    expect(hasLibrary("both")).toBe(true);
    expect(hasHome("both")).toBe(true);
    expect(hasLibrary("library")).toBe(true);
    expect(hasHome("library")).toBe(false);
    expect(hasLibrary("home")).toBe(false);
    expect(hasHome("home")).toBe(true);
    expect(hasLibrary("off")).toBe(false);
    expect(hasHome("off")).toBe(false);
  });

  it("encodes every combination, including neither (a real 'off' state)", () => {
    expect(encode(true, true)).toBe("both");
    expect(encode(false, true)).toBe("home");
    expect(encode(true, false)).toBe("library");
    expect(encode(false, false)).toBe("off");
  });
});

describe("joinPhrases", () => {
  it("joins zero, one, two and three+ parts in plain English", () => {
    expect(joinPhrases([])).toBe("");
    expect(joinPhrases(["a"])).toBe("a");
    expect(joinPhrases(["a", "b"])).toBe("a and b");
    expect(joinPhrases(["a", "b", "c"])).toBe("a, b and c");
  });
});

describe("placementSummary", () => {
  it("describes a shared row as one thing everyone sees", () => {
    expect(placementSummary(true, true, true, true, true)).toBe(
      "This row shows on everyone’s Home screen and the Recommended shelf.",
    );
  });

  it("describes a per-person row's two audiences separately", () => {
    expect(placementSummary(true, true, false, true, false)).toBe(
      "Your row shows on your Home screen and your Recommended shelf. Everyone else’s row shows on their Home screen.",
    );
  });

  it("says a row claims no shelf when every surface is off", () => {
    expect(placementSummary(false, false, false, false, false)).toBe(
      "Your row doesn’t claim a shelf. Everyone else’s row doesn’t claim a shelf.",
    );
  });
});

function user(patch: Partial<User> = {}): User {
  return {
    manage_sharing: true,
    id: 1,
    username: "sarah",
    slug: "sarah",
    user_type: "shared",
    restricted: false,
    enabled: true,
    cold_start: false,
    history_depth: 10,
    last_run_at: null,
    request_tag: "",
    hit_rate: null,
    nickname: "",
    friendly_name: "",
    display_name: "",
    avatar_url: "",
    plex_account_id: 0,
    restriction_profile: "",
    unhidden_rows: 0,
    departed: false,
    preview_titles: [],
    prefs: {},
    ...patch,
  };
}

describe("ownerName / othersCount", () => {
  it("names the owner and counts everyone else", () => {
    const users = [
      user({ id: 1, user_type: "owner", display_name: "stevezau" }),
      user({ id: 2, slug: "sarah" }),
      user({ id: 3, slug: "mike" }),
    ];
    expect(ownerName(users)).toBe("stevezau");
    expect(othersCount(users)).toBe(2);
  });

  it("returns null for an unknown owner rather than a wrong name", () => {
    expect(ownerName([])).toBeNull();
  });
});
