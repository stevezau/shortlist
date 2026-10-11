import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { TraceView } from "@/pages/run-user-trace";
import type {
  RunUserTraceResponse,
  TraceRatings,
  TraceSelection,
  TraceWatch,
} from "@/lib/types";

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
    requests: {},
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
            rating_key: 0,
            title: "Toy Story 2",
            reason: "Because you loved Toy Story",
            media_type: "movie",
            seed_title: null,
            sources: [],
            affinity: null,
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

  it("says a tab's count is every row's titles in that library, not one row's", () => {
    render(<TraceView data={okTrace()} />);
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveAttribute("title", expect.stringMatching(/delivered .* every row/));
    }
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

  it("reconciles the two counts on a source card, which count different things", () => {
    // "It added 2 titles to the pool" and "1 made the shortlist · 1 dropped" have DIFFERENT
    // denominators: `contributed` is net-new after dedup, kept/dropped covers everything the source
    // returned including titles another source had already added. Both right; nothing said so, and
    // the comment in the code recorded a real person being confused by it (audit, Sep 2026).
    render(<TraceView data={okTrace()} />);

    expect(screen.getByText(/added 2 new titles to the pool/i)).toBeTruthy();
    expect(
      screen.getByText(
        /Counting everything it returned, including titles another source found first/i,
      ),
    ).toBeTruthy();
  });

  it("writes the discover genres as a sentence, with no media-type token in it", () => {
    render(
      <TraceView
        data={okTrace({
          trace: {
            ...okTrace().trace,
            gathers: [
              {
                pool: "movie · tmdb_discover",
                discover_genres: { movie: ["Drama", "Thriller"], show: [] },
                sources: [
                  {
                    source: "tmdb_discover",
                    status: "ok",
                    contributed: 4,
                    detail: "",
                  },
                ],
              },
            ],
          },
        } as Partial<RunUserTraceResponse>)}
      />,
    );

    expect(
      screen.getByText(
        /The genres they watch most — movies: Drama, Thriller\./,
      ),
    ).toBeTruthy();
    // The media type with nothing to report is DROPPED, not printed as "Show — none".
    expect(screen.queryByText(/— none/)).toBeNull();
  });

  it("says plainly when no genre stands out, rather than printing 'Movie — none'", () => {
    render(
      <TraceView
        data={okTrace({
          trace: {
            ...okTrace().trace,
            gathers: [
              {
                pool: "movie · tmdb_discover",
                discover_genres: { movie: [] },
                sources: [
                  {
                    source: "tmdb_discover",
                    status: "ok",
                    contributed: 4,
                    detail: "",
                  },
                ],
              },
            ],
          },
        } as Partial<RunUserTraceResponse>)}
      />,
    );

    expect(screen.getByText(/No genre stands out/i)).toBeTruthy();
    expect(screen.queryByText(/Movie — none/)).toBeNull();
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

  it("tags favourite and older searches with their kind, and says how the picks were split", () => {
    const data = okTrace();
    const gather = data.trace.gathers?.[0];
    if (!gather) throw new Error("fixture must have a gather");
    gather.sources = [
      ...(gather.sources ?? []),
      { source: "llm_web", status: "ok", contributed: 3, detail: "" },
    ];
    gather.web = {
      mode: "exa",
      searches: [
        { seed: "Toy Story", query: "movies like Toy Story", cached: false, returned: [], kind: "recent" },
        { seed: "Heat", query: "movies like Heat", cached: false, returned: [], kind: "favourite" },
        { seed: "Alien", query: "movies like Alien", cached: true, returned: [], kind: "older" },
      ],
      shares: { recent: 2, favourite: 1, older: 1 },
    };
    render(<TraceView data={data} />);
    const searched = screen.getByText(/Where we searched/).closest("section") as HTMLElement;
    expect(within(searched).getByText("favourite")).toBeTruthy();
    expect(within(searched).getByText("older")).toBeTruthy();
    expect(within(searched).queryByText("recent")).toBeNull();
    expect(within(searched).getByText("Picks asked for: 2 recent, 1 favourites, 1 older")).toBeTruthy();
  });

  it("shows no kind chip or shares line on a trace recorded before the history mix", () => {
    const data = okTrace();
    const gather = data.trace.gathers?.[0];
    if (!gather) throw new Error("fixture must have a gather");
    gather.sources = [...(gather.sources ?? []), { source: "llm_web", status: "ok", contributed: 1, detail: "" }];
    gather.web = {
      mode: "exa",
      searches: [{ seed: "Toy Story", query: "movies like Toy Story", cached: false, returned: [] }],
    };
    render(<TraceView data={data} />);
    expect(screen.queryByText(/Picks asked for/)).toBeNull();
    expect(screen.queryByText("favourite")).toBeNull();
  });

  it("names the titles whose web search failed, so a thin web result is not mistaken for a quiet one", () => {
    const data = okTrace();
    const gather = data.trace.gathers?.[0];
    if (!gather) throw new Error("fixture must have a gather");
    gather.sources = [
      ...(gather.sources ?? []),
      { source: "llm_web", status: "ok", contributed: 0, detail: "" },
    ];
    gather.web = {
      mode: "exa",
      searches: [],
      failed_seeds: ["Dune", "Arrival"],
    };
    render(<TraceView data={data} />);
    const searched = screen
      .getByText(/Where we searched/)
      .closest("section") as HTMLElement;
    expect(
      within(searched).getByText(/2 searches failed and were skipped/),
    ).toBeTruthy();
    expect(within(searched).getByText(/Dune, Arrival/)).toBeTruthy();
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
    // ranking runs), and the tab still reaches a delivered ending — this is the fix for a tab stuck before delivery.
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

  it("a skipped person sees the reason, not a 'predates tracing' excuse", () => {
    // A cold-start person whose rows are set to skip has a reason and no per-library stages. The
    // EmptyState below the banner blamed a legacy run for what was a deliberate skip — two
    // explanations for one absence, one of them wrong.
    const data = okTrace({
      status: "cold_start",
      reason: "Not enough watch history yet — 0 of 10 titles.",
      trace: {},
    }) as RunUserTraceResponse;

    render(<TraceView data={data} />);

    expect(screen.getByText(/not enough watch history yet/i)).toBeTruthy();
    expect(screen.queryByText(/predates library-level tracing/i)).toBeNull();
  });

  it("does not call a delivered person 'skipped' just because they carry a reason", () => {
    // The regression this pins: `reason` used to mean "nothing was built for this person", and the
    // engine now also sets it on an `ok` person to say why their rows hold what they held last
    // night. On the second run of any night that is most of the roster, and every one of their
    // trace pages said "Skipped this person" above a full, correct delivery trace.
    const data = okTrace({
      status: "ok",
      reason:
        "It wasn't any of their rows' night to rebuild, so last run's titles were redelivered unchanged.",
    });

    render(<TraceView data={data} />);

    expect(screen.getByText(/night to rebuild/i)).toBeTruthy();
    expect(screen.queryByText(/skipped this person/i)).toBeNull();
    // The trace itself must still be there — it is the whole point of the page.
    expect(screen.getByText(/Where we searched/)).toBeTruthy();
  });

  it("still renders the library tabs when a reason coexists with stages", () => {
    // The other half of the same ternary: suppressing the EmptyState must not suppress the tabs.
    const data = okTrace({ reason: "Heads up about this run" });

    render(<TraceView data={data} />);

    expect(screen.getByText(/heads up about this run/i)).toBeTruthy();
    expect(screen.getByText(/Where we searched/)).toBeTruthy();
  });
});

/** The rating policy line. Every "nothing was dropped" cell needs its own wording: from the outcome
 *  alone, a switched-off feature, a person who rates nothing, and an account whose ratings are all
 *  tool-written are the same silence — and only one of those means the feature is working. */
describe("TraceView · what Plex ratings did", () => {
  const policy = (patch: Partial<TraceRatings> = {}): TraceRatings => ({
    enabled: true,
    threshold: 2,
    trusted: true,
    blocked: 0,
    rated: 6,
    rated_human: 6,
    ...patch,
  });

  function withRatings(
    ratings: TraceRatings,
    recent: TraceWatch[] = [],
  ): RunUserTraceResponse {
    const base = okTrace();
    return {
      ...base,
      trace: {
        ...base.trace,
        history: { ...base.trace.history!, recent, ratings },
      },
    };
  }

  const ratedOut: TraceWatch[] = [
    {
      title: "The Room",
      media: "movie",
      library: "Movies",
      year: 2003,
      watched_at: null,
      rating: 2,
      rating_blocked: true,
    },
  ];

  it("says the setting was off rather than implying a clean run", () => {
    render(
      <TraceView
        data={withRatings(policy({ enabled: false, threshold: null }))}
      />,
    );

    expect(screen.getByText(/Plex ratings are off for this run/)).toBeTruthy();
  });

  it("names the threshold and the count when ratings actually dropped titles", () => {
    render(
      <TraceView
        data={withRatings(
          policy({ blocked: 3, rated: 9, rated_human: 9 }),
          ratedOut,
        )}
      />,
    );

    expect(
      screen.getByText(
        /Across everything they.ve watched, 3 titles they rated 1★ or lower can.t be used as seeds/,
      ),
    ).toBeTruthy();
    // The badge list is this library's visible subset — a different scope, so it must not repeat the
    // number, and the sentence must not read as a claim about the tab it happens to be sitting on.
    expect(
      screen.getByText(/Dropped, among the watches shown here/),
    ).toBeTruthy();
    expect(screen.getByText(/The Room \(2003\) · 1★/)).toBeTruthy();
  });

  it("scopes its count to the whole account, not the library tab it renders in", () => {
    // The number is account-wide and the badges are one library's recent sample. On a TV tab with no
    // dropped shows in view, an unscoped "3 titles were dropped" reads as three SHOWS — sending
    // whoever is asking "why is that show missing" after an answer that was never about TV.
    const base = okTrace();
    const data: RunUserTraceResponse = {
      ...base,
      trace: {
        ...base.trace,
        history: {
          ...base.trace.history!,
          recent: ratedOut,
          watched_by_library: {
            Movies: { movie: 18, show: 0 },
            "TV Shows": { movie: 0, show: 9 },
          },
          ratings: policy({ blocked: 3, rated: 9, rated_human: 9 }),
        },
        seeds: [
          ...(base.trace.seeds ?? []),
          {
            title: "The Pitt",
            media: "show",
            library: "TV Shows",
            tmdb_id: 1,
            weight: 1,
          },
        ],
      },
    };

    render(<TraceView data={data} />);
    fireEvent.click(screen.getByRole("tab", { name: /TV Shows/ }));

    expect(screen.getByText(/Across everything they.ve watched/)).toBeTruthy();
    // ...and no movie's badge leaks onto the TV tab to be read as a dropped show.
    expect(screen.queryByText(/The Room/)).toBeNull();
  });

  it("distinguishes 'on, nothing low enough' from 'on, nothing rated at all'", () => {
    const { unmount } = render(<TraceView data={withRatings(policy())} />);
    expect(
      screen.getByText(/none of their 6 ratings are 1★ or lower/),
    ).toBeTruthy();
    unmount();

    render(
      <TraceView data={withRatings(policy({ rated: 0, rated_human: 0 }))} />,
    );
    expect(screen.getByText(/they haven.t rated anything/)).toBeTruthy();
  });

  it("counts only the ratings a person could have typed, and owns up to the rest", () => {
    // A PARTLY tool-written account stays trusted, so the fractional values are skipped one by one
    // while the account looks healthy. Counting them would let the line speak for a 0.75★ rating that
    // nothing ever looked at — the same silent no-op this summary exists to expose.
    render(
      <TraceView data={withRatings(policy({ rated: 10, rated_human: 9 }))} />,
    );

    expect(
      screen.getByText(/none of their 9 ratings are 1★ or lower/),
    ).toBeTruthy();
    expect(
      screen.getByText(/One more rating looks tool-written and wasn.t counted/),
    ).toBeTruthy();
  });

  it("says so when every rating on the account was written by a tool", () => {
    render(
      <TraceView data={withRatings(policy({ rated: 3, rated_human: 0 }))} />,
    );

    expect(
      screen.getByText(/none of their 3 ratings were typed in Plex/),
    ).toBeTruthy();
  });

  it("says a tool-managed account was disbelieved — the silent no-op this exists for", () => {
    render(
      <TraceView
        data={withRatings(
          policy({ trusted: false, rated: 40, rated_human: 4 }),
        )}
      />,
    );

    expect(
      screen.getByText(/another tool is writing ratings on this account/),
    ).toBeTruthy();
    // It must not also claim the feature was doing its job.
    expect(screen.queryByText(/can.t be used as seeds/)).toBeNull();
  });

  it("invents no policy for a run recorded before one was traced", () => {
    const base = okTrace();
    const data: RunUserTraceResponse = {
      ...base,
      trace: {
        ...base.trace,
        history: { ...base.trace.history!, recent: ratedOut },
      },
    };

    render(<TraceView data={data} />);

    expect(screen.queryByText(/Plex ratings are/)).toBeNull();
    // ...but the old list still explains the gap in the seeds, exactly as it always did.
    expect(
      screen.getByText(/Not used as seeds — they rated these low in Plex/),
    ).toBeTruthy();
  });
});

describe("TraceView — the flow explains freshness, the cut and release date", () => {
  /** The trace's `selection` entry for the Movies library, which okTrace()'s tabs land on. */
  const entry = (patch: Record<string, unknown> = {}) => ({
    row: "picked",
    library: "Movies",
    decision: "rebuilt" as const,
    size: 15,
    delivered: 15,
    candidates: 62,
    // `candidates` is what the cut LEFT, so for one media type it can never exceed the cap.
    cut_cap: 80,
    carried: 0,
    new: 15,
    refresh_night: true,
    rebuild_every_days: 8,
    recency: 0.5,
    watched_pct: 0,
    pick_order: "best",
    rewatch: false,
    unstarted_only: false,
    ...patch,
  });

  const withSelection = (patch: Record<string, unknown> = {}) =>
    okTrace({
      trace: { ...okTrace().trace, selection: [entry(patch)] },
    } as Partial<RunUserTraceResponse>);

  it("tells the owner a row was NOT re-picked, in the delivered step", () => {
    // The question the whole section exists for: "I changed a setting and nothing moved." It belongs
    // beside what was delivered, because that is the thing the owner is looking at and doubting.
    render(
      <TraceView
        data={withSelection({
          decision: "carried_forward",
          refresh_night: false,
        })}
      />,
    );
    expect(screen.getByText(/not re-picked tonight/i)).toBeInTheDocument();
    expect(screen.getByText(/titles refresh every 8 days/i)).toBeInTheDocument();
    // Names the control that EXISTS, and the right direction — the cadence is a day count now, so
    // you lower it to rebuild sooner. This asserted "Raise Freshness" for a control that was gone.
    expect(
      screen.getByText(/Lower .Titles refresh every./i),
    ).toBeInTheDocument();
  });

  it("says a row was held because nobody watched anything, and until when", () => {
    // The other half of "nothing moved": not "it wasn't due" but "it WAS due and we held it". Without
    // naming the reason this is indistinguishable from the cadence line above, and the owner goes
    // looking for a rebuild setting that is not the one holding their row.
    render(
      <TraceView
        data={withSelection({
          decision: "held_idle",
          refresh_night: true,
          idle_hold_days: 28,
        })}
      />,
    );
    expect(
      screen.getByText(/haven't watched anything since it was built/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/28 days/i)).toBeInTheDocument();
  });

  it("says what a watch-it-again row was built from, and what the cooldown held back", () => {
    // "Why is my rewatch row topped up with new titles?" is answered by these two numbers.
    render(
      <TraceView
        data={withSelection({
          decision: "rebuilt",
          rewatch: true,
          rewatches: 3,
          cooling: 2,
          rewatch_cooldown_days: 30,
        })}
      />,
    );
    expect(screen.getByText(/3 titles they've finished/i)).toBeInTheDocument();
    expect(screen.getByText(/2 finished in the last 30 days/i)).toBeInTheDocument();
  });

  it("names a settings change as the reason a row rebuilt early", () => {
    render(
      <TraceView data={withSelection({ decision: "settings_changed" })} />,
    );
    expect(
      screen.getByText(/a setting that decides its titles changed/i),
    ).toBeInTheDocument();
  });

  it("names a changed watch as the reason a named row was rebuilt, not refreshed", () => {
    // A "Because you watched X" row whose watch changed is rebuilt from scratch. Shown as a refresh
    // ("the strongest picks stayed"), a nightly full rebuild looked like the normal cadence.
    render(<TraceView data={withSelection({ decision: "seed_moved" })} />);
    expect(
      screen.getByText(/the watch it was named after changed/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/strongest picks stayed/i)).not.toBeInTheDocument();
  });

  it("has a shortlisted step showing the cut", () => {
    // Between search and order, because that is where it happens: the cut decides what can be
    // ordered at all, so explaining ordering without it describes half the mechanism.
    render(<TraceView data={withSelection()} />);
    expect(
      screen.getByText(/62 titles made the shortlist here/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/at most 80 per media type/i),
    ).toBeInTheDocument();
  });

  it("names a row the way the owner does, not by its slug", () => {
    // `TraceSelection.row` is the SLUG the engine writes, and both these lines lead with it in
    // bold — so a row configured as "✨ {library_name} Picked for You" introduced itself as
    // **picked** mid-sentence (audit finding, Sep 2026).
    render(
      <TraceView
        data={withSelection()}
        rowNames={{ picked: "✨ Picked for You" }}
      />,
    );

    expect(screen.getAllByText("✨ Picked for You").length).toBeGreaterThan(0);
    expect(screen.queryByText("picked")).toBeNull();
  });

  it("fills {library_name} with the library the section is about", () => {
    // Each of these sections is one library's story, so the token has exactly one honest value
    // here; stripping it read "📬 you asked for — their own requests…".
    render(
      <TraceView
        data={withSelection()}
        rowNames={{ picked: "✨ {library_name} Picked for You" }}
      />,
    );

    expect(screen.getAllByText("✨ Movies Picked for You").length).toBeGreaterThan(0);
    expect(screen.queryByText(/\{library_name\}/)).toBeNull();
    expect(screen.queryByText("✨ Picked for You")).toBeNull();
  });

  it("falls back to the slug for a row that no longer exists", () => {
    // A deleted row is not in the collections list, and a blank lead-in would be worse than a slug.
    render(<TraceView data={withSelection()} rowNames={{}} />);

    expect(screen.getAllByText("picked").length).toBeGreaterThan(0);
  });

  it("says release date applied to the CUT, not merely to the order", () => {
    render(<TraceView data={withSelection()} />);
    expect(
      screen.getByText(/Release date counted for 50%/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/applied to the cut itself, not just the order/i),
    ).toBeInTheDocument();
  });

  it("says release date was ignored rather than showing 0%", () => {
    render(<TraceView data={withSelection({ recency: 0 })} />);
    expect(screen.getByText(/Release date was ignored/i)).toBeInTheDocument();
  });

  it("adds no shortlisted step for a run recorded before this existed", () => {
    // Absent must read as "not recorded", never as a stage with empty numbers.
    render(<TraceView data={okTrace()} />);
    expect(screen.queryByText(/made the shortlist here/i)).not.toBeInTheDocument();
  });
});

describe("TraceView — why a title won or lost", () => {
  /** One movie gather with a single returned title, built explicitly so the test states the shape
   *  it depends on instead of reaching into okTrace()'s nesting to patch it. */
  const withReturn = (patch: Record<string, unknown>) => {
    const base = okTrace();
    return okTrace({
      trace: {
        ...base.trace,
        gathers: [
          {
            pool: "movie · tmdb_similar",
            sources: [
              {
                source: "tmdb_similar",
                status: "ok" as const,
                contributed: 1,
                detail: "",
                queries: [
                  {
                    seed: "Toy Story",
                    media: "movie",
                    total: 1,
                    returned: [
                      {
                        tmdb_id: 863,
                        title: "Toy Story 2",
                        fate: "kept" as const,
                        ...patch,
                      },
                    ],
                  },
                ],
              },
            ],
          },
        ],
      },
    } as Partial<RunUserTraceResponse>);
  };

  it("shows a returned title's year, so an era complaint is checkable", () => {
    render(<TraceView data={withReturn({ year: 1999, rating: 8.6 })} />);
    expect(screen.getByText("(1999)")).toBeInTheDocument();
  });

  it("shows the score it was judged on", () => {
    render(<TraceView data={withReturn({ year: 1999, rating: 8.6 })} />);
    expect(screen.getByText("8.6")).toBeInTheDocument();
  });

  it("shows the release-date multiplier that was applied to it", () => {
    // The number that answers "why did the older one win?" — without it a fate is an outcome with
    // no reasoning attached.
    render(
      <TraceView
        data={withReturn({ year: 1994, rating: 8.6, age_weight: 0.27 })}
      />,
    );
    expect(screen.getByText(/age ×0\.27/)).toBeInTheDocument();
  });

  it("omits the multiplier when release date was not consulted", () => {
    // "×1.0" is the absence of information dressed as information.
    render(
      <TraceView
        data={withReturn({ year: 1994, rating: 8.6, age_weight: 1 })}
      />,
    );
    expect(screen.queryByText(/age ×/)).not.toBeInTheDocument();
  });

  it("renders a legacy return that carries none of it", () => {
    render(<TraceView data={okTrace()} />);
    expect(screen.queryByText(/age ×/)).not.toBeInTheDocument();
  });
});

describe("TraceView for a shared row", () => {
  // A shared row belongs to nobody, so every "they / their" in this view is wrong for it — and it
  // records no per-person history stage at all, by design.
  const sharedData = {
    username: "👥 Popular Movies on Home Server",
    display_name: "👥 Popular Movies on Home Server",
    status: "ok",
    error: null,
    reason: null,
    requests: {},
    trace: { gathers: [{ library: "Movies", media: "movie", sources: [] }] },
    breakdown: [],
  } as unknown as RunUserTraceResponse;

  it("drops the person framing and names the row as the run page does", () => {
    render(
      <TraceView data={sharedData} rowName="👥 Popular {library_name} on Home Server" sharedRow />,
    );

    expect(
      screen.getByRole("heading", { name: /How we picked for 👥 Popular on Home Server/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/for this shared row/i)).toBeInTheDocument();
    expect(screen.queryByText(/for this person/i)).not.toBeInTheDocument();
  });
});

describe("TraceView — a Your requests row", () => {
  /** What the engine records for a requests row: one `selection` entry per library, carrying every
   *  request it looked at and what became of each. No seeds, no gathers — nothing was searched. */
  const requestsTrace = (
    patch: Partial<TraceSelection> = {},
    extraSelection: TraceSelection[] = [],
  ) =>
    okTrace({
      status: "ok",
      trace: {
        history: {
          total: 1,
          recent: [],
          watched_movies: 1,
          watched_shows: 0,
          watched_by_library: { Movies: { movie: 1, show: 0 } },
        },
        seeds: [],
        gathers: [],
        selection: [
          {
            row: "your-requests",
            library: "Movies",
            decision: "requests",
            size: 15,
            delivered: 1,
            candidates: 3,
            pick_order: "newest",
            requests: [
              {
                tmdb_id: 863,
                media_type: "movie",
                title: "Toy Story 2",
                asked_at: "2026-09-01T10:00:00Z",
                landed_at: "2026-09-03T10:00:00Z",
                found_in: ["overseerr"],
                result: "in_row",
              },
              {
                tmdb_id: 920,
                media_type: "movie",
                title: "Cars",
                asked_at: "2026-05-01T10:00:00Z",
                landed_at: "2026-05-02T10:00:00Z",
                found_in: ["overseerr", "tag"],
                result: "too_old",
              },
              {
                tmdb_id: 10193,
                media_type: "movie",
                title: "Toy Story 3",
                asked_at: null,
                landed_at: null,
                found_in: ["tag"],
                result: "not_on_plex",
              },
            ],
            ...patch,
          },
          ...extraSelection,
        ],
      },
      breakdown: okTrace().breakdown.map((b) => ({
        ...b,
        row_slug: "your-requests",
        row_title: "Your requests",
      })),
    });

  it("renders one 'What they asked for' step in place of the watched/searched/ordered steps", () => {
    render(<TraceView data={requestsTrace()} />);
    expect(screen.getByText("What they asked for")).toBeInTheDocument();
    expect(screen.getByText("3 requests looked at")).toBeInTheDocument();
    // Nothing was searched or ranked for this row, so those steps would explain a run that never
    // happened.
    expect(screen.queryByText(/What they watched recently/)).toBeNull();
    expect(screen.queryByText(/Where we searched/)).toBeNull();
    expect(screen.queryByText(/What survived/)).toBeNull();
    expect(screen.queryByText(/How we ordered the shortlist/)).toBeNull();
    // Still ends where every flow ends.
    expect(screen.getByText(/What we put in Movies/)).toBeInTheDocument();
  });

  it("counts one request in the singular", () => {
    const one = requestsTrace().trace!.selection![0]!.requests![0]!;
    render(<TraceView data={requestsTrace({ candidates: 1, delivered: 1, requests: [one] })} />);
    expect(screen.getByText("1 request looked at")).toBeInTheDocument();
  });

  it("keeps each date on one line, so a narrow screen scrolls the table instead of stacking a date", () => {
    render(<TraceView data={requestsTrace()} />);
    // The title is also in the delivered list below, so start from the table's cell.
    const cells = within(
      screen.getByRole("cell", { name: "Toy Story 2" }).closest("tr")!,
    ).getAllByRole("cell");
    expect(cells[1]).toHaveClass("whitespace-nowrap");
    expect(cells[2]).toHaveClass("whitespace-nowrap");
  });

  it("lists every request with where it was found and what became of it", () => {
    render(<TraceView data={requestsTrace()} />);
    const step = screen
      .getByText("What they asked for")
      .closest("section") as HTMLElement;
    const rows = within(step).getAllByRole("row");
    // Header + three requests.
    expect(rows).toHaveLength(4);
    const row = (i: number) => within(rows[i] as HTMLElement);
    expect(row(1).getByText("Toy Story 2")).toBeInTheDocument();
    expect(row(1).getByText("Overseerr")).toBeInTheDocument();
    expect(row(1).getByText("In the row")).toBeInTheDocument();
    expect(row(2).getByText("Overseerr, tag")).toBeInTheDocument();
    expect(row(2).getByText("Landed too long ago")).toBeInTheDocument();
    expect(row(3).getByText("tag")).toBeInTheDocument();
    expect(row(3).getByText("Not on Plex yet")).toBeInTheDocument();
    // A request with no dates shows a dash, not "Invalid Date".
    expect(row(3).getAllByText("—")).toHaveLength(2);
  });

  it("names the row's window in days when the row's settings are known", () => {
    render(
      <TraceView
        data={requestsTrace()}
        rowWindows={{ "your-requests": 60 }}
      />,
    );
    expect(
      screen.getByText("Landed more than 60 days ago"),
    ).toBeInTheDocument();
  });

  it("prefers the window the run itself recorded over the row's current setting", () => {
    // The row may have been edited since the night the verdict was reached; the trace entry carries
    // the setting that actually dropped the title.
    render(
      <TraceView
        data={requestsTrace({ requests_window_days: 30 })}
        rowWindows={{ "your-requests": 60 }}
      />,
    );
    expect(
      screen.getByText("Landed more than 30 days ago"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/60 days/)).toBeNull();
  });

  it("keeps the recommendation steps when the library also holds a picked row", () => {
    render(
      <TraceView
        data={requestsTrace({}, [
          {
            row: "picked",
            library: "Movies",
            decision: "rebuilt",
            size: 15,
            delivered: 15,
            candidates: 62,
          },
        ])}
      />,
    );
    expect(screen.getByText("What they asked for")).toBeInTheDocument();
    expect(screen.getByText(/What they watched recently/)).toBeInTheDocument();
    expect(screen.getByText(/How we ordered the shortlist/)).toBeInTheDocument();
  });
});


describe("Trace seed action feedback", () => {
  it("reports a rejected seed change and permits a successful retry with the same seed", async () => {
    const block = vi.spyOn(api, "blockSeed").mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ blocked_seeds: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><TraceView data={okTrace()} userId={7} /></QueryClientProvider>);
    expect(screen.queryByRole("button", { name: "Don’t seed" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit seeds" }));
    await userEvent.click(screen.getByRole("button", { name: "Don’t seed" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t block this seed");
    expect(screen.queryByRole("button", { name: "Seed blocked" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Seed blocked" })).toBeDisabled();
    expect(block).toHaveBeenLastCalledWith(7, { tmdbId: 862, title: "Toy Story", mediaType: "movie" });
    block.mockRestore();
  });
});


describe("Trace step navigation", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  function layout(pickerHeight = 57) {
    let searchedTop = 900;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const picker = this.tagName === "LABEL" && this.querySelector('[aria-label="Trace step"]');
      const top = picker ? 64 : this.id === "Movies-searched" ? searchedTop : this.id === "Movies-watched" ? -200 : 2000;
      const height = picker ? pickerHeight : 500;
      return { top, bottom: top + height, left: 0, right: 358, width: 358, height, x: 0, y: top, toJSON() {} };
    });
    const getStyle = window.getComputedStyle.bind(window);
    vi.spyOn(window, "getComputedStyle").mockImplementation((element) => {
      const style = getStyle(element);
      if (element.tagName === "LABEL" && element.querySelector('[aria-label="Trace step"]')) Object.defineProperty(style, "top", { value: "64px", configurable: true });
      return style;
    });
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    return { scroll, setSearchedTop: (top: number) => { searchedTop = top; } };
  }

  it("keeps the destination below both sticky bars, focuses its heading and marks the section", () => {
    const { scroll } = layout();
    render(<TraceView data={okTrace()} />);
    fireEvent.change(screen.getByRole("combobox", { name: "Trace step" }), { target: { value: "Movies-searched" } });
    const heading = screen.getByRole("heading", { name: /Where we searched/ });
    expect(scroll).toHaveBeenLastCalledWith({ top: 767, behavior: "smooth" });
    expect(heading).toHaveFocus();
    expect(heading.closest("section")).toHaveAttribute("data-navigation-highlight", "true");
  });

  it("measures a taller step picker and respects reduced motion", () => {
    const { scroll } = layout(90);
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    render(<TraceView data={okTrace()} />);
    fireEvent.change(screen.getByRole("combobox", { name: "Trace step" }), { target: { value: "Movies-searched" } });
    expect(scroll).toHaveBeenLastCalledWith({ top: 734, behavior: "instant" });
  });
  it("tracks the heading below the measured picker when scrolling between steps", () => {
    const { setSearchedTop } = layout();
    render(<TraceView data={okTrace()} />);
    setSearchedTop(133);
    fireEvent.scroll(window);
    expect(screen.getByRole("combobox", { name: "Trace step" })).toHaveValue("Movies-searched");
    setSearchedTop(900);
    fireEvent.scroll(window);
    expect(screen.getByRole("combobox", { name: "Trace step" })).toHaveValue("Movies-watched");
  });

});

describe("TraceView — the stage counts", () => {
  /** A movie library that ran a full flow: 3 seeds, 2 sources × 3 searches, a 10-title shortlist and
   *  2 titles delivered. The numbers are the engine's own shape: `candidates` is the shortlist AFTER
   *  the cut, so it never exceeds `cut_cap` for a single media type. */
  function funnelTrace(): RunUserTraceResponse {
    const base = okTrace();
    const library = base.breakdown[0];
    const pick = library?.picks[0];
    if (!library || !pick) throw new Error("okTrace() must deliver one pick");
    const seed = (title: string, tmdb_id: number, recency_days: number) => ({
      title,
      media: "movie",
      library: "Movies",
      tmdb_id,
      weight: 1,
      watch_count: 1,
      recency_days,
    });
    const source = (name: string) => ({
      source: name,
      status: "ok",
      contributed: 12,
      detail: "",
      searched: { movie: 3 },
      queries: [],
    });
    return {
      ...base,
      trace: {
        ...base.trace,
        seeds: [seed("Toy Story", 862, 0), seed("Up", 14160, 0), seed("Coco", 354912, 4)],
        gathers: [{ pool: "movie · tmdb_similar, trakt", sources: [source("tmdb_similar"), source("trakt")] }],
        selection: [
          {
            row: "picked",
            library: "Movies",
            decision: "rebuilt",
            size: 2,
            delivered: 2,
            candidates: 10,
            cut_cap: 80,
            carried: 0,
            new: 2,
            refresh_night: true,
            rebuild_every_days: 8,
            recency: 0,
            watched_pct: 0,
            pick_order: "best",
          },
        ],
      },
      breakdown: [
        {
          ...library,
          picks: [
            { ...pick, rank: 1 },
            { ...pick, rank: 2, title: "Cars" },
          ],
        },
      ],
    } as RunUserTraceResponse;
  }

  function railCounts(): Record<string, string> {
    const rail = screen.getByRole("navigation", { name: "Steps" });
    return Object.fromEntries(
      within(rail)
        .getAllByRole("link")
        .map((link) => {
          const label = link.querySelector("[data-rail-label]")?.textContent ?? "";
          const count = link.querySelector("[data-rail-count]")?.textContent ?? "";
          return [label, count];
        }),
    );
  }

  it("never grows from the shortlist to delivery, and names what every count counts", () => {
    render(<TraceView data={funnelTrace()} />);

    const counts = railCounts();
    // The two input stages count seeds and searches, so they say so: a bare "6" beside a title
    // count read as six titles found.
    expect(counts["Watched recently"]).toBe("3 seeds");
    expect(counts["Searched"]).toBe("6 searches");
    // From the shortlist on, every stage counts titles, drawn from the same selection entries.
    const titles = ["Shortlisted", "Ordered", "Delivered"].map((stage) => {
      const match = /^(\d+) titles?$/.exec(counts[stage] ?? "");
      if (!match) throw new Error(`${stage} should count titles, got "${counts[stage]}"`);
      return Number(match[1]);
    });
    expect(titles).toEqual([10, 10, 2]);
    for (let i = 1; i < titles.length; i++) expect(titles[i]).toBeLessThanOrEqual(titles[i - 1] ?? 0);
  });

  it("says the shortlist count is what the cut left, never 'the strongest 80 kept' of 10", () => {
    render(<TraceView data={funnelTrace()} />);

    expect(screen.getByText(/10 titles made the shortlist here/)).toBeInTheDocument();
    expect(screen.getByText(/at most 80 per media type/)).toBeInTheDocument();
    expect(screen.queryByText(/strongest 80/)).not.toBeInTheDocument();
  });

  it("says 'watched most recently' once, not on every seed watched that day", () => {
    render(<TraceView data={funnelTrace()} />);

    expect(screen.getAllByText("watched most recently")).toHaveLength(1);
    expect(screen.getByText("4 days ago")).toBeInTheDocument();
  });
});
