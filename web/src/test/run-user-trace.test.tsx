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

  it("tags each seed with recency only — no play-count, since frequency no longer scores", () => {
    render(<TraceView data={okTrace()} />);
    expect(screen.getByText(/3 days ago/)).toBeTruthy();
    // ×count is deliberately gone (recency alone weights a seed now).
    expect(screen.queryByText(/watched 4×/)).toBeNull();
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
      .getByText(/What they watched recently in Movies/)
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
          .getByText(/What they watched recently in 4K Movies/)
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

  it("explains how the shortlist was ordered, grounded in this library's picks — and says no AI ranks", () => {
    const data = okTrace();
    // Give the delivered pick a source + seed_title so the "grounded in this row" line has real numbers.
    const pick = data.breakdown?.[0]?.picks?.[0];
    if (!pick) throw new Error("fixture must have a delivered pick");
    pick.sources = ["tmdb_similar"];
    pick.seed_title = "Toy Story";
    render(<TraceView data={data} />);
    const ordered = screen
      .getByText(/How we ordered the shortlist/)
      .closest("section") as HTMLElement;
    expect(within(ordered).getByText(/No AI decides this order/)).toBeTruthy();
    // Grounded: 1 pick from 1 source and 1 watched title.
    expect(within(ordered).getByText(/1 source/)).toBeTruthy();
    expect(
      within(ordered).getByText(/1 different title you watched/),
    ).toBeTruthy();
  });

  it("expands the rest of a source's recorded returns behind a 'Show N more', keeping nothing hidden", async () => {
    const data = okTrace();
    const query = data.trace.gathers?.[0]?.sources?.[0]?.queries?.[0];
    if (!query) throw new Error("fixture must have a seed query");
    // 8 recorded returns, all recorded (total === length): 6 preview + 2 behind the expander, and NO
    // "not recorded" tail — everything the trace holds must be reachable.
    query.returned = Array.from({ length: 8 }, (_, i) => ({
      tmdb_id: 2000 + i,
      title: `Return ${i}`,
      fate: "kept" as const,
    }));
    query.total = 8;
    render(<TraceView data={data} />);
    const searched = screen
      .getByText(/Where we searched/)
      .closest("section") as HTMLElement;
    await userEvent.click(
      within(searched).getByText(/Follow it title by title/),
    );
    // The 2 titles past the 6-preview live behind an expander whose label counts them exactly.
    expect(within(searched).getByText(/Show 2 more/)).toBeTruthy();
    // Both the previewed and the collapsed titles are in the DOM — nothing recorded is hidden for good.
    expect(within(searched).getByText("Return 0")).toBeTruthy();
    expect(within(searched).getByText("Return 6")).toBeTruthy();
    expect(within(searched).getByText("Return 7")).toBeTruthy();
    // Nothing was dropped beyond the cap, so there's no "not recorded" tail.
    expect(
      within(searched).queryByText(/not recorded in the trace/),
    ).toBeNull();
  });

  it("marks titles beyond the recording cap honestly as 'not recorded', separate from the expander", async () => {
    const data = okTrace();
    const query = data.trace.gathers?.[0]?.sources?.[0]?.queries?.[0];
    if (!query) throw new Error("fixture must have a seed query");
    // Source returned 30 but only 8 were recorded: the 22 beyond the cap are honestly flagged.
    query.returned = Array.from({ length: 8 }, (_, i) => ({
      tmdb_id: 3000 + i,
      title: `Rec ${i}`,
      fate: "kept" as const,
    }));
    query.total = 30;
    render(<TraceView data={data} />);
    const searched = screen
      .getByText(/Where we searched/)
      .closest("section") as HTMLElement;
    await userEvent.click(
      within(searched).getByText(/Follow it title by title/),
    );
    expect(
      within(searched).getByText(
        /\+22 more returned \(not recorded in the trace\)/,
      ),
    ).toBeTruthy();
  });

  it("shows which AI-proposed titles made the shortlist vs fell out, per the proposal fates", () => {
    const data = okTrace();
    const gather = data.trace.gathers?.[0];
    if (!gather) throw new Error("fixture must have a gather");
    gather.sources = [
      ...(gather.sources ?? []),
      { source: "llm_web", status: "ok", contributed: 2, detail: "" },
    ];
    gather.web = {
      mode: "exa",
      searches: [],
      proposed: ["Kept Film", "Dropped Film", "Made Up"],
      resolved: ["Kept Film", "Dropped Film"],
      unresolved: ["Made Up"],
      proposals: [
        { title: "Kept Film", tmdb_id: 800, media: "movie", fate: "kept" },
        {
          title: "Dropped Film",
          tmdb_id: 801,
          media: "movie",
          fate: "already_watched",
        },
      ],
    };
    render(<TraceView data={data} />);
    const searched = screen
      .getByText(/Where we searched/)
      .closest("section") as HTMLElement;
    // The kept proposal reports it made the shortlist; the dropped one shows its reason; the
    // hallucination is struck through (no TMDB match).
    expect(
      within(searched).getByText(/1 of these made this library’s shortlist/),
    ).toBeTruthy();
    const droppedBadge = within(searched)
      .getByText("Dropped Film")
      .closest("li") as HTMLElement;
    expect(within(droppedBadge).getByText(/already watched/)).toBeTruthy();
    // The hallucination (no TMDB match) is struck through — the class sits on the badge wrapper.
    const hallucinated = within(searched)
      .getByText("Made Up")
      .closest("div") as HTMLElement;
    expect(hallucinated.className).toContain("line-through");
  });

  it("renders a cold-start user's flow — 'Popular titles', no ranking step, and a delivered ending", () => {
    // A cold user files a history stage (no seeds) + a synthetic cold_start gather. The flow must be
    // cold-aware: the search step becomes "Popular titles", the ranking step is omitted (no taste
    // ranking runs), and the tab still reaches a delivered ending — this is the Cassie bug's fix.
    const data = okTrace({
      status: "cold_start",
      trace: {
        history: {
          total: 1,
          recent: [],
          watched_movies: 1,
          watched_shows: 0,
          watched_by_library: { Movies: { movie: 1, show: 0 } },
        },
        seeds: [],
        gathers: [
          {
            pool: "movie · cold_start",
            sources: [
              {
                source: "cold_start",
                status: "ok",
                contributed: 1,
                detail: "",
              },
            ],
          },
        ],
      },
    });
    render(<TraceView data={data} />);
    // The cold search step, not "Where we searched".
    expect(screen.getByText(/What we pulled for Movies/)).toBeTruthy();
    expect(screen.queryByText(/Where we searched/)).toBeNull();
    // No taste-ranking step for a cold start.
    expect(screen.queryByText(/How we ordered the shortlist/)).toBeNull();
    // The cold path explains the highest-rated fallback, not "most-watched" (both the step subtitle
    // and the source card say so — the copy self-corrected from "most-watched", which cold start isn't).
    expect(
      screen.getAllByText(/highest-rated titles on this server/).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText(/most-watched/)).toBeNull();
    // Still reaches a delivered ending (the pick from the shared fixture).
    expect(screen.getByText(/What we put in Movies/)).toBeTruthy();
  });

  it("overlays the request outcome onto a 'not in your libraries' drop", async () => {
    const data = okTrace();
    const query = data.trace.gathers?.[0]?.sources?.[0]?.queries?.[0];
    if (!query) throw new Error("fixture must have a seed query");
    query.returned = [
      { tmdb_id: 1000, title: "Toy Story 5", fate: "not_in_your_libraries" },
      { tmdb_id: 1001, title: "Hoppers", fate: "not_in_your_libraries" },
    ];
    data.requests = {
      "1000:movie": {
        status: "sent",
        detail: "",
        arr_slug: "toy-story-5",
        excluded: false,
      },
      "1001:movie": {
        status: "pending",
        detail: "",
        arr_slug: null,
        excluded: false,
      },
    };
    render(<TraceView data={data} />);
    const searched = screen
      .getByText(/Where we searched/)
      .closest("section") as HTMLElement;
    await userEvent.click(
      within(searched).getByText(/Follow it title by title/),
    );
    expect(
      within(searched).getByText(/requested from Sonarr\/Radarr/),
    ).toBeTruthy();
    expect(within(searched).getByText(/queued for your approval/)).toBeTruthy();
  });
});
