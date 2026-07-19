import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectionsSection } from "@/components/settings/connections-section";
import { findProvider } from "@/lib/providers";
import type { Settings } from "@/lib/types";

const { putSettings, testConnection } = vi.hoisted(() => ({
  putSettings: vi.fn((v: Settings) => Promise.resolve(v)),
  testConnection: vi.fn(() => Promise.resolve({ ok: true, message: "ok" })),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_e: unknown, f: string) => f,
  api: {
    putSettings: (v: Settings) => putSettings(v),
    testConnection: () => testConnection(),
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
});
