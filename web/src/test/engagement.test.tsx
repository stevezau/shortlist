import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NeedsALook } from "@/components/dashboard/engagement";
import type { EffectivenessReport, EngagementReport } from "@/lib/types";

const getEngagement = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { getEngagement: (...a: unknown[]) => getEngagement(...a) },
}));

const ENGAGEMENT: EngagementReport = {
  window: "30",
  observed: true,
  people: [
    {
      username: "alex",
      display_name: "Alex",
      total: 2,
      picks: [
        {
          title: "Bailed Early",
          row: "Picked",
          media_type: "movie",
          outcome: "bounced",
          percent: 3,
          watched_at: "2026-08-22T10:00:00+00:00",
          finished_at: null,
          observed_at: "2026-08-22T10:00:00+00:00",
        },
        {
          title: "Saw It Out",
          row: "Picked",
          media_type: "movie",
          outcome: "finished",
          percent: 100,
          watched_at: "2026-08-21T10:00:00+00:00",
          finished_at: "2026-08-21T12:00:00+00:00",
          observed_at: "2026-08-21T10:00:00+00:00",
        },
      ],
    },
    {
      username: "quinn",
      display_name: "Quinn",
      total: 1,
      picks: [
        {
          title: "Most Of It",
          row: "Picked",
          media_type: "movie",
          outcome: "dropped",
          percent: 70,
          watched_at: "2026-08-20T10:00:00+00:00",
          finished_at: null,
          observed_at: "2026-08-20T10:00:00+00:00",
        },
      ],
    },
  ],
  losing: [],
  stop_points: [],
};

function report(over: Partial<EffectivenessReport> = {}): EffectivenessReport {
  return {
    // `users_idle` deliberately NOT equal to `users_with_picks - users_watched`. It was 10/4/6,
    // where the subtraction gives the same 6 — so the card could derive the number instead of
    // reading it and nothing failed, which is the exact regression the comment in `engagement.tsx`
    // says that field exists to prevent (the two populations are differently scoped). 10 - 4 = 6,
    // but the API says 7, and the API is right.
    coverage: { users_with_picks: 10, users_watched: 4, users_idle: 7 },
    per_row: [],
    requests: { sent: 0, pending: 0, watched_after_sent: 0 },
    // `overall` was absent entirely, though the fixture is cast to `EffectivenessReport`, which has
    // it. The card reads `overall.dropped`/`overall.bounced` to tell "nobody gave up" from "the only
    // give-ups were too short to list" — a distinction it cannot make against an undefined.
    overall: { dropped: 0, bounced: 0 },
    ...over,
  } as unknown as EffectivenessReport;
}

function renderPanel(r = report()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <NeedsALook report={r} reportWindow="30" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getEngagement.mockReset();
  getEngagement.mockResolvedValue(ENGAGEMENT);
});

