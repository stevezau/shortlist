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

  it("reports a healthy server without over-counting the rows", async () => {
    // No NUMBER any more. "hides all N rows" was read off `rows_on_plex`, which includes each
    // account's OWN row — so on a 40-user server the banner claimed "hides all 40" while every line
    // in the table below read "Hides 39 of 39". The claim is qualitative because the honest number
    // is per-account.
    renderPage();

    expect(
      await screen.findByText(
        /every account hides the rows that aren.t theirs/i,
      ),
    ).toBeVisible();
    expect(screen.getByText(/read from plex.tv at/i)).toBeVisible();
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

  it("a missing hide rule names the rows and what to do about it", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "missing",
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

    expect(
      await screen.findByText(/sarah can see a row that isn't theirs/i),
    ).toBeVisible();
    expect(screen.getByText(/shortlist_mike, shortlist_dan/)).toBeVisible();
    expect(screen.getByText(/next run merges it back in/i)).toBeVisible();
  });

  it("the owner row explains the Plex limitation instead of showing a fault", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        accounts: [
          account({
            display_name: "Steve",
            state: "owner",
            user_type: "owner",
            hides: [],
            missing: [],
          }),
          account(),
        ],
      }),
    );

    renderPage();

    expect(await screen.findByText(/you own the server/i)).toBeVisible();
    expect(screen.getByText(/that's plex, not a fault/i)).toBeVisible();
    // The headline stays clean: an owner row must not make the server look broken.
    expect(screen.queryByText(/can see a row that isn't theirs/i)).toBeNull();
  });

  it("a left-alone account reads as a setting, not a failure", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        accounts: [
          account({ state: "left_alone", manage_sharing: false, hides: [] }),
        ],
      }),
    );

    renderPage();

    expect(await screen.findByText(/left alone by choice/i)).toBeVisible();
    expect(screen.getByText(/that's the setting, not a fault/i)).toBeVisible();
  });

  it("an account Plex can't read a filter for names the fix, and never promises the next run", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "filter_unreadable",
        accounts: [
          account({
            state: "unreadable_filter",
            hides: [],
            missing: ["shortlist_mike"],
          }),
        ],
      }),
    );

    renderPage();

    expect(
      await screen.findByText(/plex can't read the restrictions on sarah/i),
    ).toBeVisible();
    expect(screen.getAllByText(/rename/i).length).toBeGreaterThan(0);
    expect(screen.queryByText(/next run merges it back in/i)).toBeNull();
  });

  it("a parental-profile account says Plex refuses the rule and how to fix it", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        accounts: [
          account({
            state: "refused_by_plex",
            restriction_profile: "little_kid",
            hides: [],
          }),
        ],
      }),
    );

    renderPage();

    expect(
      await screen.findByText(/plex won't accept hide rules for this account/i),
    ).toBeVisible();
    expect(screen.getByText(/clear the profile in plex/i)).toBeVisible();
  });

  it("counts rules as a number, so '1 of 1' and '0 of 0' cannot look alike", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        accounts: [account({ hides: [], should_hide: [], state: "hiding" })],
      }),
    );

    renderPage();

    // The noun is part of the assertion now: "Hides 2 of 2" left the reader to guess two of what
    // (audit finding, Sep 2026).
    expect(await screen.findByText("Hides 0 of 0 rows")).toBeVisible();
  });

  it("says 'row', singular, when only one row is in play", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        accounts: [
          account({ hides: ["r1"], should_hide: ["r1"], state: "hiding" }),
        ],
      }),
    );

    renderPage();

    expect(await screen.findByText("Hides 1 of 1 row")).toBeVisible();
  });

  it("shows the account's own filter conditions, so rule 3's preservation is visible", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        accounts: [
          account({ other_conditions: ["filterMovies: label!=Kids"] }),
        ],
      }),
    );

    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /show their own filters/i }),
    );

    expect(screen.getByText("filterMovies: label!=Kids")).toBeVisible();
  });
});

