import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectionsSection } from "@/components/settings/connections-section";
import { findProvider } from "@/lib/providers";
import type { Settings } from "@/lib/types";

const { putSettings, testConnection, getRuns } = vi.hoisted(() => ({
  putSettings: vi.fn((v: Settings) => Promise.resolve(v)),
  testConnection: vi.fn(() => Promise.resolve({ ok: true, message: "ok" })),
  getRuns: vi.fn(() => Promise.resolve([] as unknown[])),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_e: unknown, f: string) => f,
  api: {
    putSettings: (v: Settings) => putSettings(v),
    testConnection: () => testConnection(),
    getRuns: () => getRuns(),
  },
}));

function renderSection(settings: Settings) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConnectionsSection settings={settings} />
    </QueryClientProvider>,
  );
}

describe("ConnectionsSection", () => {
  beforeEach(() => {
    putSettings.mockClear();
    testConnection.mockClear();
    getRuns.mockClear();
    getRuns.mockResolvedValue([]);
  });

  it("links the AI curator's 'Get a key' to the SELECTED provider's real key page", async () => {
    // Covers the real wiring — the `curator.provider` settings key and findProvider().keyUrl — not
    // just the generic link mechanism. A typo in either would break this.
    renderSection({ "curator.provider": "google" });
    const card = screen.getByTestId("connection-llm");
    await userEvent.click(within(card).getByRole("button", { name: /edit/i }));

    const link = within(card).getByRole("link", { name: /get a key/i });
    expect(link).toHaveAttribute("href", findProvider("google")!.keyUrl!);
  });

  it("points the TMDB card's 'Get a key' at the TMDB API settings page", async () => {
    renderSection({});
    const card = screen.getByTestId("connection-tmdb");
    await userEvent.click(
      within(card).getByRole("button", { name: /set up/i }),
    );

    expect(
      within(card).getByRole("link", { name: /get a key/i }),
    ).toHaveAttribute("href", "https://www.themoviedb.org/settings/api");
  });

  it("shows the last run's Exa search count on the Exa card once a key is saved", async () => {
    // Exa has no live-quota endpoint, so the most recent finished run's search count stands in for
    // "usage" — and it's a count of searches, never tokens.
    getRuns.mockResolvedValue([
      {
        id: 2,
        finished_at: "2026-07-20T03:31:00Z",
        stats: { exa_searches: 46 },
      },
      {
        id: 1,
        finished_at: "2026-07-19T03:31:00Z",
        stats: { exa_searches: 12 },
      },
    ]);
    renderSection({ "exa.apikey": "•••••" });
    const card = screen.getByTestId("connection-exa");
    expect(
      await within(card).findByText(/Last run: 46 Exa searches/),
    ).toBeInTheDocument();
  });

  it("omits the Exa usage note when no key is saved", async () => {
    getRuns.mockResolvedValue([
      {
        id: 1,
        finished_at: "2026-07-20T03:31:00Z",
        stats: { exa_searches: 46 },
      },
    ]);
    renderSection({});
    const card = screen.getByTestId("connection-exa");
    expect(within(card).queryByText(/Exa search/)).not.toBeInTheDocument();
  });

  it("omits the Exa usage note when a key is saved but no run has finished yet", async () => {
    // Fresh install: key configured, but nothing has run — no count to show, so no note.
    getRuns.mockResolvedValue([]);
    renderSection({ "exa.apikey": "•••••" });
    const card = screen.getByTestId("connection-exa");
    // Let the runs query settle so a late-arriving footnote would have rendered.
    await new Promise((r) => setTimeout(r, 0));
    expect(within(card).queryByText(/Exa search/)).not.toBeInTheDocument();
  });
});
