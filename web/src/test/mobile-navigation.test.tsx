import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { NavBody } from "@/components/layout/app-shell";
import { MobileNavigation } from "@/components/layout/mobile-navigation";
import type * as ApiModule from "@/lib/api";
import { queryKeys } from "@/lib/queries";
import type { AccountPrivacy, Collection, PrivacyStatus, User } from "@/lib/types";

const getPrivacyStatus = vi.fn<() => Promise<PrivacyStatus>>();
const getSettings = vi.fn<() => Promise<Record<string, unknown>>>();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getSession: vi.fn().mockResolvedValue({ authenticated: true, login_required: true, username: "owner" }),
      getVersion: vi.fn().mockResolvedValue({ version: "1.9.3" }),
      getPrivacyStatus: () => getPrivacyStatus(),
      getSettings: () => getSettings(),
    },
  };
});

function privacy(states: string[]): PrivacyStatus {
  return {
    accounts: states.map(
      (state, i) =>
        ({
          account_id: i,
          display_name: `person${i}`,
          hides: [],
          manage_sharing: true,
          missing: [],
          other_conditions: [],
          restriction_profile: state === "refused_by_plex" ? "older_kid" : "",
          should_hide: [],
          slug: `person${i}`,
          state,
          user: `person${i}`,
          user_id: i,
          user_type: "shared",
        }) satisfies AccountPrivacy,
    ),
    enforcement: {} as PrivacyStatus["enforcement"],
    error: null,
    read_at: "2026-10-03T02:30:00Z",
    rows_error: null,
    rows_on_plex: [],
    snapshots_kept: 0,
    summary: "clean",
  };
}

async function openRealNav(client: QueryClient) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MobileNavigation>
          <NavBody />
        </MobileNavigation>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await userEvent.click(screen.getByRole("button", { name: "Open menu" }));
  return screen.getByRole("dialog", { name: "Main menu" });
}

function freshClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

it("traps keyboard focus, closes on Escape or a link, and restores the trigger", async () => {
  render(<><MobileNavigation><a href="#rows">Rows</a><a href="#users">Users</a></MobileNavigation><button>Outside</button></>);
  const trigger = screen.getByRole("button", { name: "Open menu" });
  await userEvent.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "Main menu" });
  for (let i = 0; i < 6; i++) {
    await userEvent.tab();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
  }
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  await userEvent.click(trigger);
  await userEvent.click(screen.getByRole("link", { name: "Rows" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

describe("the drawer carries the same navigation as the rail", () => {
  it("lists the eight sections in order, Privacy and Activity included", async () => {
    getPrivacyStatus.mockResolvedValue(privacy(["hiding"]));
    getSettings.mockResolvedValue({ "requests.enabled": true });
    const drawer = await openRealNav(freshClient());

    const main = within(drawer).getByRole("navigation", { name: "Main" });
    const labels = within(main)
      .getAllByRole("link")
      .map((link) => link.textContent?.trim());
    expect(labels).toEqual(["Dashboard", "Rows", "Users", "Privacy", "Runs", "Requests", "Activity", "Settings"]);
    expect(within(main).getByRole("link", { name: "Privacy" })).toHaveAttribute("href", "/privacy");
    expect(within(main).getByRole("link", { name: "Activity" })).toHaveAttribute("href", "/activity");
  });

  it("keeps Star on GitHub and Buy me a coffee as visible links, not behind a menu", async () => {
    getPrivacyStatus.mockResolvedValue(privacy(["hiding"]));
    getSettings.mockResolvedValue({ "requests.enabled": true });
    const drawer = await openRealNav(freshClient());

    expect(within(drawer).getByRole("link", { name: /star on github/i })).toBeVisible();
    expect(within(drawer).getByRole("link", { name: /buy me a coffee/i })).toBeVisible();
    expect(within(drawer).getByRole("link", { name: /help & docs/i })).toBeVisible();
    expect(within(drawer).getByRole("link", { name: /have an issue\?/i })).toBeVisible();
  });

  it("marks Privacy when an account will not hide other people's rows", async () => {
    getPrivacyStatus.mockResolvedValue(privacy(["hiding", "refused_by_plex"]));
    getSettings.mockResolvedValue({ "requests.enabled": true });
    const drawer = await openRealNav(freshClient());

    expect(await within(drawer).findByRole("link", { name: "Privacy (needs attention)" })).toBeInTheDocument();
  });

  it("leaves Privacy unmarked when every account hides every row", async () => {
    getPrivacyStatus.mockResolvedValue(privacy(["hiding", "owner"]));
    getSettings.mockResolvedValue({ "requests.enabled": true });
    const drawer = await openRealNav(freshClient());

    await vi.waitFor(() => expect(getPrivacyStatus).toHaveBeenCalled());
    expect(within(drawer).getByRole("link", { name: "Privacy" })).toBeInTheDocument();
    expect(within(drawer).queryByRole("link", { name: /needs attention/ })).toBeNull();
  });

  it("dims Requests, still clickable, when requests are off", async () => {
    getPrivacyStatus.mockResolvedValue(privacy(["hiding"]));
    getSettings.mockResolvedValue({ "requests.enabled": false });
    const drawer = await openRealNav(freshClient());

    const requests = await within(drawer).findByRole("link", { name: "Requests (off)" });
    expect(requests).toHaveAttribute("href", "/requests");
  });

  it("counts rows and users only when they are already loaded", async () => {
    getPrivacyStatus.mockResolvedValue(privacy(["hiding"]));
    getSettings.mockResolvedValue({ "requests.enabled": true });
    const client = freshClient();
    client.setQueryData<Collection[]>(queryKeys.collections, [{ id: 1 } as Collection, { id: 2 } as Collection]);
    const drawer = await openRealNav(client);

    const main = within(drawer).getByRole("navigation", { name: "Main" });
    // Users were never fetched, so the item carries no number rather than triggering a request.
    expect(within(main).getByRole("link", { name: "Rows" })).toHaveTextContent(/^Rows2$/);
    expect(within(main).getByRole("link", { name: "Users" })).toHaveTextContent(/^Users$/);

    client.setQueryData<User[]>(queryKeys.users, [{ id: 1 } as User, { id: 2 } as User, { id: 3 } as User]);
    expect(await within(main).findByText("3")).toBeInTheDocument();
  });
});
