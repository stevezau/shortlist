import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { AccountPrivacy, PrivacyStatus, Settings } from "@/lib/types";
import { SharingPage } from "@/pages/sharing";

const { getPrivacyStatus, getSettings, putSettings, startRun } = vi.hoisted(() => ({
  getPrivacyStatus: vi.fn(),
  getSettings: vi.fn<() => Promise<Settings>>(),
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
  startRun: vi.fn((_body: unknown) => Promise.resolve({ run_id: 512 })),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getPrivacyStatus: () => getPrivacyStatus(),
      getSettings: () => getSettings(),
      putSettings: (values: Settings) => putSettings(values),
      startRun: (body: unknown) => startRun(body),
    },
  };
});

function account(overrides: Partial<AccountPrivacy> = {}): AccountPrivacy {
  return {
    user: "sarah",
    display_name: "Sarah",
    slug: "sarah",
    account_id: 1000,
    user_id: 7,
    user_type: "shared",
    restriction_profile: "",
    manage_sharing: true,
    state: "hiding",
    hides: ["shortlist_mike"],
    should_hide: ["shortlist_mike"],
    missing: [],
    other_conditions: [],
    ...overrides,
  };
}

function status(overrides: Partial<PrivacyStatus> = {}): PrivacyStatus {
  return {
    read_at: "2026-09-05T02:00:00+00:00",
    summary: "clean",
    accounts: [account()],
    rows_on_plex: ["shortlist_mike"],
    snapshots_kept: 2,
    rows_error: null,
    error: null,
    enforcement: {
      measured: true,
      run_id: 418,
      measured_at: "2026-09-05T01:00:00+00:00",
      not_enforced: {},
      unhideable: {},
      unhideable_measured: false,
      unhideable_run_id: null,
      unhideable_measured_at: null,
    },
    ...overrides,
  };
}

function Where() {
  return <output aria-label="Address">{useLocation().pathname}</output>;
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/privacy"]}>
        <Routes>
          <Route path="/privacy" element={<SharingPage />} />
          <Route path="/runs/:id" element={<p>Run page</p>} />
        </Routes>
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const UNMEASURED = {
  measured: false,
  run_id: null,
  measured_at: null,
  not_enforced: {},
  unhideable: {},
  unhideable_measured: false,
  unhideable_run_id: null,
  unhideable_measured_at: null,
};

beforeEach(() => {
  getPrivacyStatus.mockReset();
  getPrivacyStatus.mockResolvedValue(status());
  getSettings.mockReset();
  getSettings.mockResolvedValue({});
  putSettings.mockClear();
  startRun.mockClear();
});

describe("the sharing page's four states", () => {
  it("shows a skeleton while the read is in flight", () => {
    getPrivacyStatus.mockReturnValue(new Promise(() => {}));

    const { container } = renderPage();

    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(
      0,
    );
  });

  it("offers a retry when the read fails outright", async () => {
    getPrivacyStatus.mockRejectedValue(new Error("boom"));

    renderPage();

    expect(
      await screen.findByRole("button", { name: /try again/i }),
    ).toBeVisible();
  });

  it("explains an empty roster instead of showing a bare table", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({ accounts: [], rows_on_plex: [] }),
    );

    renderPage();

    expect(await screen.findByText(/no plex accounts to check/i)).toBeVisible();
  });

  it("reports a healthy server as a grid of hidden cells, with the reading's time", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Who sees what" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "mike’s rows" })).toBeVisible();
    expect(screen.getByText("Hidden")).toBeVisible();
    expect(screen.getByText(/read from plex.tv \d/i)).toBeVisible();
    expect(screen.queryByText(/every account hides/i)).toBeNull();
  });

  it("puts each account's own row, other people's rows and the owner's view in the right cells", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        rows_on_plex: ["shortlist_mike", "shortlist_sarah"],
        accounts: [
          account({ display_name: "Steve", account_id: 9, state: "owner", user_type: "owner", hides: [], missing: [] }),
          account({ hides: ["shortlist_mike"], should_hide: ["shortlist_mike"] }),
        ],
      }),
    );
    renderPage();

    const sarah = (await screen.findByRole("link", { name: "Sarah" })).closest("tr") as HTMLElement;
    expect(within(sarah).getByText("Own")).toBeVisible();
    expect(within(sarah).getByText("Hidden")).toBeVisible();
    const steve = screen.getByText("Steve").closest("tr") as HTMLElement;
    expect(within(steve).getAllByText("Sees all")).toHaveLength(2);
  });
});

