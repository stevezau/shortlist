import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RequestHoldsField } from "@/components/request-holds-field";
import {
  EMPTY_HOLDS,
  exceptionCount,
  joinTitles,
  readHoldTags,
  readHolds,
  writeHoldTags,
  writeHolds,
  type HoldDraft,
} from "@/lib/request-holds";
import type { HoldPreview, HoldPreviewInput } from "@/lib/types";

const { previewHolds, getTmdbTags, getTmdbCompanies } = vi.hoisted(() => ({
  previewHolds: vi.fn(),
  getTmdbTags: vi.fn(),
  getTmdbCompanies: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    previewHolds: (body: unknown) => previewHolds(body),
    getTmdbTags: (q: string, media: string) => getTmdbTags(q, media),
    getTmdbCompanies: (q: string) => getTmdbCompanies(q),
  },
}));

/** Each panel's draft as the field last handed it back. */
const latest: { movies: HoldDraft; shows: HoldDraft } = { movies: EMPTY_HOLDS, shows: EMPTY_HOLDS };

function Harness({ movies, shows }: { movies: HoldDraft; shows: HoldDraft }) {
  const [m, setM] = useState(movies);
  const [s, setS] = useState(shows);
  return (
    <RequestHoldsField
      movies={m}
      shows={s}
      ratingLabel="TMDB"
      onMovies={(draft) => {
        latest.movies = draft;
        setM(draft);
      }}
      onShows={(draft) => {
        latest.shows = draft;
        setS(draft);
      }}
    />
  );
}

function renderField(props: { movies?: Partial<HoldDraft>; shows?: Partial<HoldDraft> } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  latest.movies = { ...EMPTY_HOLDS, ...props.movies };
  latest.shows = { ...EMPTY_HOLDS, ...props.shows };
  render(
    <QueryClientProvider client={client}>
      <Harness movies={latest.movies} shows={latest.shows} />
    </QueryClientProvider>,
  );
}

const panel = (name: "Movies" | "Shows") => within(screen.getByRole("group", { name }));

/** The preview bodies the field asked for, of one media type. */
const asked = (media: "movie" | "show") =>
  previewHolds.mock.calls.map(([body]) => body as HoldPreviewInput).filter((b) => b.media_type === media);

const PREVIEW: HoldPreview = {
  checked: 14,
  held: [
    {
      tmdb_id: 1,
      title: "BTS: Permission to Dance on Stage",
      year: 2022,
      reason: "tag “concert”",
      story: false,
      companies: [],
    },
    {
      tmdb_id: 2,
      title: "A Star Is Born",
      year: 2018,
      reason: "tag “concert”",
      story: true,
      companies: [{ id: 3, name: "Live Nation Productions" }],
    },
  ],
  unread: 1,
  excepted: [],
};

/** Moana 2 as TMDB lists it, held by the Animation genre. */
const MOANA_2 = {
  tmdb_id: 1241982,
  title: "Moana 2",
  year: 2024,
  reason: "genre Animation",
  story: true,
  companies: [
    { id: 2, name: "Walt Disney Pictures" },
    { id: 6125, name: "Walt Disney Animation Studios" },
  ],
};

