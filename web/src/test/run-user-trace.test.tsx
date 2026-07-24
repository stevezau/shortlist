import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TraceView } from "@/pages/run-user-trace";
import type { RunUserTraceResponse } from "@/lib/types";

/** A run that reached delivery for a movie library, with a second (4K) movie library sharing the
 *  same by-taste search. Covers: real library-name tabs, seed "why", per-title fates, delivery. */
function okTrace(
  patch: Partial<RunUserTraceResponse> = {},
): RunUserTraceResponse {
  return {
    username: "sarah",
    display_name: "Sarah",
    status: "ok",
    error: null,
    reason: null,
    trace: {
      history: {
        total: 20,
        recent: [],
        watched_movies: 18,
        watched_shows: 2,
        watched_by_library: { Movies: { movie: 18, show: 0 } },
      },
      seeds: [
        {
          title: "Toy Story",
          media: "movie",
          library: "Movies",
          tmdb_id: 862,
          weight: 4.0,
          watch_count: 4,
          recency_days: 3,
        },
      ],
      gathers: [
        {
          pool: "movie · tmdb_similar",
          sources: [
            {
              source: "tmdb_similar",
              status: "ok",
              contributed: 2,
              detail: "",
              disposition: { kept: 1, already_watched: 1 },
              queries: [
                {
                  seed: "Toy Story",
                  media: "movie",
                  total: 2,
                  returned: [
                    { tmdb_id: 863, title: "Toy Story 2", fate: "kept" },
                    { tmdb_id: 920, title: "Cars", fate: "already_watched" },
                  ],
                },
              ],
            },
          ],
        },
      ],
    },
    breakdown: [
      {
        row_slug: "picked-for-you",
        row_title: "Picked for Sarah",
        library_key: "1",
        library_title: "Movies",
        added: ["Toy Story 2"],
        removed: [],
        kept: [],
        deleted: [],
        created: true,
        picks: [
          {
            rank: 1,
            title: "Toy Story 2",
            reason: "Because you loved Toy Story",
            media_type: "movie",
          },
        ],
      },
    ],
    ...patch,
  };
}

describe("TraceView", () => {
  it("uses real library names as tabs, never a hardcoded 'Movies'/'TV Shows' when a name exists", () => {
    render(<TraceView data={okTrace()} />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toContainEqual(
      expect.stringContaining("Movies"),
    );
  });

  it("spells out why each seed mattered from its watch count and recency", () => {
    render(<TraceView data={okTrace()} />);
    expect(screen.getByText(/watched 4×, 3 days ago/)).toBeTruthy();
  });

  it("shows each returned title's fate — kept, or the plain reason it was dropped", async () => {
    render(<TraceView data={okTrace()} />);
    const searched = screen
      .getByText(/Where we searched/)
      .closest("section") as HTMLElement;
    await userEvent.click(
      within(searched).getByText(/Follow it title by title/),
    );
    expect(within(searched).getByText("Cars")).toBeTruthy();
    expect(within(searched).getByText("already watched")).toBeTruthy();
  });

  it("surfaces the error for a person the run failed on", () => {
    render(
      <TraceView
        data={okTrace({
          status: "error",
          error: "plex.tv returned 500 for share filter",
        })}
      />,
    );
    expect(screen.getByText(/This run failed for this person/)).toBeTruthy();
    expect(screen.getByText(/plex.tv returned 500/)).toBeTruthy();
  });

  it("shows the delivered picks and their reasons as the final stage", () => {
    render(<TraceView data={okTrace()} />);
    const delivered = screen
      .getByText(/What we put in Movies/)
      .closest("section");
    expect(delivered).toBeTruthy();
    expect(
      within(delivered as HTMLElement).getByText(/Because you loved Toy Story/),
    ).toBeTruthy();
  });

  it("reports the true watched total, not the length of the recent sample", () => {
    // recent is empty but the full-history counts say 18 movies — the tab must say 18, not 0/4.
    render(<TraceView data={okTrace()} />);
    const watched = screen
      .getByText(/What they watched in Movies/)
      .closest("section") as HTMLElement;
    expect(within(watched).getByText(/Watched 18 movies here/)).toBeTruthy();
  });

  it("uses the exact per-library total, distinguishing two libraries of the same media type", () => {
    // Two movie libraries: the per-MEDIA total (18) is shared, but each tab must show its OWN count.
    const data = okTrace({
      breakdown: [
        {
          row_slug: "picked-for-you",
          row_title: "Picked for Sarah",
          library_key: "2",
          library_title: "4K Movies",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [],
        },
      ],
    });
    const history = data.trace.history;
    if (history)
      history.watched_by_library = {
        "4K Movies": { movie: 6, show: 0 },
      };
    render(<TraceView data={data} />);
    expect(
      within(
        screen
          .getByText(/What they watched in 4K Movies/)
          .closest("section") as HTMLElement,
      ).getByText(/Watched 6 movies here/),
    ).toBeTruthy();
  });

  it("shows the AI web search once, naming Exa vs the model and its two steps", async () => {
    const data = okTrace();
    const gather = data.trace.gathers?.[0];
    if (!gather) throw new Error("fixture must have a gather");
    // Add llm_web as BOTH a source row and a web block — the old UI rendered it twice.
    gather.sources = [
      ...(gather.sources ?? []),
      { source: "llm_web", status: "ok", contributed: 3, detail: "" },
    ];
    gather.web = {
      mode: "exa",
      searches: [
        {
          seed: "Toy Story",
          query: "movies like Toy Story",
          cached: false,
          returned: ["A Bug's Life"],
        },
      ],
      proposed: ["A Bug's Life"],
      resolved: ["A Bug's Life"],
      unresolved: [],
      rag_system: "You are a curator.",
      rag_user: "Pick from these results.",
    };
    render(<TraceView data={data} />);
    const searched = screen
      .getByText(/Where we searched/)
      .closest("section") as HTMLElement;
    // Rendered exactly once (not once as a bare source card and once as the rich card).
    expect(within(searched).getAllByText("AI web search")).toHaveLength(1);
    // Says HOW it ran, and separates the web searches (step 1) from the AI's proposals (step 2).
    expect(
      within(searched).getByText(/searched the web with Exa/i),
    ).toBeTruthy();
    expect(within(searched).getByText(/Step 1/)).toBeTruthy();
    expect(within(searched).getByText(/Step 2/)).toBeTruthy();
  });
});