describe("the enforcement panel", () => {
  it("an unmeasured check says 'not checked recently', not 'all clear'", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        enforcement: {
          measured: false,
          run_id: null,
          measured_at: null,
          not_enforced: {},
          unhideable: {},
          unhideable_measured: false,
          unhideable_run_id: null,
          unhideable_measured_at: null,
        },
      }),
    );

    renderPage();

    // Said twice on purpose: in the status strip and on the panel that offers to fix it.
    expect((await screen.findAllByText(/not checked recently/i))[0]).toBeVisible();
    expect(screen.queryByText(/plex was applying the hide rules/i)).toBeNull();
    // A status with nothing to do about it is a dead end on the panel an owner opens when they are
    // already worried (audit finding, Sep 2026). The check rides a RUN, so say when it happens
    // again and offer the page that starts one.
    // "tries again ... may answer it", not "will". `_verify_filters_enforced` returns early on a
    // dry run and can end unmeasured whenever a token or hub read fails, so the next run is not a
    // guarantee — and this paragraph only renders when the last ones already failed to measure.
    expect(screen.getByText(/every run tries.*again/is)).toBeVisible();
    // The staleness is a WARNING, not grey: nothing here says the rules are applied.
    expect(screen.getAllByText("Not checked recently").length).toBeGreaterThan(0);
  });

  it("'Verify now' starts a run — and says so — then opens it", async () => {
    getPrivacyStatus.mockResolvedValue(status({ enforcement: UNMEASURED }));
    renderPage();

    const verify = await screen.findByRole("button", { name: "Verify now" });
    expect(screen.getByText(/starts a run/i)).toBeVisible();
    await userEvent.click(verify);

    // The same request as Runs' "Run all rows now": the check rides a run, so there is no lighter
    // call to make, and pretending otherwise would be a button that cannot do what it says.
    await waitFor(() => expect(startRun).toHaveBeenCalledWith({}));
    await waitFor(() => expect(screen.getByLabelText("Address")).toHaveTextContent("/runs/512"));
  });

  it("names the run and when it measured", async () => {
    renderPage();

    expect(await screen.findByText(/checked in run #418/i)).toBeVisible();
    expect(
      screen.getByText(
        /plex was applying the hide rules on the accounts checked/i,
      ),
    ).toBeVisible();
  });

  it("reports an exposure as something to file, not a setting to change", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "not_enforced",
        enforcement: {
          measured: true,
          run_id: 419,
          measured_at: "2026-09-05T01:00:00+00:00",
          not_enforced: { sarah: [21, 22] },
          unhideable: {},
          unhideable_measured: false,
          unhideable_run_id: null,
          unhideable_measured_at: null,
        },
      }),
    );

    renderPage();

    expect(
      await screen.findByText(/plex is ignoring the privacy filter/i),
    ).toBeVisible();
    expect(screen.getByText(/please open an issue/i)).toBeVisible();
  });

  it("a measured exposure turns the HEADLINE red, not just the panel", async () => {
    // The whole point of the summary. `_verify_filters_enforced` only spot-checks accounts that
    // ALREADY carry our excludes, so in this state every account is legitimately `hiding` with
    // `missing: []` — and the headline used to read "Every account hides all 1 row that aren't
    // theirs" directly above the red panel saying Plex was ignoring the filter.
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "not_enforced",
        accounts: [account({ state: "hiding", missing: [] })],
        enforcement: {
          measured: true,
          run_id: 419,
          measured_at: "2026-09-05T01:00:00+00:00",
          not_enforced: { sarah: [21, 22] },
          unhideable: {},
          unhideable_measured: false,
          unhideable_run_id: null,
          unhideable_measured_at: null,
        },
      }),
    );

    renderPage();

    expect(
      await screen.findByText(/plex saved every hide rule and is showing/i),
    ).toBeVisible();
    expect(screen.queryByText(/every account hides all/i)).toBeNull();
    // Not the "missing hide rule" wording either: these filters are complete.
    expect(screen.queryByText(/missing a hide rule/i)).toBeNull();
  });

  it("a verdict this build doesn't know says so plainly, never green — and keeps the raw code out of the sentence", async () => {
    // The SPA trusts the server's ranking, so a seventh state nobody wired up here must not fall
    // through to "Every account hides all N rows" — the one direction this page must never default.
    //
    // The second half is the audit finding: the token was interpolated straight into the English
    // ("couldn't interpret this reading (rows_unknown)"). It is still on the page, because a
    // server/SPA mismatch is exactly what a bug report needs — but as a labelled code to quote.
    getPrivacyStatus.mockResolvedValue(
      status({ summary: "some_future_state" }),
    );

    renderPage();

    const sentence = await screen.findByText(/doesn’t recognise the verdict/i);
    expect(sentence).toBeVisible();
    expect(sentence.textContent).not.toContain("some_future_state");
    expect(screen.queryByText(/every account hides all/i)).toBeNull();

    const code = screen.getByText("some_future_state");
    expect(code.tagName).toBe("CODE");
  });

  it("states the Home-only scope once, and never claims the Collections tab", async () => {
    renderPage();

    expect(
      await screen.findByText(/these checks cover the home screen/i),
    ).toBeVisible();
    expect(
      screen.getByText(
        /no way to confirm what plex does on the collections tab/i,
      ),
    ).toBeVisible();
  });
});

describe("the Privacy header", () => {
  it("says what the page reads, and 'Read again' reads it again", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Privacy", level: 1 })).toBeVisible();
    expect(screen.getByText("Which rows each Plex account can see, read live from plex.tv.")).toBeVisible();
    await screen.findByText(/every account hides/i);
    expect(getPrivacyStatus).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Read again" }));
    await waitFor(() => expect(getPrivacyStatus).toHaveBeenCalledTimes(2));
  });
});