describe("what the page refuses to claim", () => {
  it("a failed plex.tv read renders as 'not current', never as hidden", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        error: "TimeoutError: plex.tv timed out",
        accounts: [],
        summary: "unreadable",
      }),
    );

    renderPage();

    expect(await screen.findByText(/nothing below is current/i)).toBeVisible();
    expect(screen.queryByText(/hides all/i)).toBeNull();
    // The account-state claim, not the strip's "Accounts hiding every row" label, which names a count.
    expect(screen.queryByText(/hiding every row —/i)).toBeNull();
  });

  it("a failed PMS row read says there is nothing to check against", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        rows_error: "ConnectionError: PMS down",
        rows_on_plex: [],
        summary: "rows_unknown",
        accounts: [account({ state: "unknown" })],
      }),
    );

    renderPage();

    expect(
      await screen.findByText(/nothing to check the filters against/i),
    ).toBeVisible();
    // The account-state claim, not the strip's "Accounts hiding every row" label, which names a count.
    expect(screen.queryByText(/hiding every row —/i)).toBeNull();
  });


  it("a missing hide rule says what the account sees, in rows, on its own line", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "missing",
        rows_on_plex: ["shortlist_mike", "shortlist_dan"],
        accounts: [
          account({
            state: "missing",
            hides: [],
            missing: ["shortlist_mike", "shortlist_dan"],
            should_hide: ["shortlist_mike", "shortlist_dan"],
          }),
        ],
      }),
    );

    renderPage();

    expect(await screen.findAllByText("Sees it")).toHaveLength(2);
    expect(screen.getByText(/missing hide rules/i)).toBeVisible();
    expect(screen.getByText(/next run merges them back in/i)).toBeVisible();
  });

  it("the owner line explains the Plex limitation instead of showing a fault", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        accounts: [
          account({ display_name: "Steve", state: "owner", user_type: "owner", hides: [], missing: [] }),
          account({ account_id: 2 }),
        ],
      }),
    );

    renderPage();

    expect(await screen.findByText("Sees all")).toBeVisible();
    expect(screen.getByText("Plex limit", { selector: "span.rounded-full" })).toBeVisible();
    expect(screen.queryByText(/missing hide rules/i)).toBeNull();
  });

  it("a left-alone account reads as a setting, not a failure", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({ accounts: [account({ state: "left_alone", manage_sharing: false, hides: [] })] }),
    );

    renderPage();

    expect(await screen.findByText(/asked shortlist to leave this account.s plex sharing alone/i)).toBeVisible();
    expect(screen.getByText("Left alone")).toBeVisible();
  });

  it("an account Plex can't read a filter for names the fix, and never promises the next run", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "filter_unreadable",
        accounts: [account({ state: "unreadable_filter", hides: [], missing: ["shortlist_mike"] })],
      }),
    );

    renderPage();

    expect(await screen.findByText(/plex can.t read this account.s restrictions/i)).toBeVisible();
    expect(screen.getAllByText(/rename/i).length).toBeGreaterThan(0);
    expect(screen.queryByText(/next run merges them back in/i)).toBeNull();
  });

  it("a parental-profile account is explained once, on its own line, in rows", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "unhideable",
        rows_on_plex: ["shortlist_mike", "shortlist_dan", "shortlist_ann"],
        accounts: [
          account({
            display_name: "kid",
            user: "kid",
            state: "refused_by_plex",
            restriction_profile: "older_kid",
            hides: [],
            missing: ["shortlist_mike", "shortlist_dan", "shortlist_ann"],
          }),
        ],
        // Two libraries per row: the run counts 6 collections for the same 3 rows.
        enforcement: { ...UNMEASURED, unhideable: { kid: [1, 2, 3, 4, 5, 6] }, unhideable_measured: true, unhideable_run_id: 420 },
      }),
    );

    renderPage();

    const note = await screen.findByText(/plex rejects hide rules for restriction profiles/i);
    expect(note).toHaveTextContent("kid can see 3 rows that aren’t theirs");
    expect(note).toHaveTextContent(/clear it in plex/i);
    // The profile reads as its name, and "How →" leads to the page that walks through the fix.
    expect(screen.getByText(/Restriction Profile Older Kid/)).toBeInTheDocument();
    expect(within(note).getByRole("link", { name: /How/ })).toHaveAttribute("href", "/users/7");
    expect(within(screen.getByRole("table")).queryByText(/collection/i)).toBeNull();
    // The explanation is not repeated in a banner above the grid.
    expect(screen.getAllByText(/restriction profiles/i)).toHaveLength(1);
  });

  it("a stored rule a run saw Plex ignore is not drawn as hidden", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "not_enforced",
        enforcement: { ...UNMEASURED, measured: true, run_id: 419, not_enforced: { sarah: [21] } },
      }),
    );

    renderPage();

    expect(await screen.findByText("Rule stored, not applied")).toBeVisible();
    expect(screen.queryByText("Hidden")).toBeNull();
  });

  it("shows the account's own filter conditions, so rule 3's preservation is visible", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({ accounts: [account({ other_conditions: ["filterMovies: label!=Kids"] })] }),
    );

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: /show their own filters/i }));

    expect(screen.getByText("filterMovies: label!=Kids")).toBeVisible();
  });
});

