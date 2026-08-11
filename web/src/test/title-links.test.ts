import { describe, expect, it } from "vitest";

import { titleLinks } from "@/lib/title-links";

describe("titleLinks", () => {
  it("returns nothing when there is no TMDB id", () => {
    expect(titleLinks({ title: "Reunion", media_type: "movie" })).toEqual([]);
  });

  it("deep-links IMDb when an id is known, rather than searching", () => {
    const links = titleLinks({ tmdb_id: 1, media_type: "movie", title: "X", imdb_id: "tt0133093" });
    expect(links.find((l) => l.label === "IMDb")?.href).toBe(
      "https://www.imdb.com/title/tt0133093/",
    );
  });

  it("escapes a title with spaces and punctuation into the IMDb search", () => {
    const links = titleLinks({ tmdb_id: 1, media_type: "movie", title: "Orwell: 2+2=5" });
    expect(links.find((l) => l.label === "IMDb")?.href).toContain(
      encodeURIComponent("Orwell: 2+2=5"),
    );
  });
});