describe("NeedsALook", () => {
  it("leads with the people who got picks and watched nothing", async () => {
    // The biggest silent failure on a server: rows delivered, nobody looked. Nothing else on the
    // dashboard says it — every other figure counts what people DID.
    renderPanel();

    expect(
      await screen.findByText(/got picks and watched none/),
    ).toBeInTheDocument();
    // 7, the API's own figure — NOT `users_with_picks - users_watched`, which is 6 here. The two used
    // to be equal in this fixture, which is what let the card derive it instead of reading it.
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("says so plainly when more than half the server ignored their row", async () => {
    // "6 of 10" and "9 of 10" are different problems: the second is not a picking problem at all.
    renderPanel(
      report({
        coverage: { users_with_picks: 10, users_watched: 1, users_idle: 9 },
      } as never),
    );

    // Behind the (i) now — present and reachable, not printed under the finding. The idle item is
    // the first problem in the list, so its control is the first one.
    const why = await screen.findAllByRole("button", { name: /why/i });
    await userEvent.click(why[0]!);
    expect(await screen.findByText(/More than half/)).toBeInTheDocument();
  });

  it("names a row that delivered and landed nothing", async () => {
    renderPanel(
      report({
        per_row: [
          {
            slug: "dud",
            library: "Movies",
            name: "Dud Row",
            delivered: 400,
            watched: 0,
            finished: 0,
            deleted: false,
          },
          {
            slug: "ok",
            library: "Movies",
            name: "Fine Row",
            delivered: 400,
            watched: 9,
            finished: 9,
            deleted: false,
          },
        ],
      } as never),
    );

    expect(await screen.findByText("Dud Row")).toBeInTheDocument();
    expect(screen.queryByText("Fine Row")).not.toBeInTheDocument();
  });

  it("ignores a row too small to conclude anything from", async () => {
    // Three picks and no watches is a quiet week, not a broken row.
    renderPanel(
      report({
        per_row: [
          {
            slug: "tiny",
            library: "Movies",
            name: "Tiny Row",
            delivered: 3,
            watched: 0,
            finished: 0,
            deleted: false,
          },
        ],
      } as never),
    );

    await screen.findByText(/got picks and watched none/);
    expect(screen.queryByText("Tiny Row")).not.toBeInTheDocument();
  });

  it("leaves a sub-5% bounce out of the findings entirely", async () => {
    // One to three minutes of a film cannot tell a wrong pick from a mis-click, a scrub, or a client
    // that autoplayed. On a real 47-user server 48 of 124 unfinished picks were under 5% — and with
    // the old ascending sort they were the ONLY ones ever shown, under a warning triangle with the
    // strongest negative claim on the page attached to the weakest data it has.
    //
    // They stay counted in the "gave up part-way" tile. They are not findings.
    renderPanel();

    await screen.findByText("Most Of It");
    expect(screen.queryByText("Bailed Early")).not.toBeInTheDocument();
  });

  it("puts the biggest loss first among real abandonments", async () => {
    const dropped = ENGAGEMENT.people[1]!.picks[0]!;
    getEngagement.mockResolvedValue({
      ...ENGAGEMENT,
      people: [
        {
          ...ENGAGEMENT.people[1]!,
          picks: [
            { ...dropped, title: "Half Way", percent: 45 },
            { ...dropped, title: "Nearly Done", percent: 80 },
          ],
        },
      ],
    });
    renderPanel();

    await screen.findByText("Nearly Done");
    const items = Array.from(document.querySelectorAll("li")).map(
      (li) => li.textContent ?? "",
    );
    const most = items.findIndex((t) => t.includes("Nearly Done"));
    const half = items.findIndex((t) => t.includes("Half Way"));
    expect(most).toBeGreaterThan(-1);
    expect(most).toBeLessThan(half);
  });

  it("says nothing is wrong rather than rendering an empty list", async () => {
    getEngagement.mockResolvedValue({ ...ENGAGEMENT, people: [] });
    renderPanel(
      report({
        coverage: { users_with_picks: 4, users_watched: 4, users_idle: 0 },
      } as never),
    );

    expect(await screen.findByText(/no row came up empty/)).toBeInTheDocument();
    expect(document.querySelectorAll("li").length).toBe(0);
  });

  it("flags titles fetched for people that nobody watched", async () => {
    renderPanel(
      report({
        requests: { sent: 34, pending: 53, watched_after_sent: 0 },
      } as never),
    );

    expect(
      await screen.findByText(/titles were fetched for people and none/),
    ).toBeInTheDocument();
    expect(screen.getByText("34")).toBeInTheDocument();
  });

  it("stays quiet about requests once one has been watched", async () => {
    renderPanel(
      report({
        requests: { sent: 34, pending: 0, watched_after_sent: 1 },
      } as never),
    );

    await screen.findByText(/got picks and watched none/);
    expect(screen.queryByText(/titles were fetched/)).toBeNull();
  });

  it("ignores a handful of requests, which prove nothing either way", async () => {
    renderPanel(
      report({
        requests: { sent: 2, pending: 0, watched_after_sent: 0 },
      } as never),
    );

    await screen.findByText(/got picks and watched none/);
    expect(screen.queryByText(/titles were fetched/)).toBeNull();
  });
});

describe("NeedsALook — saying why", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getEngagement.mockResolvedValue(ENGAGEMENT);
  });

  it("says when the app is willing to call something a give-up", async () => {
    // The rule is invisible otherwise, and "gave up" is the strongest negative claim on the page.
    renderPanel();

    // Behind an (i) now, not printed under every line — the findings are scanned, the reasoning is
    // consulted. It must still be REACHABLE, and by click rather than hover, because a hover-only
    // explanation does not exist on a phone.
    expect(screen.queryByText(/Only films, and only after/)).toBeNull(); // collapsed by default

    // Scoped to the GIVE-UP line — several findings each carry their own control, and the first
    // belongs to the idle-people item.
    const line = (await screen.findAllByText(/gave up on/))[0]!.closest("li")!;
    const why = within(line).getByRole("button", { name: /why/i });
    await userEvent.click(why);

    const hint = within(line).getByText(/Only films, and only after/);
    expect(hint.textContent).toMatch(/24h with no further play/);
    expect(hint.textContent).toMatch(/clock restarts/);
    expect(hint.textContent).toMatch(/series is never counted/);
    expect(why.getAttribute("aria-expanded")).toBe("true");
  });
});