describe("proof first", () => {
  it("the 'is Plex applying the rules' check sits above the grid, with the Home-only limit once", async () => {
    renderPage();

    const check = await screen.findByRole("region", { name: /plex was applying the rules when last checked/i });
    const grid = screen.getByRole("heading", { name: "Who sees what" });
    expect(check.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(check).getByText(/collections tab and related shelves can.t be checked/i)).toBeVisible();
    expect(screen.getAllByText(/collections tab/i)).toHaveLength(1);
  });

  it("an unmeasured check says it hasn't been checked, as a warning, not 'all clear'", async () => {
    getPrivacyStatus.mockResolvedValue(status({ enforcement: UNMEASURED }));

    renderPage();

    const headline = await screen.findByText(/hasn.t been checked applying these rules/i);
    expect(headline.className).toMatch(/warning/);
    expect(screen.queryByText(/plex was applying the/i)).toBeNull();
    // "may answer it", not "will": the check can end unmeasured whenever a token or hub read fails.
    expect(screen.getByText(/every run tries.*again/is)).toBeVisible();
  });

  it("'Verify now' starts a run — and says so — then opens it", async () => {
    getPrivacyStatus.mockResolvedValue(status({ enforcement: UNMEASURED }));
    renderPage();

    const verify = await screen.findByRole("button", { name: "Verify now" });
    expect(screen.getByText(/starts a run of every row/i)).toBeVisible();
    await userEvent.click(verify);

    await waitFor(() => expect(startRun).toHaveBeenCalledWith({}));
    await waitFor(() => expect(screen.getByLabelText("Address")).toHaveTextContent("/runs/512"));
  });

  it("names the run and when it measured", async () => {
    renderPage();

    expect(await screen.findByText(/checked in run #418/i)).toBeVisible();
    expect(screen.getByText(/plex was applying the hide rules on the accounts checked/i)).toBeVisible();
  });

  it("reports an exposure as something to file, and leads the page in red", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "not_enforced",
        accounts: [account({ state: "hiding", missing: [] })],
        enforcement: { ...UNMEASURED, measured: true, run_id: 419, not_enforced: { sarah: [21, 22] } },
      }),
    );

    renderPage();

    const headline = await screen.findByText(/plex is ignoring the privacy filter/i);
    expect(headline.className).toMatch(/destructive/);
    expect(screen.getByText(/please open an issue/i)).toBeVisible();
    expect(screen.queryByText(/every account hides/i)).toBeNull();
  });

  it("a verdict this build doesn't know says so plainly, never green — and keeps the raw code out of the sentence", async () => {
    getPrivacyStatus.mockResolvedValue(status({ summary: "some_future_state" }));

    renderPage();

    const sentence = await screen.findByText(/doesn’t recognise the verdict/i);
    expect(sentence).toBeVisible();
    expect(sentence.textContent).not.toContain("some_future_state");
    expect(screen.getByText("some_future_state").tagName).toBe("CODE");
  });
});

