import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RequestCandidate } from "@/lib/types";
import { RequestsPage } from "@/pages/requests";

const { listRequests, sendRequests, rejectRequests, getSettings } = vi.hoisted(
  () => ({
    listRequests: vi.fn(),
    sendRequests: vi.fn((_ids: number[], _dryRun?: boolean) =>
      Promise.resolve({ sent: 1, dry_run: false, outcomes: [] }),
    ),
    rejectRequests: vi.fn((_ids: number[]) => Promise.resolve({ rejected: 1 })),
    getSettings: vi.fn(() => Promise.resolve({ "requests.enabled": true })),
  }),
);

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    listRequests: () => listRequests(),
    sendRequests: (ids: number[], dryRun?: boolean) =>
      sendRequests(ids, dryRun),
    rejectRequests: (ids: number[]) => rejectRequests(ids),
    getSettings: () => getSettings(),
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
    tags: [],
    wanters: [],
    why: [],
    status: "pending",
    detail: "",
    updated_at: null,
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
    getSettings.mockResolvedValue({ "requests.enabled": true });
  });

  it("shows an empty state when nothing has ever been queued", async () => {
    listRequests.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText(/Nothing waiting/i)).toBeTruthy();
  });

  it("shows a distinct 'off' empty state when requests are disabled", async () => {
    listRequests.mockResolvedValue([]);
    getSettings.mockResolvedValue({ "requests.enabled": false });
    renderPage();
    // Never implies auto-send is running; points the owner at Settings to turn it on.
    expect(await screen.findByText(/Requests are off/i)).toBeTruthy();
    expect(screen.getByText(/Enable in Settings/i)).toBeTruthy();
  });

  it("files a sent title under the Sonarr/Radarr send log with its outcome and when", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "Dune: Part Two", status: "pending" }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "Shogun",
        media_type: "show",
        status: "sent",
        detail: "added to Sonarr",
        updated_at: "2026-07-17T03:31:00Z",
      }),
    ]);
    renderPage();
    expect(await screen.findByText("Dune: Part Two")).toBeTruthy();
    expect(screen.getByText("Shogun")).toBeTruthy();
    // The send log is its own section, and each entry carries the app's answer (the "reason why").
    expect(
      screen.getByRole("heading", { name: "Sent to Sonarr/Radarr" }),
    ).toBeTruthy();
    expect(screen.getByText(/added to Sonarr/i)).toBeTruthy();
  });

  it("explains where a request came from: which person, which row, and why", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        title: "El Chavo del Ocho",
        wanters: ["Sarah", "Mike"],
        why: [
          {
            user: "Sarah",
            row: "Comedy Classics",
            seed: "Fawlty Towers",
            source: "tmdb_similar",
          },
          {
            user: "Mike",
            row: "Sci-Fi Night",
            seed: "Futurama",
            source: "trakt",
          },
        ],
      }),
    ]);
    renderPage();
    await screen.findByText("El Chavo del Ocho");
    // Each (person, row) is spelled out with the reason, not just a bare "wanted by 2 people".
    expect(screen.getByText("Comedy Classics")).toBeTruthy();
    expect(
      screen.getByText(/because they watched Fawlty Towers/i),
    ).toBeTruthy();
    expect(screen.getByText("Sci-Fi Night")).toBeTruthy();
    expect(screen.getByText(/because they watched Futurama/i)).toBeTruthy();
  });

  it("shows how a seedless pick was suggested when there is no seed", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        title: "Trending Thing",
        why: [
          {
            user: "Sarah",
            row: "Fresh Picks",
            seed: "",
            source: "tmdb_discover",
          },
        ],
      }),
    ]);
    renderPage();
    await screen.findByText("Trending Thing");
    // With no seed there is no "because they watched"; the row still explains how it was suggested.
    expect(screen.getByText("Fresh Picks")).toBeTruthy();
    expect(screen.getByText(/via /i)).toBeTruthy();
    expect(screen.queryByText(/because they watched/i)).toBeNull();
  });

  it("names who wanted a title, and falls back to the count when none were recorded", async () => {
    listRequests.mockResolvedValue([
      candidate({ id: 1, title: "With Names", wanters: ["Sarah", "Mike"] }),
      candidate({
        id: 2,
        tmdb_id: 200,
        title: "No Names",
        demand: 3,
        wanters: [],
      }),
    ]);
    renderPage();
    expect(await screen.findByText(/Wanted by Sarah, Mike/)).toBeTruthy();
    expect(screen.getByText(/wanted by 3 people/)).toBeTruthy();
  });

  it("truncates a long wanters list to three names plus a +N more count", async () => {
    listRequests.mockResolvedValue([
      candidate({
        id: 1,
        title: "Popular",
        wanters: ["Sarah", "Mike", "Ann", "Jo", "Lee"],
      }),
    ]);
    renderPage();
    expect(
      await screen.findByText(/Wanted by Sarah, Mike, Ann \+2 more/),
    ).toBeTruthy();
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

  it("reads as off — and cannot send — when requests are disabled but candidates are on file", async () => {
    // The "off" state used to depend on the inbox being EMPTY, so stale candidates rendered the
    // full inbox with a live Send button on a feature the owner had turned off.
    getSettings.mockResolvedValue({ "requests.enabled": false });
    listRequests.mockResolvedValue([candidate({ id: 3, title: "Fallout" })]);
    renderPage();

    expect(await screen.findByText(/Requests are off/i)).toBeTruthy();
    expect(screen.getByText("Fallout")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /to Sonarr\/Radarr/i }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: /Reject/i })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Fallout/i })).toBeDisabled();
  });

  it("keeps the inbox actionable while requests are on", async () => {
    listRequests.mockResolvedValue([candidate({ id: 3, title: "Fallout" })]);
    renderPage();

    expect(await screen.findByText("Fallout")).toBeTruthy();
    expect(screen.queryByText(/Requests are off/i)).toBeNull();
    expect(
      screen.getByRole("checkbox", { name: /Fallout/i }),
    ).not.toBeDisabled();
  });
});