describe("RequestHoldsField", () => {
  beforeEach(() => {
    previewHolds.mockReset();
    previewHolds.mockResolvedValue(PREVIEW);
    getTmdbTags.mockReset();
    getTmdbTags.mockResolvedValue([]);
    getTmdbCompanies.mockReset();
    getTmdbCompanies.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("asks nothing of the server until something is picked, in either panel", async () => {
    vi.useFakeTimers();
    renderField();
    expect(screen.getByText(/Nothing picked, so any movie can be requested automatically/i)).toBeInTheDocument();
    expect(screen.getByText(/Nothing picked, so any show can be requested automatically/i)).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(previewHolds).not.toHaveBeenCalled();
  });

  it("toggles a movie genre and previews it against the movies in the inbox", async () => {
    renderField();
    const music = panel("Movies").getByRole("button", { name: "Music" });
    expect(music).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(music);
    expect(music).toHaveAttribute("aria-pressed", "true");
    await waitFor(() =>
      expect(previewHolds).toHaveBeenCalledWith({
        media_type: "movie",
        genres: [10402],
        tags: [],
        except_companies: [],
        except_min_rating: null,
      }),
    );
    expect(await screen.findByText(/Would hold 2 of the 14 movies waiting in your inbox/i)).toBeInTheDocument();
    expect(asked("show")).toEqual([]);
  });

  it("offers TMDB's TV genres in the Shows panel and keeps its picks apart", async () => {
    previewHolds.mockResolvedValue({ checked: 6, held: [], unread: 0, excepted: [] });
    renderField();
    // Reality is a TV genre only; Music is a movie genre only.
    expect(panel("Shows").queryByRole("button", { name: "Music" })).toBeNull();
    expect(panel("Movies").queryByRole("button", { name: "Reality" })).toBeNull();
    expect(panel("Shows").getByText(/Reality, Talk, Kids and News exist only for shows/i)).toBeInTheDocument();

    await userEvent.click(panel("Shows").getByRole("button", { name: "Reality" }));
    await waitFor(() => expect(asked("show").at(-1)).toMatchObject({ genres: [10764], tags: [] }));
    expect(latest.movies).toEqual(EMPTY_HOLDS);
    expect(latest.shows.genres).toEqual([10764]);
    expect(await panel("Shows").findByText(/Holds none of the 6 shows waiting in your inbox/i)).toBeInTheDocument();
    expect(asked("movie")).toEqual([]);
  });

  it("suggests show tags in the Shows panel, and a tag picked there is a show pick", async () => {
    renderField();
    await userEvent.click(panel("Shows").getByRole("button", { name: "Add tag anime" }));
    expect(latest.shows.tags).toEqual([{ id: 210024, name: "anime" }]);
    expect(latest.movies.tags).toEqual([]);
    // The movie panel's own suggestion of the same tag is untouched.
    expect(panel("Movies").getByRole("button", { name: "Add tag stand-up comedy" })).toBeInTheDocument();
    expect(panel("Shows").getByRole("button", { name: "Remove tag anime" })).toBeInTheDocument();
  });

  it("searches show tags by their show count", async () => {
    getTmdbTags.mockResolvedValue([{ id: 210024, name: "anime", movies: null, shows: 4100 }]);
    renderField();
    await userEvent.type(panel("Shows").getByLabelText("Search TMDB tags"), "anime");
    expect(await panel("Shows").findByText(/4100 shows on TMDB/)).toBeInTheDocument();
    expect(getTmdbTags).toHaveBeenCalledWith("anime", "show");
  });

  it("flags a held story film so a too-broad pick shows itself", async () => {
    renderField({ movies: { tags: [{ id: 6029, name: "concert" }] } });
    expect(await screen.findByText(/1 of these is a story film/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Story film")).toBeInTheDocument();
    expect(screen.getByText(/TMDB didn.t answer for 1 movie/i)).toBeInTheDocument();
  });

  it("adds a suggested tag, then stops suggesting it, and removes it again", async () => {
    renderField();
    const movies = panel("Movies");
    await userEvent.click(movies.getByRole("button", { name: "Add tag concert film" }));
    expect(movies.queryByRole("button", { name: "Add tag concert film" })).toBeNull();
    await userEvent.click(movies.getByRole("button", { name: /Remove tag concert film/i }));
    expect(movies.getByRole("button", { name: "Add tag concert film" })).toBeInTheDocument();
  });

  it("says plainly when the picks hold nothing waiting", async () => {
    previewHolds.mockResolvedValue({ checked: 14, held: [], unread: 0, excepted: [] });
    renderField({ movies: { genres: [37] } });
    expect(await screen.findByText(/Holds none of the 14 movies waiting in your inbox/i)).toBeInTheDocument();
  });

  it("offers a retry when the preview fails", async () => {
    previewHolds.mockRejectedValue(new Error("boom"));
    renderField({ movies: { genres: [37] } });
    expect(await screen.findByText(/Couldn.t check your inbox just now/i)).toBeInTheDocument();
    previewHolds.mockResolvedValue({ checked: 3, held: [], unread: 0, excepted: [] });
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/Holds none of the 3 movies/i)).toBeInTheDocument();
  });

  it("names the matches an exception sends anyway", async () => {
    previewHolds.mockResolvedValue({
      ...PREVIEW,
      excepted: ["Amy", "Summer of Soul", "Stop Making Sense"].map((title, i) => ({
        tmdb_id: 10 + i,
        title,
        year: null,
        reason: "genre Documentary",
        exception: "studio A24",
      })),
    });
    renderField({ movies: { genres: [99], companies: [{ id: 41077, name: "A24" }] } });
    expect(
      await screen.findByText("Sent anyway (exception): Amy, Summer of Soul and Stop Making Sense."),
    ).toBeInTheDocument();
  });

  describe("a held row", () => {
    it("opens to the title's own studios, one row at a time", async () => {
      renderField({ movies: { tags: [{ id: 6029, name: "concert" }] } });
      const born = await screen.findByRole("button", { name: /A Star Is Born/ });
      const bts = screen.getByRole("button", { name: /BTS: Permission to Dance on Stage/ });
      expect(born).toHaveAttribute("aria-expanded", "false");

      await userEvent.click(born);
      expect(born).toHaveAttribute("aria-expanded", "true");
      expect(screen.getByRole("button", { name: "Let Live Nation Productions through" })).toBeInTheDocument();

      await userEvent.click(bts);
      expect(born).toHaveAttribute("aria-expanded", "false");
      expect(screen.getByText(/TMDB lists no studios for this movie/i)).toBeInTheDocument();
    });

    it("lets a studio through from the title, and the preview sends the title anyway", async () => {
      previewHolds.mockImplementation((body: HoldPreviewInput) =>
        Promise.resolve(
          body.except_companies?.includes(6125)
            ? {
                checked: 3,
                held: [],
                unread: 0,
                excepted: [{ ...MOANA_2, exception: "studio Walt Disney Animation Studios" }],
              }
            : { checked: 3, held: [MOANA_2], unread: 0, excepted: [] },
        ),
      );
      renderField({ movies: { genres: [16] } });
      await userEvent.click(await screen.findByRole("button", { name: /Moana 2/ }));
      await userEvent.click(screen.getByRole("button", { name: "Let Walt Disney Animation Studios through" }));

      // Saved exactly as the studio search would save it: the id on the title, with its name.
      expect(latest.movies.companies).toEqual([{ id: 6125, name: "Walt Disney Animation Studios" }]);
      await waitFor(() => expect(asked("movie").at(-1)).toMatchObject({ except_companies: [6125] }));
      expect(await screen.findByText("Sent anyway (exception): Moana 2.")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Moana 2/ })).toBeNull();
      expect(panel("Movies").getByRole("button", { name: /Exceptions/ })).toHaveTextContent("1 exception");
    });

    it("merges a studio TMDB lists twice into one row that lets every id through in one save", async () => {
      const moana = {
        ...MOANA_2,
        companies: [
          { id: 6125, name: "Walt Disney Animation Studios" },
          { id: 2, name: "Walt Disney Pictures" },
          { id: 158526, name: " walt disney animation studios" },
        ],
      };
      previewHolds.mockResolvedValue({ checked: 3, held: [moana], unread: 0, excepted: [] });
      renderField({ movies: { genres: [16], companies: [{ id: 41077, name: "A24" }] } });
      await userEvent.click(await screen.findByRole("button", { name: /Moana 2/ }));
      const studios = within(screen.getByRole("list", { name: "Studios of Moana 2" }));
      expect(studios.getAllByRole("listitem").map((li) => li.firstChild?.textContent)).toEqual([
        "Walt Disney Animation Studios",
        "Walt Disney Pictures",
      ]);
      expect(studios.getAllByRole("button", { name: "Let Walt Disney Animation Studios through" })).toHaveLength(1);

      const saves = previewHolds.mock.calls.length;
      await userEvent.click(studios.getByRole("button", { name: "Let Walt Disney Animation Studios through" }));
      // Stored as the separate ids a run matches, both from the one click.
      expect(latest.movies.companies).toEqual([
        { id: 41077, name: "A24" },
        { id: 6125, name: "Walt Disney Animation Studios" },
        { id: 158526, name: " walt disney animation studios" },
      ]);
      await waitFor(() => expect(asked("movie").at(-1)).toMatchObject({ except_companies: [6125, 41077, 158526] }));
      expect(previewHolds.mock.calls.length).toBe(saves + 1);
      expect(studios.getByText("Already let through")).toBeInTheDocument();
    });

    it("still offers a merged studio while any of its ids is missing", async () => {
      const moana = {
        ...MOANA_2,
        companies: [
          { id: 6125, name: "Walt Disney Animation Studios" },
          { id: 158526, name: "Walt Disney Animation Studios" },
        ],
      };
      previewHolds.mockResolvedValue({ checked: 3, held: [moana], unread: 0, excepted: [] });
      renderField({ movies: { genres: [16], companies: [{ id: 6125, name: "Walt Disney Animation Studios" }] } });
      await userEvent.click(await screen.findByRole("button", { name: /Moana 2/ }));
      await userEvent.click(screen.getByRole("button", { name: "Let Walt Disney Animation Studios through" }));
      expect(latest.movies.companies.map((c) => c.id)).toEqual([6125, 158526]);
    });

    it("says when a studio on the title is already let through", async () => {
      previewHolds.mockResolvedValue({ checked: 3, held: [MOANA_2], unread: 0, excepted: [] });
      renderField({ movies: { genres: [16], companies: [{ id: 2, name: "Walt Disney Pictures" }] } });
      await userEvent.click(await screen.findByRole("button", { name: /Moana 2/ }));
      expect(screen.queryByRole("button", { name: "Let Walt Disney Pictures through" })).toBeNull();
      expect(screen.getByText("Already let through")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Let Walt Disney Animation Studios through" })).toBeInTheDocument();
    });
  });

  describe("exceptions", () => {
    it("are folded away, and the folded row says how many are set", async () => {
      renderField({ shows: { companies: [{ id: 3268, name: "HBO" }], minRating: 8.5 } });
      const movies = panel("Movies").getByRole("button", { name: /Exceptions/ });
      const shows = panel("Shows").getByRole("button", { name: /Exceptions/ });
      expect(movies).toHaveAttribute("aria-expanded", "false");
      expect(movies).toHaveTextContent("none");
      expect(shows).toHaveTextContent("2 exceptions");
      expect(panel("Shows").queryByLabelText("Rated at least")).toBeNull();
    });

    it("show each found studio's country and TMDB logo, so the real one stands out", async () => {
      getTmdbCompanies.mockResolvedValue([
        { id: 6125, name: "Walt Disney Animation Studios", country: "US", logo_path: "/wdas.png" },
        { id: 158526, name: "Walt Disney Animation Studios", country: "", logo_path: "" },
      ]);
      renderField();
      const movies = panel("Movies");
      await userEvent.click(movies.getByRole("button", { name: /Exceptions/ }));
      await userEvent.type(movies.getByLabelText("Search TMDB studios"), "walt");
      const found = within(await movies.findByRole("list", { name: "TMDB studios found" }));
      const [real, empty] = found.getAllByRole("listitem");
      expect(real).toHaveTextContent("(US)");
      expect(real?.querySelector("img")).toHaveAttribute("src", "https://image.tmdb.org/t/p/w45/wdas.png");
      expect(empty?.querySelector("img")).toBeNull();
    });

    it("add a studio from a TMDB company search, and remove it again", async () => {
      getTmdbCompanies.mockResolvedValue([{ id: 41077, name: "A24", country: "US", logo_path: "" }]);
      renderField({ movies: { genres: [99] } });
      const movies = panel("Movies");
      await userEvent.click(movies.getByRole("button", { name: /Exceptions/ }));
      expect(movies.getByText(/A match above still goes out on its own/i)).toBeInTheDocument();
      await userEvent.type(movies.getByLabelText("Search TMDB studios"), "a24");
      await userEvent.click(await movies.findByRole("button", { name: "Add studio A24" }));
      expect(latest.movies.companies).toEqual([{ id: 41077, name: "A24" }]);
      await waitFor(() => expect(asked("movie").at(-1)).toMatchObject({ except_companies: [41077] }));
      expect(movies.getByRole("button", { name: /Exceptions/ })).toHaveTextContent("1 exception");

      await userEvent.click(movies.getByRole("button", { name: "Remove studio A24" }));
      expect(latest.movies.companies).toEqual([]);
    });

    it("say a show's studio is its production company, never its network", async () => {
      renderField();
      await userEvent.click(panel("Shows").getByRole("button", { name: /Exceptions/ }));
      expect(panel("Shows").getByText(/not the network a show airs on/i)).toBeInTheDocument();
    });

    it("read a cleared rating box as off, not as a bar of 0", async () => {
      renderField({ shows: { tags: [{ id: 210024, name: "anime" }], minRating: 8.5 } });
      const shows = panel("Shows");
      await userEvent.click(shows.getByRole("button", { name: /Exceptions/ }));
      const box = shows.getByLabelText("Rated at least");
      expect(box).toHaveValue(8.5);
      expect(shows.getByText("on TMDB")).toBeInTheDocument();
      await userEvent.clear(box);
      expect(latest.shows.minRating).toBeNull();
      await waitFor(() => expect(asked("show").at(-1)).toMatchObject({ except_min_rating: null }));
      await userEvent.type(box, "9");
      expect(latest.shows.minRating).toBe(9);
    });
  });
});

describe("hold storage", () => {
  it("round-trips the saved {id: name} map and drops anything malformed", () => {
    const tags = [
      { id: 156205, name: "concert film" },
      { id: 9716, name: "stand-up comedy" },
    ];
    // A JS object lists integer-like keys in numeric order, so the round trip keeps the tags, not their order.
    expect(readHoldTags(writeHoldTags(tags))).toEqual([...tags].sort((a, b) => a.id - b.id));
    expect(readHoldTags({ abc: "x", "5": 7, "6": "ok" })).toEqual([{ id: 6, name: "ok" }]);
    expect(readHoldTags(["156205"])).toEqual([]);
    expect(readHoldTags(undefined)).toEqual([]);
  });

  it("reads the unprefixed keys as the movie picks, so an upgrade keeps what it held", () => {
    const settings = {
      "requests.hold_genres": [99],
      "requests.hold_tags": { "9716": "stand-up comedy" },
      "requests.hold_show_tags": { "210024": "anime" },
      "requests.hold_show_except_min_rating": 8.5,
    };
    expect(readHolds(settings, "movie")).toEqual({
      genres: [99],
      tags: [{ id: 9716, name: "stand-up comedy" }],
      companies: [],
      minRating: null,
    });
    expect(readHolds(settings, "show")).toEqual({
      genres: [],
      tags: [{ id: 210024, name: "anime" }],
      companies: [],
      minRating: 8.5,
    });
  });

  it("writes each panel to its own keys", () => {
    const draft: HoldDraft = {
      genres: [10764],
      tags: [{ id: 210024, name: "anime" }],
      companies: [{ id: 3268, name: "HBO" }],
      minRating: 0,
    };
    expect(writeHolds(draft, "show")).toEqual({
      "requests.hold_show_genres": [10764],
      "requests.hold_show_tags": { "210024": "anime" },
      "requests.hold_show_except_companies": { "3268": "HBO" },
      "requests.hold_show_except_min_rating": 0,
    });
    expect(Object.keys(writeHolds(EMPTY_HOLDS, "movie"))).toEqual([
      "requests.hold_genres",
      "requests.hold_tags",
      "requests.hold_except_companies",
      "requests.hold_except_min_rating",
    ]);
    // 0 is a real bar and counts as an exception; only null is off.
    expect(exceptionCount(draft)).toBe(2);
    expect(exceptionCount(EMPTY_HOLDS)).toBe(0);
  });

  it("joins titles the house way", () => {
    expect(joinTitles([])).toBe("");
    expect(joinTitles(["A"])).toBe("A");
    expect(joinTitles(["A", "B"])).toBe("A and B");
    expect(joinTitles(["A", "B", "C"])).toBe("A, B and C");
  });
});
