import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GroupedPicks } from "@/components/user-detail/grouped-picks";
import { UserDetailBody } from "@/pages/user-detail";
import type { Pick, PrivacyStatus, User, UserRow } from "@/lib/types";
import { makeUser } from "@/test/user-fixtures";

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

const KID = makeUser({
  id: 2,
  username: "kid",
  slug: "kid",
  user_type: "managed",
  enabled: false,
  restricted: true,
  restriction_profile: "older_kid",
  unhidden_rows: 5,
});

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

// Reset for every test in the file: the nested tests below swap these and restore them by hand.
beforeEach(() => {
  userRows.current = [
    { collection_id: 1, slug: "picked", name: "Picked for You", library: "", media: "both", size: 15, recent_count: 8, is_default: true, muted: false, override: {}, picks: [pick(1)] } as unknown as UserRow,
  ];
  privacyStatus.current = {
    accounts: [{ user_id: 2, state: "refused_by_plex", missing: ["a", "b", "c"], user: "kid" }],
    enforcement: {},
  } as unknown as PrivacyStatus;
});

describe("an Off person's page", () => {
  it("is plainly Off: Run now disabled with a reason, no Active switch, rows not applying", async () => {
    renderBody(KID);

    expect(screen.getByRole("button", { name: /Run for kid/ })).toBeDisabled();
    expect(screen.getByText(/Clear kid’s Restriction Profile in Plex first/)).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /Pause or resume/ })).toBeNull();
    expect(await screen.findByText(/No new rows — kid is off/)).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /does not apply while kid is off/ })).toBeDisabled();
  });

  it("offers the fix in Plex, not a Turn on that could not change anything, when a profile keeps them off", () => {
    renderBody(KID);

    expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
    expect(screen.getByRole("link", { name: /Fix in Plex/ })).toHaveAttribute(
      "href",
      expect.stringContaining("app.plex.tv"),
    );
  });

  it("badges the profile as a neutral outline, not a red alarm", () => {
    renderBody(KID);

    const badge = screen.getByText("Restriction: Older Kid");
    expect(badge.className).not.toMatch(/bg-destructive/);
    expect(badge.className).toMatch(/border/);
  });

  it("leads the title with a Users breadcrumb instead of a back line above it", () => {
    renderBody(KID);

    // The separator is spoken: a screen reader hears "Users / kid", not "Userskid".
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Users / kid");
    expect(screen.getByRole("link", { name: "Users" })).toHaveAttribute("href", "/users");
  });

  it("names unbuilt rows in plain words, never with raw tokens or token chips", async () => {
    const original = userRows.current;
    userRows.current = [
      { collection_id: 2, slug: "because", name: "🎯 Because you watched {top_seed}", library: "", media: "movie", size: 20, recent_count: 8, is_default: false, muted: false, override: {}, picks: [] } as unknown as UserRow,
      { collection_id: 3, slug: "seen", name: "☕ {library_name} you've already seen", library: "", media: "both", size: 15, recent_count: 8, is_default: false, muted: false, override: {}, picks: [] } as unknown as UserRow,
    ];
    try {
      renderBody(KID);

      expect(await screen.findByText("🎯 Because you watched …")).toBeInTheDocument();
      expect(screen.getByText("☕ You've already seen")).toBeInTheDocument();
      expect(document.body.textContent).not.toContain("{");
    } finally {
      userRows.current = original;
    }
  });

  it("counts rows, not collections, and gives the Plex fix once", async () => {
    renderBody(KID);

    const banner = await screen.findByTestId("off-banner");
    expect(await screen.findByText(/kid can see 3 rows that aren’t theirs/)).toBeInTheDocument();
    expect(banner).not.toHaveTextContent(/collection/i);
    expect(banner).not.toHaveTextContent(/5 /);
    expect(banner).toHaveTextContent("Restriction Profile → None");
  });

  it("names the person in a row titled with {user}", async () => {
    const original = userRows.current;
    userRows.current = [
      { collection_id: 4, slug: "mine", name: "{user}'s picks", library: "", media: "both", size: 15, recent_count: 8, is_default: false, muted: false, override: {}, picks: [] } as unknown as UserRow,
    ];
    try {
      renderBody({ ...KID, display_name: "Kiddo" });
      expect(await screen.findByText("Kiddo's picks")).toBeInTheDocument();
    } finally {
      userRows.current = original;
    }
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

  it("walks through the Plex fix for a profiled account that is exposed but not Off", async () => {
    renderBody({ ...KID, enabled: true, restricted: false });

    const banner = await screen.findByTestId("profile-exposure-banner");
    expect(await screen.findByText(/kid can see 3 rows that aren’t theirs/)).toBeInTheDocument();
    expect(banner).toHaveTextContent("Restriction Profile → None");
    expect(banner).not.toHaveTextContent(/\bOff\b/);
    expect(banner).not.toHaveTextContent(/does not fix this/);
    expect(screen.getByRole("link", { name: /Open Plex Users/ })).toBeInTheDocument();
  });

  it("shows no profile banner on a profiled account nothing is exposed on", async () => {
    const original = privacyStatus.current;
    privacyStatus.current = {
      accounts: [{ user_id: 2, state: "hiding", missing: [], user: "kid" }],
      enforcement: {},
    } as unknown as PrivacyStatus;
    try {
      renderBody({ ...KID, enabled: true, restricted: false, unhidden_rows: 0 });
      await screen.findByText("Title 1");
      expect(screen.queryByTestId("profile-exposure-banner")).toBeNull();
    } finally {
      privacyStatus.current = original;
    }
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
  it("keeps rank order across seeds and gives each poster its own reason", () => {
    render(
      <GroupedPicks
        collapseAfter={10}
        picks={[
          pick(1),
          pick(2, { seed_title: "Breaking Bad" }),
          pick(3),
          pick(4, { seed_title: null }),
        ]}
      />,
    );

    expect(screen.queryByRole("heading", { level: 3 })).toBeNull();
    const cards = screen.getAllByRole("listitem");
    expect(cards.map((c) => c.textContent)).toEqual([
      expect.stringContaining("Title 1"),
      expect.stringContaining("Title 2"),
      expect.stringContaining("Title 3"),
      expect.stringContaining("Title 4"),
    ]);
    expect(cards[0]).toHaveTextContent("Because you watched GoodFellas");
    expect(cards[1]).toHaveTextContent("Because you watched Breaking Bad");
    expect(cards[2]).toHaveTextContent("Because you watched GoodFellas");
    expect(cards[3]).not.toHaveTextContent("Because you watched");
    expect(screen.queryByText("Also picked")).toBeNull();
  });

  it("collapses to the first N and expands on request", async () => {
    render(<GroupedPicks collapseAfter={2} picks={[pick(1), pick(2), pick(3)]} />);

    expect(screen.queryByText("Title 3")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show all 3 titles" }));
    expect(screen.getByText("Title 3")).toBeInTheDocument();
  });
});
