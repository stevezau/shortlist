import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RequestCandidate } from "@/lib/types";
import { RequestsPage } from "@/pages/requests";

const { listRequests, sendRequests, rejectRequests } = vi.hoisted(() => ({
  listRequests: vi.fn(),
  sendRequests: vi.fn((_ids: number[], _dryRun?: boolean) =>
    Promise.resolve({ sent: 1, dry_run: false, outcomes: [] }),
  ),
  rejectRequests: vi.fn((_ids: number[]) => Promise.resolve({ rejected: 1 })),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    listRequests: () => listRequests(),
    sendRequests: (ids: number[], dryRun?: boolean) =>
      sendRequests(ids, dryRun),
    rejectRequests: (ids: number[]) => rejectRequests(ids),
  },
}));

function candidate(
  overrides: Partial<RequestCandidate> = {},
): RequestCandidate {
  return {
    id: 1,
    tmdb_id: 100,
    media_type: "movie",
    title: "Dune: Part Two",
    year: 2024,
    rating: 8.3,
    vote_count: 5000,
    demand: 4,
    status: "pending",
    detail: "",
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <RequestsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RequestsPage", () => {
  beforeEach(() => {
    listRequests.mockReset();
    sendRequests.mockClear();
    rejectRequests.mockClear();
  });

  it("shows an empty state when nothing has ever been queued", async () => {
    listRequests.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText(/Nothing waiting/i)).toBeTruthy();
  });

  it("lists pending titles and files handled ones under Already handled", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune: Part Two", status: "pending" }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Shogun",
        media_type: "show",
        status: "sent",
      }),
    ]);
    renderPage();
    expect(await screen.findByText("Dune: Part Two")).toBeTruthy();
    expect(screen.getByText("Shogun")).toBeTruthy();
    expect(screen.getByText(/Already handled/i)).toBeTruthy();
    expect(screen.getByText(/sent to Sonarr\/Radarr/i)).toBeTruthy();
  });

  it("sends the selected title by its id", async () => {
    listRequests.mockResolvedValue([candidate({ id: 7, title: "Fallout" })]);
    renderPage();
    await screen.findByText("Fallout");
    await userEvent.click(screen.getByRole("checkbox", { name: /Fallout/i }));
    await userEvent.click(screen.getByRole("button", { name: /Send/i }));
    await waitFor(() => expect(sendRequests).toHaveBeenCalledWith([7], false));
  });

  it("rejects the selected title by its id", async () => {
    listRequests.mockResolvedValue([candidate({ id: 9, title: "Ripley" })]);
    renderPage();
    await screen.findByText("Ripley");
    await userEvent.click(screen.getByRole("checkbox", { name: /Ripley/i }));
    await userEvent.click(screen.getByRole("button", { name: /Reject/i }));
    await waitFor(() => expect(rejectRequests).toHaveBeenCalledWith([9]));
  });
});
