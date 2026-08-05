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
      getSupportBundle: () => getSupportBundle(),
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
    expect(await screen.findByText(/no watched record and no delivery/i)).toBeTruthy();
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
  it("offers the full file as a download for when a paste gets truncated", async () => {
    renderPage();
    const link = await screen.findByRole("link", {
      name: /download it as a file/i,
    });
    expect(link.getAttribute("href")).toBe("/api/support/bundle.txt");
    expect(link.getAttribute("download")).toBe("shortlist-report.txt");
  });
});

describe("IssuePage — every check is reachable", () => {
  it("lists all nineteen checks behind one disclosure, not just the six shortcuts", async () => {
    // The six problem cards are the front door; the rest still have to be reachable, or building
    // them was pointless.
    renderPage();
    const toggle = await screen.findByRole("button", { name: /show all 19 checks/i });
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
      await screen.findByRole("button", { name: /one person's recommendations look wrong/i }),
    );

    expect(screen.getByRole("button", { name: /^check$/i }).hasAttribute("disabled")).toBe(true);
    expect(supportPerson).not.toHaveBeenCalled();

    await userEvent.type(screen.getByLabelText(/plex username/i), "chris35352");
    await userEvent.click(screen.getByRole("button", { name: /^check$/i }));

    await waitFor(() => expect(supportPerson).toHaveBeenCalledWith("chris35352"));
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

    const link = await screen.findByRole("link", { name: /report a bug on github/i });
    expect(link.getAttribute("href")).toContain("github.com");
    expect(screen.getByRole("button", { name: /copy the full report/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /download it as a file/i })).toBeTruthy();
  });

  it("says outright that the report carries no secrets", async () => {
    // Someone is about to paste this into a public GitHub issue.
    renderPage();
    expect(
      await screen.findByText(/no passwords, tokens or api keys/i),
    ).toBeTruthy();
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
    getSupportBundle.mockResolvedValue("=== Shortlist support · full diagnostic ===");
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /copy the full report/i }),
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
      await screen.findByRole("button", { name: /copy the full report/i }),
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
      await screen.findByRole("button", { name: /copy the full report/i }),
    );

    expect(await screen.findByText(/use the download instead/i)).toBeTruthy();
  });
});
