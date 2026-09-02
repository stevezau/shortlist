import { describe, expect, it } from "vitest";

import {
  libraryOptions,
  typesWorthNaming,
} from "@/lib/watched-libraries";

const lib = (name: string, media_type: string) => ({ name, media_type });

/** One "Movies", one "TV Shows" — the layout most Plex servers have, and the maintainer's. */
const ONE_PER_TYPE = [lib("Movies", "movie"), lib("TV Shows", "show")];

/** Four movie libraries and three TV ones. The "some people have seven or more" case. */
const SEVEN = [
  lib("4K Movies", "movie"),
  lib("Anime", "show"),
  lib("Documentaries", "movie"),
  lib("Kids Movies", "movie"),
  lib("Kids TV", "show"),
  lib("Movies", "movie"),
  lib("TV Shows", "show"),
];

describe("libraryOptions", () => {
  it("offers nothing when every type has exactly one library", () => {
    // A library dropdown would then say "Movies" and "TV Shows" beside buttons saying "Movies" and
    // "Shows" — the same choice twice. Fewer than two options means the control is not rendered.
    expect(libraryOptions(ONE_PER_TYPE, "", "")).toEqual([]);
    expect(libraryOptions(ONE_PER_TYPE, "movie", "")).toEqual([]);
  });

  it("offers nothing for a server with no libraries recorded yet", () => {
    // Every row before the first sync after the upgrade.
    expect(libraryOptions([], "", "")).toEqual([]);
  });

  it("offers every library once a single type holds two", () => {
    expect(
      libraryOptions(
        [...ONE_PER_TYPE, lib("4K Movies", "movie")],
        "",
        "",
      ),
    ).toEqual(["4K Movies", "Movies", "TV Shows"]);
  });

  it("narrows to the selected type, so seven libraries never all crowd one choice", () => {
    expect(libraryOptions(SEVEN, "show", "")).toEqual([
      "Anime",
      "Kids TV",
      "TV Shows",
    ]);
    expect(libraryOptions(SEVEN, "movie", "")).toEqual([
      "4K Movies",
      "Documentaries",
      "Kids Movies",
      "Movies",
    ]);
    expect(libraryOptions(SEVEN, "", "")).toHaveLength(7);
  });

  it("keeps a selected library that is no longer offered, so it can still be cleared", () => {
    // Renamed in Plex between requests, or a type filter applied after choosing it. Dropping it
    // would leave a filter applied with no visible control holding it.
    expect(libraryOptions(SEVEN, "show", "4K Movies")).toContain("4K Movies");
    expect(libraryOptions(ONE_PER_TYPE, "", "Movies")).toEqual([
      "Movies",
      "TV Shows",
    ]);
  });
});

describe("typesWorthNaming", () => {
  it("names no type when each has one library", () => {
    expect(typesWorthNaming(ONE_PER_TYPE)).toEqual(new Set());
  });

  it("names only the type that actually has a choice", () => {
    // Movies gain a library line; TV rows don't, because "TV Shows" under every show says nothing
    // the "Show ·" on the same line hasn't already said.
    expect(
      typesWorthNaming([...ONE_PER_TYPE, lib("4K Movies", "movie")]),
    ).toEqual(new Set(["movie"]));
  });

  it("names both when both are split", () => {
    expect(typesWorthNaming(SEVEN)).toEqual(new Set(["movie", "show"]));
  });
});
