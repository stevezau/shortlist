import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectionsSection } from "@/components/settings/connections-section";
import { findProvider } from "@/lib/providers";
import type { Settings } from "@/lib/types";

const { putSettings, testConnection, getRuns } = vi.hoisted(() => ({
  putSettings: vi.fn((v: Settings) => Promise.resolve(v)),
  testConnection: vi.fn((_service: string) => Promise.resolve({ ok: true, message: "ok" })),
  getRuns: vi.fn(() => Promise.resolve([] as unknown[])),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_e: unknown, f: string) => f,
  api: {
    putSettings: (v: Settings) => putSettings(v),
    testConnection: (service: string) => testConnection(service),
    getRuns: () => getRuns(),
  },
}));

function renderSection(settings: Settings) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const ui = (s: Settings) => (
    <QueryClientProvider client={client}>
      <ConnectionsSection settings={s} />
    </QueryClientProvider>
  );
  const { rerender } = render(ui(settings));
  // The settings page re-renders its children when the settings query refreshes after a save; a
  // test that never does that can't see anything a card derives from freshly-saved settings.
  return { refresh: (next: Settings) => rerender(ui(next)) };
}

describe("ConnectionsSection", () => {
  beforeEach(() => {
    putSettings.mockClear();
    testConnection.mockClear();
    getRuns.mockClear();
    getRuns.mockResolvedValue([]);
  });

  describe("the one Web search card", () => {
    it("is a single card, not one per backend", () => {
      // Exa and SearXNG are two ways to do ONE thing, so they are one connection with a choice
      // inside it — the same shape as the AI provider card, not a card each.
      renderSection({});
      expect(screen.getByTestId("connection-websearch")).toBeInTheDocument();
      expect(screen.queryAllByTestId("connection-websearch")).toHaveLength(1);
      expect(screen.getByText("Web search")).toBeInTheDocument();
    });

    it("shows only the chosen backend's fields", async () => {
      renderSection({ "llm_web.search_provider": "searxng" });
      const card = screen.getByTestId("connection-websearch");
      await userEvent.click(within(card).getByRole("button", { name: /edit|set up/i }));

      expect(within(card).getByLabelText(/SearXNG address/i)).toBeInTheDocument();
      expect(within(card).queryByLabelText(/Exa API key/i)).not.toBeInTheDocument();
    });

    it("swaps the fields when the backend is switched", async () => {
      renderSection({ "llm_web.search_provider": "exa" });
      const card = screen.getByTestId("connection-websearch");
      await userEvent.click(within(card).getByRole("button", { name: /edit|set up/i }));
      expect(within(card).getByLabelText(/Exa API key/i)).toBeInTheDocument();

      await userEvent.click(within(card).getByRole("button", { name: /SearXNG/i }));
      expect(within(card).getByLabelText(/SearXNG address/i)).toBeInTheDocument();
      expect(within(card).queryByLabelText(/Exa API key/i)).not.toBeInTheDocument();
    });

    it("asks for one backend's fields, never both", () => {
      // Exactly one backend runs, so exactly one is ever configured. Showing both was `auto`'s
      // doing, and `auto` is gone.
      renderSection({ "llm_web.search_provider": "exa" });
      const card = screen.getByTestId("connection-websearch");
      return userEvent
        .click(within(card).getByRole("button", { name: /edit|set up/i }))
        .then(() => {
          expect(within(card).getByLabelText(/Exa API key/i)).toBeInTheDocument();
          expect(
            within(card).queryByLabelText(/SearXNG address/i),
          ).not.toBeInTheDocument();
        });
    });

    it("tests the backend you switched TO, not the one you left", async () => {
      // `service` is derived from SAVED settings, so a Test fired straight after Save probes the
      // PREVIOUS backend — and reports a green "Connection OK" for an instance never contacted.
      const { refresh } = renderSection({
        "llm_web.search_provider": "exa",
        "exa.apikey": "•••••",
      });
      const card = screen.getByTestId("connection-websearch");
      await userEvent.click(
        within(card).getByRole("button", { name: /edit|set up/i }),
      );
      await userEvent.click(
        within(card).getByRole("button", { name: /^SearXNG/i }),
      );
      await userEvent.type(
        within(card).getByLabelText(/SearXNG address/i),
        "http://searx.local:8080",
      );
      await userEvent.click(within(card).getByRole("button", { name: /^save$/i }));

      // What the real page does once the save invalidates the settings query.
      refresh({
        "llm_web.search_provider": "searxng",
        "exa.apikey": "•••••",
        "searxng.url": "http://searx.local:8080",
      } as unknown as Settings);

      await waitFor(() =>
        expect(testConnection.mock.calls.at(-1)?.[0]).toBe("searxng"),
      );
    });

    it("never auto-probes the provider's own search — that probe costs a real search", async () => {
      // Every other card auto-tests on load to colour its dot. This one's native probe performs a
      // REAL web search, so firing it unasked would bill (and, with no provider, error) on every
      // visit to Settings.
      renderSection({
        "curator.provider": "anthropic",
        "llm_web.search_provider": "native",
      });
      await waitFor(() => expect(getRuns).toHaveBeenCalled());
      expect(
        testConnection.mock.calls.map((c) => c[0]),
      ).not.toContain("native_search");
    });

    it("does auto-probe an external backend, which is cheap", async () => {
      renderSection({
        "llm_web.search_provider": "searxng",
        "searxng.url": "http://searx.local:8080",
      });
      await waitFor(() =>
        expect(testConnection.mock.calls.map((c) => c[0])).toContain("searxng"),
      );
    });

    it("summarises which backend is in play", () => {
      renderSection({
        "llm_web.search_provider": "searxng",
        "searxng.url": "http://searx.local:8080",
      });
      expect(screen.getByText(/SearXNG · http:\/\/searx.local:8080/)).toBeInTheDocument();
    });
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

  it("shows the last run's web-search count, without claiming it was billed", async () => {
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
    const card = screen.getByTestId("connection-websearch");
    expect(
      await within(card).findByText(/Last run: 46 web searches/),
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
    const card = screen.getByTestId("connection-websearch");
    // Settle the runs query first. Asserting straight away passed whatever the component did,
    // because the note cannot be on screen before the data it renders has arrived.
    await waitFor(() =>
      expect(
        screen.getByTestId("connection-websearch").textContent,
      ).toBeDefined(),
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(within(card).queryByText(/Last run:/)).not.toBeInTheDocument();
  });

  it("omits the Exa usage note when a key is saved but no run has finished yet", async () => {
    // Fresh install: key configured, but nothing has run — no count to show, so no note.
    getRuns.mockResolvedValue([]);
    renderSection({ "exa.apikey": "•••••" });
    const card = screen.getByTestId("connection-websearch");
    // Let the runs query settle so a late-arriving footnote would have rendered.
    await new Promise((r) => setTimeout(r, 0));
    expect(within(card).queryByText(/Last run:/)).not.toBeInTheDocument();
  });
});