describe("the status strip", () => {
  const strip = async () => within(await screen.findByRole("region", { name: "Privacy status" }));

  it("counts the accounts hiding every row out of the accounts that can be filtered, owner left out", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "unhideable",
        accounts: [
          account({ display_name: "sarah", account_id: 1 }),
          account({ display_name: "mike", account_id: 2 }),
          account({ display_name: "kid", account_id: 3, state: "refused_by_plex", restriction_profile: "older_kid", hides: [] }),
          account({ display_name: "Steve", account_id: 4, state: "owner", user_id: null }),
        ],
      }),
    );
    renderPage();
    const cells = await strip();
    expect(cells.getByText("2 of 3")).toBeVisible();
    expect(cells.getByText("sarah and mike")).toBeVisible();
    expect(cells.getByText("1")).toBeVisible();
    expect(cells.getByText(/kid · Restriction Profile/)).toBeVisible();
  });

  it("reads 'Not checked recently' as a warning when no run has measured enforcement", async () => {
    getPrivacyStatus.mockResolvedValue(status({ enforcement: UNMEASURED }));
    renderPage();
    const pill = (await strip()).getByText("Not checked recently");
    expect(pill.className).toMatch(/warning/);
  });

  it("names the run that last verified", async () => {
    renderPage();
    expect((await strip()).getByText("Run #418")).toBeVisible();
  });

  it("never says '0 of 0', NaN or a warning when there is nobody to hide rows from", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({ accounts: [account({ state: "owner", display_name: "Steve" })], rows_on_plex: [] }),
    );
    renderPage();
    const cells = await strip();
    expect(cells.queryByText(/0 of 0/)).toBeNull();
    expect(cells.queryByText(/NaN/)).toBeNull();
    expect(cells.getByText("No shared or managed accounts")).toBeVisible();
  });

  it("says 'Unknown' rather than a count off a failed plex.tv read", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({ summary: "unreadable", error: "plex.tv timed out", accounts: [account({ state: "unknown" })] }),
    );
    renderPage();
    const cells = await strip();
    expect(cells.getAllByText("Unknown").length).toBe(2);
    expect(cells.queryByText(/of 1/)).toBeNull();
  });

  it("counts the share-filter snapshots uninstall restores from", async () => {
    getPrivacyStatus.mockResolvedValue(status({ snapshots_kept: 4 }));
    renderPage();
    const cell = (await strip()).getByText("Snapshots kept").closest("div")!;
    expect(within(cell).getByText("4")).toBeVisible();
    expect(within(cell).getByText("Restored on uninstall")).toBeVisible();
  });

  it("still counts the snapshots when plex.tv cannot be read, since they are Shortlist's own records", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({ summary: "unreadable", error: "plex.tv timed out", accounts: [], snapshots_kept: 3 }),
    );
    renderPage();
    const cell = (await strip()).getByText("Snapshots kept").closest("div")!;
    expect(within(cell).getByText("3")).toBeVisible();
  });
});

describe("the Policy panel", () => {
  it("carries 'Disabled users see nothing' and saves it the moment it is flipped", async () => {
    getSettings.mockResolvedValue({ "privacy.hide_shared_from_disabled": true });
    renderPage();
    const toggle = await screen.findByRole("switch", { name: "Hide shared rows from disabled users" });
    expect(toggle).toBeChecked();
    expect(screen.getByText("Disabled users see nothing")).toBeVisible();

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
    expect(screen.getByText(/Plex cannot filter its own account/)).toBeVisible();
  });
});

describe("the Privacy alert and the account list", () => {
  it("states the unhideable verdict as one paragraph led by a bold sentence", async () => {
    getPrivacyStatus.mockResolvedValue(
      status({
        summary: "unhideable",
        accounts: [account({ display_name: "kid", user: "kid", state: "refused_by_plex", restriction_profile: "older_kid", hides: [] })],
        enforcement: { ...UNMEASURED, unhideable: { kid: [11, 12] }, unhideable_measured: true, unhideable_run_id: 420 },
      }),
    );
    renderPage();

    const lead = await screen.findByText(/Plex will not hide other people.s rows from kid at all/);
    expect(lead.tagName).toBe("STRONG");
    const paragraph = lead.closest("p") as HTMLElement;
    expect(paragraph).toHaveTextContent(/Restriction Profile to None/);
    expect(paragraph).toHaveTextContent(/Read from plex\.tv at/);
  });

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

    const sarah = (await screen.findByRole("link", { name: "sarah" })).closest("li") as HTMLElement;
    expect(within(sarah).getByText("Shared")).toBeVisible();
    const jess = screen.getByRole("link", { name: "jess" }).closest("li") as HTMLElement;
    expect(within(jess).getByText("Managed")).toBeVisible();
  });
});