describe("no green headline above an exposure", () => {
  it.each(["missing", "filter_unreadable", "unhideable"])(
    "says in red that accounts are exposed when the verdict is %s, and the check is not drawn green",
    async (summary) => {
      getPrivacyStatus.mockResolvedValue(
        status({
          summary,
          rows_on_plex: ["shortlist_mike"],
          accounts: [account({ state: "missing", hides: [], missing: ["shortlist_mike"] })],
        }),
      );

      renderPage();

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(/1 account.*(can see|isn.t hiding|not hiding)/i);
      const check = screen.getByRole("region", { name: /plex was applying the rules when last checked/i });
      expect(check.querySelector(".bg-success")).toBeNull();
    },
  );

  it("keeps the proof panel when plex.tv cannot be read, since it comes from Shortlist's own database", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({ summary: "unreadable", error: "plex.tv timed out", accounts: [] }),
    );

    renderPage();

    expect(await screen.findByRole("region", { name: /plex was applying the rules when last checked/i })).toBeVisible();
  });
});

describe("the Privacy header", () => {
  it("says what the page reads, and 'Read again' reads it again", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Privacy", level: 1 })).toBeVisible();
    expect(screen.getByText("Who can see which row on Plex, read live from plex.tv.")).toBeVisible();
    await screen.findByText("Who sees what");
    expect(getPrivacyStatus).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Read again" }));
    await waitFor(() => expect(getPrivacyStatus).toHaveBeenCalledTimes(2));
  });
});

describe("the snapshot line", () => {
  it("counts the share-filter snapshots uninstall restores from", async () => {
    getPrivacyStatus.mockResolvedValue(status({ snapshots_kept: 4 }));
    renderPage();
    expect(await screen.findByText(/4 snapshots kept/)).toBeVisible();
  });

  it("says 'snapshot' for one", async () => {
    getPrivacyStatus.mockResolvedValue(status({ snapshots_kept: 1 }));
    renderPage();
    expect(await screen.findByText(/1 snapshot kept/)).toBeVisible();
  });

  it("still counts the snapshots when plex.tv cannot be read, since they are Shortlist's own records", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({ summary: "unreadable", error: "plex.tv timed out", accounts: [], snapshots_kept: 3 }),
    );
    renderPage();
    expect(await screen.findByText(/3 snapshots kept/)).toBeVisible();
  });
});

describe("the Policy panel", () => {
  it("carries 'Disabled people see no rows at all' and saves it the moment it is flipped", async () => {
    getSettings.mockResolvedValue({ "privacy.hide_shared_from_disabled": true });
    renderPage();
    const toggle = await screen.findByRole("switch", { name: "Hide shared rows from disabled users" });
    expect(toggle).toBeChecked();
    expect(screen.getByText("Disabled people see no rows at all")).toBeVisible();

    await userEvent.click(toggle);

    // Same key and body the Settings › Advanced switch sent before it moved here.
    await waitFor(() => expect(putSettings).toHaveBeenCalledWith({ "privacy.hide_shared_from_disabled": false }));
    expect(putSettings).toHaveBeenCalledTimes(1);
  });

  it("reads an unset value as on, the server's default", async () => {
    renderPage();
    expect(await screen.findByRole("switch", { name: "Hide shared rows from disabled users" })).toBeChecked();
  });

  it("stays reachable when plex.tv cannot be read", async () => {
    getPrivacyStatus.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByRole("switch", { name: "Hide shared rows from disabled users" })).toBeVisible();
  });

  it("states the owner's own view as a Plex limit, not a control", async () => {
    renderPage();
    expect(await screen.findByText("Your own account sees every row")).toBeVisible();
    expect(screen.getByText("Plex limit")).toBeVisible();
    expect(screen.getByText(/Plex never filters the account that owns the server/)).toBeVisible();
  });
});

describe("the account lines", () => {
  it("names each account's kind beside its name", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        accounts: [
          account({ display_name: "sarah", account_id: 1, user_type: "shared" }),
          account({ display_name: "jess", account_id: 2, user_type: "managed" }),
        ],
      }),
    );
    renderPage();

    const sarah = (await screen.findByRole("link", { name: "sarah" })).closest("tr") as HTMLElement;
    expect(within(sarah).getByText("Shared")).toBeVisible();
    const jess = screen.getByRole("link", { name: "jess" }).closest("tr") as HTMLElement;
    expect(within(jess).getByText("Managed")).toBeVisible();
  });
});
