import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { AccountPrivacy, PrivacyStatus } from "@/lib/types";
import { SharingPage } from "@/pages/sharing";

const { getPrivacyStatus } = vi.hoisted(() => ({
  getPrivacyStatus: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: { ...actual.api, getPrivacyStatus: () => getPrivacyStatus() },
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
    rows_error: null,
    error: null,
    enforcement: {
      measured: true,
      run_id: 418,
      measured_at: "2026-09-05T01:00:00+00:00",
      not_enforced: {},
    },
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SharingPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getPrivacyStatus.mockReset();
  getPrivacyStatus.mockResolvedValue(status());
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

  it("reports a healthy server with the number of rows and when it was read", async () => {
    renderPage();

    expect(
      await screen.findByText(
        /every account hides all 1 row that aren't theirs/i,
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
    expect(screen.queryByText(/hiding every row/i)).toBeNull();
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
    expect(screen.queryByText(/hiding every row/i)).toBeNull();
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

    expect(await screen.findByText("Hides 0 of 0")).toBeVisible();
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
        },
      }),
    );

    renderPage();

    expect(await screen.findByText(/not checked recently/i)).toBeVisible();
    expect(screen.queryByText(/plex was applying the hide rules/i)).toBeNull();
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
        enforcement: {
          measured: true,
          run_id: 419,
          measured_at: "2026-09-05T01:00:00+00:00",
          not_enforced: { sarah: [21, 22] },
        },
      }),
    );

    renderPage();

    expect(
      await screen.findByText(/plex is ignoring the privacy filter/i),
    ).toBeVisible();
    expect(screen.getByText(/please open an issue/i)).toBeVisible();
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