describe("NeedsALook — the thresholds it acts on", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getEngagement.mockResolvedValue({ ...ENGAGEMENT, people: [] });
  });

  /** Coverage with the fields this card reads overridden; the rest kept whole. */
  const coverage = (over: Record<string, number>) =>
    ({
      users_enabled: 10,
      users_total: 10,
      users_with_picks: 10,
      users_watched: 4,
      users_idle: 7,
      users_watched_delta: 0,
      rows_enabled: 1,
      ...over,
    }) as unknown as EffectivenessReport["coverage"];

  const row = (over: Record<string, unknown> = {}) => ({
    slug: "r",
    section_key: "1",
    library: "Movies",
    name: "Dud Row",
    deleted: false,
    delivered: 100,
    watched: 0,
    finished: 0,
    ...over,
  });

  it("flags a row at exactly the delivery floor, and not one below it", async () => {
    // The gate is `delivered >= 20`. It survived being moved anywhere between 4 and 400 because the
    // fixtures only ever used 3 and 400 — so nothing pinned where the line actually is.
    renderPanel(report({ per_row: [row({ delivered: 20 })] }));
    expect(await screen.findByText(/delivered 20 picks/)).toBeTruthy();

    cleanup();
    renderPanel(report({ coverage: coverage({ users_idle: 0 }), per_row: [row({ delivered: 19 })] }));
    expect(await screen.findByText(/no row came up empty/i)).toBeTruthy();
    expect(screen.queryByText(/delivered 19 picks/)).toBeNull();
  });

  it("only says 'none were watched' when none were", async () => {
    // `watched === 0` survived being loosened to `<= 8`, which puts a FALSE STATEMENT on screen: a
    // row that landed 8 picks announced as having landed none.
    renderPanel(report({ coverage: coverage({ users_idle: 0 }), per_row: [row({ watched: 1 })] }));

    expect(await screen.findByText(/no row came up empty/i)).toBeTruthy();
    expect(screen.queryByText(/none were watched/)).toBeNull();
  });

  it("does not nag about a row the owner already deleted", async () => {
    renderPanel(report({ coverage: coverage({ users_idle: 0 }), per_row: [row({ deleted: true })] }));

    expect(await screen.findByText(/no row came up empty/i)).toBeTruthy();
    expect(screen.queryByText(/Dud Row/)).toBeNull();
  });

  it("lists up to three dead rows, not one", async () => {
    const rows = [1, 2, 3, 4].map((n) => row({ slug: `r${n}`, name: `Dud ${n}` }));
    renderPanel(report({ per_row: rows }));

    expect(await screen.findByText(/Dud 1/)).toBeTruthy();
    expect(screen.getByText(/Dud 2/)).toBeTruthy();
    expect(screen.getByText(/Dud 3/)).toBeTruthy();
    expect(screen.queryByText(/Dud 4/)).toBeNull(); // bounded at three
  });

  it("keeps the 'more than half' hint for more than half, and withholds it otherwise", async () => {
    // Forcing the hint on survived: every fixture reaching this line already had idle >= half, so
    // nothing ever asserted its ABSENCE.
    renderPanel(report({ coverage: coverage({ users_watched: 8, users_idle: 2 }) }));

    expect(await screen.findByText(/got picks and watched none/)).toBeTruthy();
    // No (i) at all when there is no hint to give — the control must not appear for its own sake.
    expect(screen.queryByRole("button", { name: /why/i })).toBeNull();
  });

  it("reports a single idle person rather than rounding them away", async () => {
    renderPanel(report({ coverage: coverage({ users_watched: 9, users_idle: 1 }) }));

    expect(await screen.findByText(/got picks and watched none/)).toBeTruthy();
  });

  it("needs five sent requests before calling them unwatched", async () => {
    const requests = { sent: 5, pending: 0, watched_after_sent: 0 };
    renderPanel(report({ requests }));
    expect(await screen.findByText(/titles were fetched/)).toBeTruthy();

    cleanup();
    renderPanel(report({ coverage: coverage({ users_idle: 0 }), requests: { ...requests, sent: 4 } }));
    expect(await screen.findByText(/no row came up empty/i)).toBeTruthy();
    expect(screen.queryByText(/titles were fetched/)).toBeNull();
  });
});

/** The two cards count different sets now, so they must not claim the same thing. */
describe("NeedsALook agrees with the Verdict card", () => {
  it("explains the give-ups rather than claiming everyone watched something", async () => {
    // The verdict tile totals EVERY abandonment; this list leaves out the ones under 5%. On a day
    // whose only give-ups were bounces, a bare "everyone watched something" sat directly under
    // "N gave up part-way" and read as one of the two being wrong.
    getEngagement.mockResolvedValue({ ...ENGAGEMENT, people: [] });
    renderPanel(
      report({
        coverage: { users_with_picks: 4, users_watched: 4, users_idle: 0 },
        overall: { dropped: 0, bounced: 3 },
      } as never),
    );

    expect(await screen.findByText(/all under 5% in/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/everyone who got a pick watched something/i),
    ).not.toBeInTheDocument();
  });

  it("still says everyone watched something when there were no give-ups at all", async () => {
    getEngagement.mockResolvedValue({ ...ENGAGEMENT, people: [] });
    renderPanel(
      report({
        coverage: { users_with_picks: 4, users_watched: 4, users_idle: 0 },
        overall: { dropped: 0, bounced: 0 },
      } as never),
    );

    expect(
      await screen.findByText(/everyone who got a pick watched something/i),
    ).toBeInTheDocument();
  });
});
