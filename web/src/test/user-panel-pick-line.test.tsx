import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UserPanel } from "@/components/runs/user-panel";
import type { RunDetail, RunUserResult } from "@/lib/types";

/** A finished run user carrying one library's breakdown — what the run page actually renders. */
function result(picks: Record<string, unknown>[]): RunUserResult {
  return {
    username: "sarah",
    display_name: "Sarah",
    status: "ok",
    error: null,
    reason: null,
    duration_ms: 1200,
    llm_tokens: 0,
    llm_tokens_by_step: {},
    exa_searches: 0,
    has_trace: false,
    diff: { added: [], removed: [], kept: [], deleted: [] },
    picks: [],
    breakdown: [
      {
        row_slug: "picked",
        row_title: "Picked for You",
        library_key: "1",
        library_title: "Movies",
        added: [],
        removed: [],
        kept: [],
        deleted: [],
        created: false,
        rating_key: 0,
        picks,
      },
    ],
  } as unknown as RunUserResult;
}

const RUN = { id: 1, status: "ok", users: [] } as unknown as RunDetail;

const BASE = {
  rank: 1,
  title: "The Matrix",
  reason: "Because you liked action",
  seed_title: "John Wick",
  tmdb_id: 603,
  media_type: "movie",
  sources: ["tmdb_similar"],
  affinity: 1,
};

describe("run report pick line", () => {
  it("shows the release year beside the title", () => {
    render(
      <UserPanel
        run={RUN}
        result={result([{ ...BASE, year: 1999, rating: 8.2 }])}
      />,
    );
    expect(screen.getByText(/\(1999\)/)).toBeInTheDocument();
  });

  it("shows the score it was picked on, labelled as TMDB", () => {
    // Labelled TMDB rather than the server's configured rating source: the stored number IS TMDB's
    // vote_average whatever that setting says, so any other label would misattribute it.
    render(
      <UserPanel
        run={RUN}
        result={result([{ ...BASE, year: 1999, rating: 8.2 }])}
      />,
    );
    expect(screen.getByText(/TMDB 8\.2/)).toBeInTheDocument();
  });

  it("keeps the provenance line alongside the score", () => {
    render(
      <UserPanel
        run={RUN}
        result={result([{ ...BASE, year: 1999, rating: 8.2 }])}
      />,
    );
    expect(screen.getByText(/TMDB 8\.2 · /)).toBeInTheDocument();
  });

  it("says nothing at all for a pick with no year", () => {
    // A cold-start pick comes from the library's top-rated list, not a TMDB candidate. Rendering
    // "(null)" or "(0)" there would read as a data error rather than an absent field.
    render(
      <UserPanel
        run={RUN}
        result={result([{ ...BASE, year: null, rating: 7 }])}
      />,
    );
    expect(screen.getByText("The Matrix")).toBeInTheDocument();
    expect(screen.queryByText(/\(null\)|\(0\)/)).not.toBeInTheDocument();
  });

  it("does not render an unrated pick as a score of zero", () => {
    render(
      <UserPanel
        run={RUN}
        result={result([{ ...BASE, year: 1999, rating: 0 }])}
      />,
    );
    expect(screen.queryByText(/TMDB 0\.0/)).not.toBeInTheDocument();
  });

  it("still renders a legacy pick that carries neither field", () => {
    render(<UserPanel run={RUN} result={result([BASE])} />);
    expect(screen.getByText("The Matrix")).toBeInTheDocument();
  });
});

describe("run report pick line — look-it-up links", () => {
  it("links a pick to TMDB, IMDb and Trakt", () => {
    render(
      <UserPanel
        run={RUN}
        result={result([{ ...BASE, year: 1999, rating: 8.2 }])}
      />,
    );
    expect(screen.getByRole("link", { name: "TMDB" })).toHaveAttribute(
      "href",
      "https://www.themoviedb.org/movie/603",
    );
    expect(screen.getByRole("link", { name: "Trakt" })).toHaveAttribute(
      "href",
      "https://trakt.tv/search/tmdb/603?id_type=movie",
    );
  });

  it("searches IMDb by title, since a delivered pick carries no IMDb id", () => {
    render(
      <UserPanel
        run={RUN}
        result={result([{ ...BASE, year: 1999, rating: 8.2 }])}
      />,
    );
    expect(screen.getByRole("link", { name: "IMDb" })).toHaveAttribute(
      "href",
      "https://www.imdb.com/find/?q=The%20Matrix&s=tt",
    );
  });

  it("uses the tv path for a show", () => {
    render(
      <UserPanel
        run={RUN}
        result={result([
          { ...BASE, media_type: "show", year: 2008, rating: 9.4 },
        ])}
      />,
    );
    expect(screen.getByRole("link", { name: "TMDB" })).toHaveAttribute(
      "href",
      "https://www.themoviedb.org/tv/603",
    );
  });

  it("shows no links at all for a pick with no TMDB id", () => {
    // A cold-start pick comes from the library, not TMDB. A link to /movie/undefined is worse than
    // no link — it looks like a working link and 404s.
    const { tmdb_id: _omitted, ...noId } = BASE;
    render(<UserPanel run={RUN} result={result([{ ...noId, year: 1999 }])} />);
    expect(
      screen.queryByRole("link", { name: "TMDB" }),
    ).not.toBeInTheDocument();
  });

  it("opens them in a new tab without leaking the referrer", () => {
    render(
      <UserPanel
        run={RUN}
        result={result([{ ...BASE, year: 1999, rating: 8.2 }])}
      />,
    );
    const link = screen.getByRole("link", { name: "TMDB" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });
});
