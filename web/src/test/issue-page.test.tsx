/**
 * "Have an issue?" page.
 *
 * The person driving this page cannot interpret a table, and the maintainer reading the result was
 * never on the call. So the assertions here are about the two things that actually decide whether
 * the feature works: does the page state its finding in a SENTENCE, and does the copy button put
 * the server's block on the clipboard verbatim.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type {
  SupportHealth,
  SupportRows,
  SupportStatus,
  SupportTitleLookup,
} from "@/lib/types";
import { IssuePage } from "@/pages/issue";

const {
  supportStatus,
  enableSupport,
  disableSupport,
  supportHealth,
  supportTitle,
  supportRows,
  supportLibraries,
  supportPerson,
  getSupportBundle,
  supportSuggestions,
  supportSurfaces,
  supportJobs,
  supportRowSchedule,
  supportClocks,
  supportTimeline,
} = vi.hoisted(() => ({
  supportStatus: vi.fn(),
  enableSupport: vi.fn(),
  disableSupport: vi.fn(),
  supportHealth: vi.fn(),
  supportTitle: vi.fn(),
  supportRows: vi.fn(),
  supportLibraries: vi.fn(),
  supportPerson: vi.fn(),
  getSupportBundle: vi.fn(),
  supportSuggestions: vi.fn(),
  supportSurfaces: vi.fn(),
  supportJobs: vi.fn(),
  supportRowSchedule: vi.fn(),
  supportClocks: vi.fn(),
  supportTimeline: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      supportStatus: () => supportStatus(),
      enableSupport: () => enableSupport(),
      disableSupport: () => disableSupport(),
      supportHealth: () => supportHealth(),
      supportTitle: (q: string) => supportTitle(q),
      supportRows: () => supportRows(),
      supportLibraries: () => supportLibraries(),
      supportPerson: (slug: string) => supportPerson(slug),
      supportBundleUrl: () => "/api/support/bundle.txt",
      supportReportZipUrl: () => "/api/support/report.zip",
      getSupportBundle: () => getSupportBundle(),
      supportSuggestions: () => supportSuggestions(),
      supportSurfaces: () => supportSurfaces(),
      supportJobs: () => supportJobs(),
      supportRowSchedule: () => supportRowSchedule(),
      supportClocks: () => supportClocks(),
      supportTimeline: (user: string) => supportTimeline(user),
    },
  };
});

const ON: SupportStatus = {
  enabled: true,
  expires_at: "2026-08-06T09:00:00+00:00",
  seconds_remaining: 23 * 3600 + 41 * 60,
};
const OFF: SupportStatus = {
  enabled: false,
  expires_at: null,
  seconds_remaining: 0,
};

const HEALTHY: SupportHealth = {
  checks: [
    { name: "Plex server", ok: true, detail: "1.43.3 · 42ms" },
    { name: "Database", ok: true, detail: "head 0059" },
  ],
  text: "=== Shortlist support · health ===\nOK\n=== end ===",
};

const TEACUP_BUG: SupportTitleLookup = {
  query: "Teacup",
  rows: [
    {
      user: "chris35352",
      title: "Teacup",
      tmdb_id: 226637,
      watched_record: false,
      media_type: "show",
      viewed_leaf_count: null,
      leaf_count: null,
      counts_as_watched: false,
      cap_pct: 0,
      cap_source: "global",
      delivered: [{ row: "picked", rank: 2, library: "TV Shows" }],
      problem: true,
    },
  ],
  flagged: ["chris35352"],
  flagged_detail: ["chris35352 (Teacup)"],
  capped: false,
  text: "=== Shortlist support · title lookup ===\nPROBLEM\n=== end ===",
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <IssuePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  supportStatus.mockResolvedValue(ON);
  enableSupport.mockResolvedValue(ON);
  disableSupport.mockResolvedValue(OFF);
  supportHealth.mockResolvedValue(HEALTHY);
  supportRows.mockResolvedValue({
    rows: [],
    global_watched_pct: 0,
    text: "rows",
  } satisfies SupportRows);
  supportLibraries.mockResolvedValue({ libraries: [], error: null, text: "l" });
  supportTitle.mockResolvedValue(TEACUP_BUG);
  supportSurfaces.mockResolvedValue({
    rows: [],
    on_owner_home: [],
    on_owner_shelf: [],
    unlabelled: [],
    owner_label: "shortlist_steve",
    error: null,
    text: "surfaces",
  });
  supportJobs.mockResolvedValue({ jobs: [], counts: {}, failed: 0, text: "j" });
  supportRowSchedule.mockResolvedValue({ rows: [], text: "s" });
  supportClocks.mockResolvedValue({
    tz: "Australia/Sydney",
    local_now: "2026-08-13T18:00:00+10:00",
    utc_now: "2026-08-13T08:00:00Z",
    offset_hours: 10,
    scheduled: [{ kind: "sync.users", at: "2026-08-14T03:30:00+10:00" }],
    text: "c",
  });
  supportTimeline.mockResolvedValue({ entries: [], text: "t" });
  supportSuggestions.mockResolvedValue({
    people: [
      { slug: "chris35352", display_name: "Chris", enabled: true },
      { slug: "svoiss", display_name: "svoiss", enabled: false },
    ],
    titles: ["Teacup", "Severance"],
  });
});

describe("IssuePage — the mode", () => {
  it("offers a single button to turn it on, and hides the tools until it is", async () => {
    supportStatus.mockResolvedValue(OFF);
    renderPage();

    expect(
      await screen.findByRole("button", { name: /switch on the checks/i }),
    ).toBeTruthy();
    // No tools while it's off — a wall of 403s would be the alternative.
    expect(screen.queryByText(/what's the problem/i)).toBeNull();
    expect(supportHealth).not.toHaveBeenCalled();
  });

  it("says when the mode will switch itself off, in plain words", async () => {
    renderPage();
    expect(
      await screen.findByText(/switch themselves off in 23h 41m/i),
    ).toBeTruthy();
  });

  it("turns the mode on when asked", async () => {
    supportStatus.mockResolvedValue(OFF);
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /switch on the checks/i }),
    );
    await waitFor(() => expect(enableSupport).toHaveBeenCalledTimes(1));
  });

  it("explains a failure to reach the server instead of rendering nothing", async () => {
    supportStatus.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(
      await screen.findByText(/could not reach the shortlist server/i),
    ).toBeTruthy();
  });
});

describe("IssuePage — the title check", () => {
  it("states the finding as a sentence, not just a table", async () => {
    // The whole point: the operator can't read a table, and the maintainer isn't here to read it
    // for them. The verdict has to name the person and say what's wrong.
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /keep seeing something/i }),
    );
    await userEvent.type(screen.getByLabelText(/title/i), "Teacup");
    await userEvent.click(screen.getByRole("button", { name: /^check$/i }));

    expect(
      await screen.findByText(
        /given even though the row is set to show nothing they've watched: chris35352 \(Teacup\)/i,
      ),
    ).toBeTruthy();
    expect(supportTitle).toHaveBeenCalledWith("Teacup");
  });

  it("copies the server's block verbatim rather than re-rendering the table", async () => {
    // The copy text is the deliverable, and it is rendered server-side so the format is decided in
    // one place. If the page ever built its own string, this breaks.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /keep seeing something/i }),
    );
    await userEvent.type(screen.getByLabelText(/title/i), "Teacup");
    await userEvent.click(screen.getByRole("button", { name: /^check$/i }));
    await screen.findByText(/chris35352 \(Teacup\)/i);

    const copyButtons = await screen.findAllByRole("button", {
      name: /copy for support/i,
    });
    // The last panel on the page is the title tool; health owns the first button.
    await userEvent.click(copyButtons[copyButtons.length - 1]!);

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(TEACUP_BUG.text),
    );
  });

  it("says nothing is wrong when nothing is wrong", async () => {
    supportTitle.mockResolvedValue({
      ...TEACUP_BUG,
      flagged: [],
      flagged_detail: [],
      rows: [
        { ...TEACUP_BUG.rows[0]!, problem: false, counts_as_watched: true },
      ],
    });
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /keep seeing something/i }),
    );
    await userEvent.type(screen.getByLabelText(/title/i), "Teacup");
    await userEvent.click(screen.getByRole("button", { name: /^check$/i }));

    expect(await screen.findByText(/nothing unexpected/i)).toBeTruthy();
  });

  it("tells the person what to do when the title matched nothing", async () => {
    supportTitle.mockResolvedValue({
      query: "Nope",
      rows: [],
      flagged: [],
      flagged_detail: [],
      capped: false,
      text: "No watched record and no delivery for this title, for anyone.",
    });
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /keep seeing something/i }),
    );
    await userEvent.type(screen.getByLabelText(/title/i), "Nope");
    await userEvent.click(screen.getByRole("button", { name: /^check$/i }));

    // The empty state is the SERVER's sentence, shown verbatim — the page never writes its own.
    expect(
      await screen.findByText(/no watched record and no delivery/i),
    ).toBeTruthy();
  });
});

describe("IssuePage — health", () => {
  it("summarises which checks need attention", async () => {
    supportHealth.mockResolvedValue({
      checks: [
        { name: "Plex server", ok: false, detail: "not connected" },
        { name: "Database", ok: true, detail: "head 0059" },
      ],
      text: "t",
    } satisfies SupportHealth);
    renderPage();

    expect(
      await screen.findByText(/1 of 2 checks need attention: plex server/i),
    ).toBeTruthy();
  });

  it("says so plainly when everything is fine", async () => {
    renderPage();
    expect(
      await screen.findByText(/everything shortlist depends on is working/i),
    ).toBeTruthy();
  });
});

describe("IssuePage — sending a long report", () => {
  it("offers the full file as a download, and says it is working while it builds", async () => {
    // It was a plain `<a download>`, so the browser fetched in silence while the server gathered
    // and zipped the logs — the button looked dead for the several seconds that takes.
    let release: (value: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(pending));

    renderPage();
    const button = await screen.findByRole("button", {
      name: /download everything \(with logs\)/i,
    });
    await userEvent.click(button);

    expect(await screen.findByRole("button", { name: /Preparing/i })).toBeTruthy();
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/support/report.zip"),
      expect.objectContaining({ credentials: "same-origin" }),
    );
    release(new Response(new Blob(["zip"])));
    vi.unstubAllGlobals();
  });
});

describe("IssuePage — every check is reachable", () => {
  it("lists every check behind one disclosure, not just the seven shortcuts", async () => {
    // The six problem cards are the front door; the rest still have to be reachable, or building
    // them was pointless.
    renderPage();
    const toggle = await screen.findByRole("button", {
      name: /show all 22 checks/i,
    });
    await userEvent.click(toggle);

    expect(
      screen.getByRole("button", { name: /ask plex directly, as one person/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /why is this NOT in their row/i }),
    ).toBeTruthy();
  });

  it("asks for a username before running a per-person check", async () => {
    // Running with an empty name would return every person's data under a heading naming one.
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /one person's recommendations look wrong/i,
      }),
    );

    expect(
      screen.getByRole("button", { name: /^check$/i }).hasAttribute("disabled"),
    ).toBe(true);
    expect(supportPerson).not.toHaveBeenCalled();

    await userEvent.type(screen.getByLabelText(/plex username/i), "chris35352");
    await userEvent.click(screen.getByRole("button", { name: /^check$/i }));

    await waitFor(() =>
      expect(supportPerson).toHaveBeenCalledWith("chris35352"),
    );
  });

  it("shows the exact text that will be copied, so a paste holds no surprises", async () => {
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /keep seeing something/i }),
    );
    await userEvent.type(screen.getByLabelText(/title/i), "Teacup");
    await userEvent.click(screen.getByRole("button", { name: /^check$/i }));

    expect(await screen.findByText(/this is exactly what/i)).toBeTruthy();
    // The server's block, rendered VERBATIM rather than rebuilt from the JSON. Matched on the
    // node's raw textContent because getByText normalises whitespace, which would hide exactly the
    // line-break differences this assertion exists to pin down.
    const pre = document.querySelector("pre");
    expect(pre?.textContent).toBe(TEACUP_BUG.text);
  });
});

describe("IssuePage — filing the report", () => {
  it("puts the bug link and the diagnostics on the same page as the checks", async () => {
    // They used to be two sidebar buttons, which asked the person to know that a report wants
    // diagnostics attached and that the other button is where they come from.
    renderPage();

    const link = await screen.findByRole("link", {
      name: /report a bug on github/i,
    });
    expect(link.getAttribute("href")).toContain("github.com");
    expect(
      screen.getByRole("button", { name: /copy the summary/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /download everything \(with logs\)/i }),
    ).toBeTruthy();
  });

  it("says what the report masks, what it keeps, and that masking is not a guarantee", async () => {
    // Someone is about to paste this into a public GitHub issue. "No passwords or tokens" was true
    // and misleading — it sat beside a button that publishes, and it said nothing about the fact
    // that the report names every person on the server. The absolute framing was then dropped
    // outright: three separate leaks reached a real report while that sentence was on screen, and a
    // promise the code cannot keep is what gets a report pasted unread.
    renderPage();

    expect(
      await screen.findByText(/passwords, tokens, api keys, ip addresses/i),
    ).toBeTruthy();
    expect(screen.getByText(/rather than a guarantee/i)).toBeTruthy();
    expect(
      screen.getByText(/skim before\s+posting anywhere public/i),
    ).toBeTruthy();
    expect(
      screen.getByText(/plex usernames of people on your server/i),
    ).toBeTruthy();
  });

  it("offers no name-hiding toggle, and says so rather than implying one", async () => {
    // Removed at the owner's request (2026-08-06): a person who wants names out can take them out,
    // and a tickbox that governed only the report — not the per-check Copy buttons beside it — read
    // like a page-wide privacy setting it never was. The copy now states plainly that names are in.
    renderPage();

    expect(
      await screen.findByText(/plex usernames of people on your server/i),
    ).toBeTruthy();
    // The teeth are here: no checkbox means the control is gone from the page. A companion
    // assertion on the download href used to sit below this, and could not fail — the href it read
    // came from this file's own `vi.mock` of `@/lib/api`, where `supportReportZipUrl` is a hardcoded
    // string, so it was checking the mock rather than the page. `api.ts` losing the parameter is
    // what actually matters, and that is a property of the un-mocked module.
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});

describe("IssuePage — the report copy can fail two ways", () => {
  // Ported from the old sidebar button. Both the FETCH (a 500 building the report) and the
  // CLIPBOARD write can fail, and a silently dead button is indistinguishable from success — the
  // person then pastes nothing into their issue and waits for a reply that never makes sense.
  beforeEach(() => {
    getSupportBundle.mockReset();
    Object.assign(navigator, { clipboard: { writeText: vi.fn() } });
  });

  it("copies the report on success", async () => {
    getSupportBundle.mockResolvedValue(
      "=== Shortlist support · full diagnostic ===",
    );
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /copy the summary/i }),
    );

    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        "=== Shortlist support · full diagnostic ===",
      ),
    );
    expect(await screen.findByText(/paste it into the issue/i)).toBeTruthy();
  });

  it("says so when the report itself could not be built", async () => {
    getSupportBundle.mockRejectedValue(new Error("500"));
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /copy the summary/i }),
    );

    expect(await screen.findByText(/use the download instead/i)).toBeTruthy();
  });

  it("says so when the browser blocks the clipboard write", async () => {
    getSupportBundle.mockResolvedValue("report");
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /copy the summary/i }),
    );

    expect(await screen.findByText(/use the download instead/i)).toBeTruthy();
  });
});

describe("IssuePage — the inputs help you get them right", () => {
  it("offers the real roster as you type, instead of asking you to remember a username", async () => {
    // A username typed from memory is the commonest way one of these checks comes back empty — and
    // empty is indistinguishable from "nothing is wrong", which is the worst answer a diagnostic
    // can give. Display name is shown as the option LABEL, but the value is the slug, because the
    // slug is what the API takes.
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /one person's recommendations look wrong/i,
      }),
    );

    const input = await screen.findByLabelText(/plex username/i);
    const listId = input.getAttribute("list");
    expect(listId).toBeTruthy();

    await waitFor(() =>
      expect(document.querySelectorAll(`#${listId} option`).length).toBe(2),
    );
    const options = [...document.querySelectorAll(`#${listId} option`)];
    expect(options.map((o) => o.getAttribute("value"))).toEqual([
      "chris35352",
      "svoiss",
    ]);
    expect(options[0]!.textContent).toContain("Chris");
  });

  it("warns before you submit when the name isn't anyone on the server", async () => {
    // Cheaper than a round trip that comes back empty and reads like a finding.
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /one person's recommendations look wrong/i,
      }),
    );
    await userEvent.type(
      await screen.findByLabelText(/plex username/i),
      "Chris",
    );

    // "Chris" is a display name, not the slug — the exact mistake this catches.
    expect(
      await screen.findByText(/no one on this server is called/i),
    ).toBeTruthy();
    expect(supportPerson).not.toHaveBeenCalled();
  });

  it("does not warn while the roster is still loading", async () => {
    // Flagging every name during the fetch would train people to ignore the warning.
    supportSuggestions.mockReturnValue(new Promise(() => {}));
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /one person's recommendations look wrong/i,
      }),
    );
    await userEvent.type(
      await screen.findByLabelText(/plex username/i),
      "anyone",
    );

    expect(screen.queryByText(/no one on this server is called/i)).toBeNull();
  });

  it("offers titles as you type too", async () => {
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /keep seeing something/i }),
    );

    const input = await screen.findByLabelText(/title/i);
    const listId = input.getAttribute("list");
    await waitFor(() =>
      expect(document.querySelectorAll(`#${listId} option`).length).toBe(2),
    );
  });
});

describe("IssuePage — an opened check appears where you clicked", () => {
  it("renders below the full list when opened from the full list", async () => {
    // Reported 2026-08-05: clicking a check in "Show all 19 checks" rendered its panel in the slot
    // ABOVE that list — off-screen upward — so the click looked like it had done nothing at all.
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /show all 22 checks/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /what did the ai do/i }),
    );

    const panel = await screen.findByRole("heading", {
      name: /what did the ai do/i,
      level: 2,
    });
    const grid = screen.getByRole("button", {
      name: /^hide all 22 checks$/i,
    });
    // DOCUMENT_POSITION_FOLLOWING: the panel comes after the disclosure in document order.
    expect(
      grid.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("renders directly below the shortcuts when opened from a shortcut", async () => {
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /keep seeing something/i }),
    );

    const panel = await screen.findByRole("heading", {
      name: /is this title counted as watched/i,
      level: 2,
    });
    const disclosure = screen.getByRole("button", {
      name: /show all 22 checks/i,
    });
    // The panel sits BEFORE the "show all" disclosure — i.e. next to the cards it was opened from.
    expect(
      disclosure.compareDocumentPosition(panel) &
        Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
  });

  it("closes when the same check is clicked again", async () => {
    renderPage();
    const card = await screen.findByRole("button", {
      name: /keep seeing something/i,
    });
    await userEvent.click(card);
    expect(
      await screen.findByRole("heading", {
        name: /is this title counted as watched/i,
        level: 2,
      }),
    ).toBeTruthy();

    await userEvent.click(card);

    expect(
      screen.queryByRole("heading", {
        name: /is this title counted as watched/i,
        level: 2,
      }),
    ).toBeNull();
  });
});

describe("IssuePage — a problem runs every check it promises", () => {
  // Three of the seven cards were writing cheques one check couldn't cash: the blurb named the
  // queue, the schedule AND the clocks, and the wiring opened the queue alone.
  it("opens all three checks behind 'rows aren't updating at all'", async () => {
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /rows aren't updating at all/i,
      }),
    );

    for (const heading of [
      /is background work stuck/i,
      /when does each row next rebuild/i,
      /are the clocks right/i,
    ]) {
      expect(
        await screen.findByRole("heading", { name: heading, level: 2 }),
      ).toBeTruthy();
    }
  });

  it("reaches the owner's own Home screen for a row-visibility question", async () => {
    // `sharing` reads the share filters that hide a row from other people. The OWNER has no share
    // filter (plex-safety rule 5), so nothing but the row's own promotion flag keeps someone
    // else's row off their Home — and only `surfaces` can see that. It existed on the server for
    // issue #75 and was wired to nothing, leaving this question answerable in half.
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /someone can see another person's row/i,
      }),
    );

    expect(
      await screen.findByRole("heading", {
        name: /who can see whose rows/i,
        level: 2,
      }),
    ).toBeTruthy();
    expect(
      await screen.findByRole("heading", {
        name: /where is each row actually showing/i,
        level: 2,
      }),
    ).toBeTruthy();
  });

  it("keeps earlier answers on screen when another check is opened", async () => {
    // A bug report takes three or four answers. Opening the second used to destroy the first, so
    // each had to be copied before moving on or it was gone.
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /show all 22 checks/i }),
    );
    await userEvent.click(
      screen.getByRole("button", {
        name: /which libraries can shortlist see/i,
      }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /which setting actually applied/i }),
    );

    expect(
      screen.getByRole("heading", {
        name: /which libraries can shortlist see/i,
        level: 2,
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", {
        name: /which setting actually applied/i,
        level: 2,
      }),
    ).toBeTruthy();
  });
});

describe("IssuePage — reporting without the checks switched on", () => {
  it("still offers the GitHub link, and says how to attach diagnostics", async () => {
    // The whole page used to render nothing below the toggle until Support Mode was on — including
    // the report section. Someone who only wanted to file a bug was shown a lone switch and no way
    // to report anything.
    supportStatus.mockResolvedValue(OFF);
    renderPage();

    const link = await screen.findByRole("link", {
      name: /report a bug on github/i,
    });
    expect(link.getAttribute("href")).toContain("github.com");
    expect(screen.getByText(/switch the checks on above first/i)).toBeTruthy();
    // The two buttons that read the GATED endpoints are absent rather than present and 403ing.
    expect(
      screen.queryByRole("button", { name: /copy the summary/i }),
    ).toBeNull();
  });
});

describe("IssuePage — every check states a verdict", () => {
  // The page's own promise is that each check answers in a SENTENCE, because the operator can't
  // read a table and the maintainer isn't there to read it for them. Ten of the checks had no
  // verdict case at all and rendered only the raw copy blob.
  it("says in words what the row settings check found", async () => {
    supportRows.mockResolvedValue({
      rows: [
        { slug: "picked", enabled: true },
        { slug: "faves", enabled: false },
      ],
      global_watched_pct: 0,
      text: "rows",
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /show all 22 checks/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /which setting actually applied/i }),
    );

    expect(await screen.findByText(/1 of 2 rows are on/i)).toBeTruthy();
  });

  it("flags a server where every row is switched off", async () => {
    supportRows.mockResolvedValue({
      rows: [{ slug: "picked", enabled: false }],
      global_watched_pct: 0,
      text: "rows",
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /show all 22 checks/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /which setting actually applied/i }),
    );

    expect(
      await screen.findByText(/all 1 rows are switched off/i),
    ).toBeTruthy();
  });
});

describe("IssuePage — the owner's own Home screen", () => {
  it("calls someone else's row on the owner's Home a bug, in words", async () => {
    // An INVARIANT, not a preference: no configuration makes that correct. This is the check the
    // page's highest-stakes question needed and could not reach.
    supportSurfaces.mockResolvedValue({
      rows: [],
      on_owner_home: [
        { title: "✨ Picked for Sarah", label: "shortlist_sarah" },
      ],
      on_owner_shelf: [],
      unlabelled: [],
      owner_label: "shortlist_steve",
      error: null,
      text: "surfaces",
    });
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /someone can see another person's row/i,
      }),
    );

    expect(await screen.findByText(/on your own Home screen/i)).toBeTruthy();
    expect(screen.getByText(/No setting makes that correct/i)).toBeTruthy();
  });

  it("explains the Recommended shelf as a Plex limitation, not a fault", async () => {
    // A CONSEQUENCE: the shelf is one flag per collection and the owner has no filter, so a row
    // shown on friends' library shelves lands on the owner's too. The fix is a setting, not code —
    // colouring it red would send someone hunting a bug that isn't there.
    supportSurfaces.mockResolvedValue({
      rows: [],
      on_owner_home: [],
      on_owner_shelf: [
        { title: "✨ Picked for Sarah", label: "shortlist_sarah" },
      ],
      unlabelled: [],
      owner_label: "shortlist_steve",
      error: null,
      text: "surfaces",
    });
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /someone can see another person's row/i,
      }),
    );

    expect(await screen.findByText(/that is a Plex limitation/i)).toBeTruthy();
  });
});

describe("IssuePage — the timeline can be narrowed to one person", () => {
  it("runs without a name, and passes one when given", async () => {
    // The server has always taken a person here; the page hardcoded "" and could never ask for one,
    // so half the endpoint was unreachable.
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", { name: /show all 22 checks/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /what has been happening/i }),
    );

    // The button is live with the field empty — the whole-server timeline is the common case.
    const run = screen.getByRole("button", { name: /^check$/i });
    expect(run.hasAttribute("disabled")).toBe(false);
    await userEvent.click(run);
    await waitFor(() => expect(supportTimeline).toHaveBeenCalledWith(""));

    await userEvent.type(screen.getByLabelText(/plex username/i), "chris35352");
    await userEvent.click(screen.getByRole("button", { name: /^check$/i }));
    await waitFor(() =>
      expect(supportTimeline).toHaveBeenCalledWith("chris35352"),
    );
  });
});

describe("IssuePage — the surfaces verdict cannot say all-clear over a real finding", () => {
  it("calls an UNLABELLED row what the server calls it: a bug everyone can see", async () => {
    // The worst finding this check has, and the banner was reading straight past it. No `label!=`
    // exclude can hide a row carrying no label, so it is visible to every person on the server —
    // and `sweep_broken_rows` deletes it as an orphan. The server prints "BUG:" in the copy text
    // below; the sentence above it was saying "Every row is showing exactly where it should".
    supportSurfaces.mockResolvedValue({
      rows: [
        {
          title: "✨ Picked for Sarah",
          label: "shortlist_sarah",
          own_home: false,
        },
        {
          title: "✨ Picked for Mike",
          label: "",
          marked: true,
          own_home: false,
        },
      ],
      on_owner_home: [],
      on_owner_shelf: [],
      unlabelled: [{ title: "✨ Picked for Mike", library: "Movies" }],
      owner_label: "shortlist_steve",
      error: null,
      text: "BUG: 1 collection(s) are ours but carry NO label.",
    });
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /someone can see another person's row/i,
      }),
    );

    // Matched on the VERDICT's own wording, not on "carry NO label" — that phrase also appears in
    // the server's copy blob below, which was never the thing at fault here.
    expect(
      await screen.findByText(/Nothing can hide an unlabelled row/i),
    ).toBeTruthy();
    expect(screen.queryByText(/showing exactly where it should/i)).toBeNull();
  });

  it("treats EVERY row reading unlabelled as a failed read, not a server full of leaks", async () => {
    // plex-safety rule 4's own lesson: if not one row reads as labelled, that is a label read that
    // failed. Reporting it as N separate leaks sends someone hunting rows that are probably fine.
    supportSurfaces.mockResolvedValue({
      rows: [
        { title: "A", label: "" },
        { title: "B", label: "" },
      ],
      on_owner_home: [],
      on_owner_shelf: [],
      unlabelled: [{ title: "A" }, { title: "B" }],
      owner_label: "shortlist_steve",
      error: null,
      text: "…",
    });
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /someone can see another person's row/i,
      }),
    );

    expect(
      await screen.findByText(/more likely to be a failed read/i),
    ).toBeTruthy();
  });

  it("refuses to call it clean when the rows could not be read at all", async () => {
    // `owned_row_surfaces` attaches an `error` to a row whose hub read raised and emits NO surface
    // flags for it — so a server where every read failed produced exactly the same empty
    // `on_owner_home` as a server where nothing was wrong. An empty read is not proof (rule 4).
    supportSurfaces.mockResolvedValue({
      rows: [
        { title: "A", label: "shortlist_a", error: "BadRequest: 500" },
        { title: "B", label: "shortlist_b", error: "BadRequest: 500" },
      ],
      on_owner_home: [],
      on_owner_shelf: [],
      unlabelled: [],
      owner_label: "shortlist_steve",
      error: null,
      text: "…",
    });
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /someone can see another person's row/i,
      }),
    );

    expect(
      await screen.findByText(/could not be read from Plex/i),
    ).toBeTruthy();
    expect(screen.queryByText(/showing exactly where it should/i)).toBeNull();
  });

  it("still says all-clear when the reads succeeded and nothing is wrong", async () => {
    renderPage();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /someone can see another person's row/i,
      }),
    );

    expect(
      await screen.findByText(/showing exactly where it should/i),
    ).toBeTruthy();
  });
});
