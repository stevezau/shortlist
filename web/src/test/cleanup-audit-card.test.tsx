import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CleanupAuditCard } from "@/components/settings/cleanup-audit-card";
import type { OwnedCollectionsAudit } from "@/lib/types";

const { getOwnedCollections } = vi.hoisted(() => ({
  getOwnedCollections: vi.fn<() => Promise<OwnedCollectionsAudit>>(),
}));

vi.mock("@/lib/api", () => ({
  ApiError: class extends Error {},
  apiErrorMessage: (_e: unknown, fallback: string) => fallback,
  api: { getOwnedCollections: () => getOwnedCollections() },
}));

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CleanupAuditCard />
    </QueryClientProvider>,
  );
}

describe("CleanupAuditCard", () => {
  beforeEach(() => getOwnedCollections.mockReset());

  it("does not hit Plex until asked, then lists rows and flags orphans", async () => {
    getOwnedCollections.mockResolvedValue({
      total: 2,
      orphans: 1,
      collections: [
        {
          library: "Movies",
          title: "Old Row",
          label: "shortlist_ghost",
          rating_key: 2,
          kind: "user",
          slug: "ghost",
          orphan: true,
        },
        {
          library: "Movies",
          title: "Picked for You",
          label: "shortlist_sarah",
          rating_key: 1,
          kind: "user",
          slug: "sarah",
          orphan: false,
        },
      ],
    });
    renderCard();

    // On-demand: nothing fetched on render.
    expect(getOwnedCollections).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /Check Plex/i }));

    expect(await screen.findByText(/user or shared row is gone/i)).toBeInTheDocument();
    expect(screen.getByText("Old Row")).toBeInTheDocument();
    expect(screen.getByText(/no longer in the app/i)).toBeInTheDocument(); // the orphan badge
    expect(getOwnedCollections).toHaveBeenCalledTimes(1);
  });

  it("reassures when everything is in sync", async () => {
    getOwnedCollections.mockResolvedValue({
      total: 1,
      orphans: 0,
      collections: [
        {
          library: "TV",
          title: "Picked for You",
          label: "shortlist_sarah",
          rating_key: 5,
          kind: "user",
          slug: "sarah",
          orphan: false,
        },
      ],
    });
    renderCard();

    await userEvent.click(screen.getByRole("button", { name: /Check Plex/i }));

    expect(await screen.findByText(/all in sync with the app/i)).toBeInTheDocument();
  });
});
