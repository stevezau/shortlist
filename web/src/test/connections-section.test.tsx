import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectionsSection } from "@/components/settings/connections-section";
import { findProvider } from "@/lib/providers";
import type { Settings } from "@/lib/types";

const { putSettings, testConnection, getRuns } = vi.hoisted(() => ({
  putSettings: vi.fn((v: Settings) => Promise.resolve(v)),
  testConnection: vi.fn((_service: string) =>
    Promise.resolve({ ok: true, message: "ok" }),
  ),
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
      expect(screen.getByTestId("connection-llm")).toBeInTheDocument();
      expect(screen.queryAllByTestId("connection-llm")).toHaveLength(1);
      expect(screen.getByText("AI & Web search")).toBeInTheDocument();
    });

    it("shows only the chosen backend's fields", async () => {
      renderSection({ "llm_web.search_provider": "searxng" });
      const card = screen.getByTestId("connection-llm");
      await userEvent.click(
        within(card).getByRole("button", { name: /edit|set up/i }),
      );

      expect(
        within(card).getByLabelText(/SearXNG address/i),
      ).toBeInTheDocument();
      expect(
        within(card).queryByLabelText(/Exa API key/i),
      ).not.toBeInTheDocument();
    });

    it("swaps the fields when the backend is switched", async () => {
      renderSection({ "llm_web.search_provider": "exa" });
      const card = screen.getByTestId("connection-llm");
      await userEvent.click(
        within(card).getByRole("button", { name: /edit|set up/i }),
      );
      expect(within(card).getByLabelText(/Exa API key/i)).toBeInTheDocument();

      await userEvent.click(
        within(card).getByRole("button", { name: /SearXNG/i }),
      );
      expect(
        within(card).getByLabelText(/SearXNG address/i),
      ).toBeInTheDocument();
      expect(
        within(card).queryByLabelText(/Exa API key/i),
      ).not.toBeInTheDocument();
    });

    it("asks for one backend's fields, never both", () => {
      // Exactly one backend runs, so exactly one is ever configured. Showing both was `auto`'s
      // doing, and `auto` is gone.
      renderSection({ "llm_web.search_provider": "exa" });
      const card = screen.getByTestId("connection-llm");
      return userEvent
        .click(within(card).getByRole("button", { name: /edit|set up/i }))
        .then(() => {
          expect(
            within(card).getByLabelText(/Exa API key/i),
          ).toBeInTheDocument();
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
      const card = screen.getByTestId("connection-llm");
      await userEvent.click(
        within(card).getByRole("button", { name: /edit|set up/i }),
      );
      await userEvent.click(
        within(card).getByRole("button", { name: "SearXNG" }),
      );
      await userEvent.type(
        within(card).getByLabelText(/SearXNG address/i),
        "http://searx.local:8080",
      );
      await userEvent.click(
        within(card).getByRole("button", { name: /^save$/i }),
      );

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
      // The card auto-tests on load to colour its dot, and the `native_search` probe performs a
      // REAL billable web search — so it must never be what gets fired. Asserting the POSITIVE
      // (that it probed "llm" instead) keeps this honest: the old form only checked that
      // "native_search" was absent, which went green whatever the component did once that string
      // became unreachable.
      renderSection({
        "curator.provider": "anthropic",
        "llm_web.search_provider": "native",
      });
      await waitFor(() => expect(getRuns).toHaveBeenCalled());
      const probed = testConnection.mock.calls.map((c) => c[0]);
      expect(probed).not.toContain("native_search");
      expect(probed).toContain("llm");
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
      expect(
        screen.getByText(/SearXNG · http:\/\/searx.local:8080/),
      ).toBeInTheDocument();
    });

    async function openWebSearchCard(settings: Settings) {
      renderSection(settings);
      const card = screen.getByTestId("connection-llm");
      await userEvent.click(
        within(card).getByRole("button", { name: /edit|set up/i }),
      );
      return card;
    }

    it("keeps the depth buttons to a name, with no price crammed in", async () => {
      // Buttons reading "Thorough — $0.012, recommended" made the row unreadable, so the trade-off
      // moved to one line under the row. The names are Exa's own, so the setting can be checked
      // against their dashboard — and only the three modes that actually return titles are offered.
      const card = await openWebSearchCard({
        "llm_web.search_provider": "exa",
        "exa.search_type": "deep-lite",
      });
      const depth = within(card).getByRole("group", { name: /search depth/i });
      const names = within(depth)
        .getAllByRole("button")
        .map((b) => b.textContent?.trim());
      expect(names).toEqual(["Instant", "Deep lite", "Deep"]);
    });

    it("explains the depth you have selected, and only that one", async () => {
      const card = await openWebSearchCard({
        "llm_web.search_provider": "exa",
        "exa.search_type": "deep-lite",
      });
      expect(
        within(card).getByText(/three to four times/i),
      ).toBeInTheDocument();
      expect(
        within(card).queryByText(/without a release year/i),
      ).not.toBeInTheDocument();

      await userEvent.click(
        within(card).getByRole("button", { name: "Instant" }),
      );
      expect(
        within(card).getByText(/without a release year/i),
      ).toBeInTheDocument();
      expect(
        within(card).queryByText(/three to four times/i),
      ).not.toBeInTheDocument();
    });

    it("falls back to the default depth's explanation when none is saved", async () => {
      // A server upgraded from before this setting existed has no stored value, and the backend
      // defaults it to deep-lite — so the blank must read as Deep lite, not as no explanation.
      const card = await openWebSearchCard({
        "llm_web.search_provider": "exa",
      });
      expect(
        within(card).getByText(/three to four times/i),
      ).toBeInTheDocument();
    });

    it("says Gemini won't refresh its picks, without blocking the choice", async () => {
      // Gemini issues no searches for this, but re-measured under the year-anchored prompt it
      // returned 12 of 12 titles from 2024+. The behaviour is real; "its picks are stale" was not.
      // So it stays selectable and the caveat is what it actually is.
      const card = await openWebSearchCard({
        "curator.provider": "google",
        "llm_web.search_provider": "native",
      });
      expect(within(card).getByText(/age with the model/i)).toBeInTheDocument();
      expect(
        within(card).getByRole("button", { name: "Gemini" }),
      ).toBeEnabled();
    });

    it("disables local models under native search — they have no search tool", async () => {
      const card = await openWebSearchCard({
        "llm_web.search_provider": "native",
      });
      expect(
        within(card).getByRole("button", { name: /Local/i }),
      ).toBeDisabled();
      expect(
        within(card).getByRole("button", { name: "Claude" }),
      ).toBeEnabled();
    });

    it("disables None under SearXNG — something must read the snippets", async () => {
      const card = await openWebSearchCard({
        "llm_web.search_provider": "searxng",
      });
      expect(within(card).getByRole("button", { name: "None" })).toBeDisabled();
      expect(
        within(card).getByRole("button", { name: /Local/i }),
      ).toBeEnabled();
    });

    it("enables every provider under Exa, None included", async () => {
      // Exa extracts titles itself, so an AI is genuinely optional — nothing here is off-limits.
      const card = await openWebSearchCard({
        "llm_web.search_provider": "exa",
      });
      for (const name of ["None", "Claude", "OpenAI", "Gemini"]) {
        expect(within(card).getByRole("button", { name })).toBeEnabled();
      }
    });

    it("tells you Exa reads its own results, so no AI is needed", async () => {
      // The setting that used to fail silently and expensively: Exa with the provider set to None
      // paid for every search and threw the titles away. Exa extracts them itself, so this is the
      // one combination that works keyless — and nothing said so.
      const card = await openWebSearchCard({
        "llm_web.search_provider": "exa",
        "curator.provider": "none",
      });
      expect(
        within(card).getByText(/reads its own results/i),
      ).toBeInTheDocument();
    });

    it("tells you SearXNG DOES need one, because it extracts nothing", async () => {
      // The asymmetry that makes this per-backend rather than one sentence: SearXNG returns raw
      // snippets, so something has to read them.
      const card = await openWebSearchCard({
        "llm_web.search_provider": "searxng",
        "curator.provider": "none",
      });
      expect(
        within(card).getByText(/needs an AI provider below/i),
      ).toBeInTheDocument();
    });

    it("probes the AI provider when no external backend is on file", async () => {
      // One Test button, two things worth testing. With no Exa/SearXNG key there is no search to
      // probe, so it tests the AI provider — which also gives heuristic mode a real answer instead
      // of erroring on a search that cannot run.
      renderSection({
        "llm_web.search_provider": "exa",
        "curator.provider": "none",
      });
      await waitFor(() => expect(getRuns).toHaveBeenCalled());
      expect(testConnection.mock.calls.map((c) => c[0])).not.toContain("exa");
    });

    it("probes the external backend once its key is saved", async () => {
      renderSection({
        "llm_web.search_provider": "exa",
        "exa.apikey": "•••••",
      });
      await waitFor(() =>
        expect(testConnection.mock.calls.map((c) => c[0])).toContain("exa"),
      );
    });

    it("spells out why the selected provider can't be used, in text", async () => {
      // A disabled Button carries `disabled:pointer-events-none` and takes no focus, so its tooltip
      // is unreachable by mouse AND keyboard. The reason has to be rendered, not attached.
      const searx = await openWebSearchCard({
        "llm_web.search_provider": "searxng",
        "curator.provider": "none",
      });
      expect(
        within(searx).getByText(/something has to read them/i),
      ).toBeInTheDocument();
    });

    it("says an AI is required under SearXNG once a usable one is picked", async () => {
      const searx = await openWebSearchCard({
        "llm_web.search_provider": "searxng",
        "curator.provider": "anthropic",
      });
      expect(within(searx).getByText(/^Required:/)).toBeInTheDocument();
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
    const card = screen.getByTestId("connection-llm");
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
    const card = screen.getByTestId("connection-llm");
    // Settle the runs query first. Asserting straight away passed whatever the component did,
    // because the note cannot be on screen before the data it renders has arrived.
    await waitFor(() =>
      expect(screen.getByTestId("connection-llm").textContent).toBeDefined(),
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
    const card = screen.getByTestId("connection-llm");
    // Let the runs query settle so a late-arriving footnote would have rendered.
    await new Promise((r) => setTimeout(r, 0));
    expect(within(card).queryByText(/Last run:/)).not.toBeInTheDocument();
  });
});
