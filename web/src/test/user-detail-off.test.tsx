import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GroupedPicks } from "@/components/user-detail/grouped-picks";
import { UserDetailBody } from "@/pages/user-detail";
import type { Pick, PrivacyStatus, User, UserRow } from "@/lib/types";

const { userRows, privacyStatus } = vi.hoisted(() => ({
  userRows: { current: [] as unknown[] },
  privacyStatus: { current: null as unknown },
}));
vi.mock("@/lib/api", () => ({
  apiUrl: (path: string) => path,
  api: new Proxy(
    {},
    {
      get: (_target, name) => {
        if (name === "getUserRows") return () => Promise.resolve(userRows.current);
        if (name === "getPrivacyStatus") return () => Promise.resolve(privacyStatus.current);
        if (name === "getReport") return () => Promise.resolve({ first_pick: null, overall: { landing: {} } });
        return () => Promise.resolve([]);
      },
    },
  ),
}));

const KID = {
  id: 2,
  username: "kid",
  display_name: "",
  slug: "kid",
  user_type: "managed",
  enabled: false,
  restricted: true,
  restriction_profile: "older_kid",
  unhidden_rows: 5,
  history_depth: 0,
  last_run_at: null,
  prefs: {},
} as unknown as User;

function pick(rank: number, patch: Partial<Pick> = {}): Pick {
  return {
    rank,
    title: `Title ${rank}`,
    rating_key: 100 + rank,
    reason: "because",
    seed_title: "GoodFellas",
    sources: ["tmdb_similar"],
    affinity: 0.9,
    ...patch,
  } as Pick;
}

function renderBody(user: User) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UserDetailBody user={user} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("an Off person's page", () => {
  userRows.current = [
    { collection_id: 1, slug: "picked", name: "Picked for You", library: "", media: "both", size: 15, recent_count: 8, is_default: true, muted: false, override: {}, picks: [pick(1)] } as unknown as UserRow,
  ];
  privacyStatus.current = {
    accounts: [{ user_id: 2, state: "refused_by_plex", missing: ["a", "b", "c"], user: "kid" }],
    enforcement: {},
  } as unknown as PrivacyStatus;

  it("is plainly Off: Run now disabled with a reason, no Active switch, rows not applying", async () => {
    renderBody(KID);

    expect(screen.getByRole("button", { name: /Run for kid/ })).toBeDisabled();
    expect(screen.getByText(/Clear kid’s Restriction Profile in Plex first/)).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /Pause or resume/ })).toBeNull();
    expect(await screen.findByText(/No new rows — kid is off/)).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /does not apply while kid is off/ })).toBeDisabled();
  });

  it("counts rows, not collections, and gives the Plex fix once", async () => {
    renderBody(KID);

    const banner = await screen.findByTestId("off-banner");
    expect(await screen.findByText(/kid can see 3 rows that aren’t theirs/)).toBeInTheDocument();
    expect(banner).not.toHaveTextContent(/collection/i);
    expect(banner).not.toHaveTextContent(/5 /);
    expect(banner).toHaveTextContent("Restriction Profile → None");
  });

  it("still lists the picks of a row that is on Plex, and says no NEW rows are built", async () => {
    renderBody(KID);

    expect(await screen.findByText("Title 1")).toBeInTheDocument();
    const banner = await screen.findByTestId("off-banner");
    expect(banner).toHaveTextContent(/No new rows are built for kid/);
    expect(banner).not.toHaveTextContent(/has no Shortlist row/);
  });

  it("does not call a profile added after a row existed rowless", async () => {
    renderBody({ ...KID, enabled: true });

    expect(await screen.findByTestId("off-banner")).toHaveTextContent(/No new rows are built for kid/);
    expect(await screen.findByText("Title 1")).toBeInTheDocument();
  });

  it("says turning them off does not fix an exposure", async () => {
    renderBody(KID);

    const banner = await screen.findByTestId("off-banner");
    await vi.waitFor(() => expect(banner).toHaveTextContent(/Turning kid off in Shortlist does not fix this/));
  });

  describe("when the live reading is unavailable", () => {
    const original = privacyStatus.current;
    afterEach(() => {
      privacyStatus.current = original;
    });

    it("falls back to the last run's collection count, labelled as such, when plex.tv failed", async () => {
      privacyStatus.current = { accounts: [], error: "plex.tv timed out", enforcement: {} } as unknown as PrivacyStatus;
      renderBody(KID);

      const banner = await screen.findByTestId("off-banner");
      await vi.waitFor(() => expect(banner).toHaveTextContent(/last run/i));
      expect(banner).toHaveTextContent(/5 collections/);
    });

    it("falls back when the account is not in the reading", async () => {
      privacyStatus.current = { accounts: [], error: null, enforcement: {} } as unknown as PrivacyStatus;
      renderBody(KID);

      const banner = await screen.findByTestId("off-banner");
      await vi.waitFor(() => expect(banner).toHaveTextContent(/5 collections/));
    });

    it("says it could not check when there is no run count either", async () => {
      privacyStatus.current = { accounts: [], error: "plex.tv timed out", enforcement: {} } as unknown as PrivacyStatus;
      renderBody({ ...KID, unhidden_rows: 0 });

      const banner = await screen.findByTestId("off-banner");
      await vi.waitFor(() => expect(banner).toHaveTextContent(/couldn.t check what kid can see/i));
    });
  });

  it("is not Off for a profile on an account not reported restricted", () => {
    renderBody({ ...KID, enabled: true, restricted: false });

    expect(screen.queryByTestId("off-banner")).toBeNull();
    expect(screen.getByRole("switch", { name: /Pause or resume kid/ })).toBeChecked();
  });

  it("offers Turn on to a person who is merely switched off", () => {
    renderBody({ ...KID, restricted: false, restriction_profile: "" });

    expect(screen.getByRole("button", { name: "Turn on" })).toBeInTheDocument();
    expect(screen.getByText("Turn kid on first")).toBeInTheDocument();
  });
});

describe("an active person's page", () => {
  it("shows an Active switch and an enabled Run now", () => {
    renderBody({ ...KID, enabled: true, restriction_profile: "" });

    expect(screen.getByRole("switch", { name: /Pause or resume kid/ })).toBeChecked();
    expect(screen.getByRole("button", { name: /Run for kid/ })).toBeEnabled();
    expect(screen.queryByTestId("off-banner")).toBeNull();
  });
});

describe("GroupedPicks", () => {
  it("groups by the seed each pick came from, not once per pick", () => {
    render(
      <GroupedPicks
        collapseAfter={10}
        picks={[
          pick(1),
          pick(2),
          pick(3, { seed_title: "Breaking Bad" }),
          pick(4, { seed_title: null }),
        ]}
      />,
    );

    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "Because you watched GoodFellas",
      "Because you watched Breaking Bad",
      "Also picked",
    ]);
  });

  it("collapses to the first N and expands on request", async () => {
    render(<GroupedPicks collapseAfter={2} picks={[pick(1), pick(2), pick(3)]} />);

    expect(screen.queryByText("Title 3")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show all 3 titles" }));
    expect(screen.getByText("Title 3")).toBeInTheDocument();
  });
});
