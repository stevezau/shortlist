import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RecommendationsSection } from "@/components/settings/recommendations-section";
import type { Settings } from "@/lib/types";

const { putSettings } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: { putSettings, testConnection: vi.fn() },
}));

function renderSection(settings: Settings) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RecommendationsSection settings={settings} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RecommendationsSection", () => {
  beforeEach(() => putSettings.mockClear());

  // The model is "intent + inline fix": a source's toggle is never disabled; when it's on but its
  // dependency is missing, the card shows exactly how to satisfy it right there.

  it("shows an inline Trakt key field when the Trakt source is on without a key", () => {
    renderSection({ "candidates.sources": ["trakt"] });
    expect(screen.getByLabelText(/Trakt API key/i)).toBeInTheDocument();
  });

  it("AI web search: with no curator, prompts to set one up (every backend needs a model)", () => {
    renderSection({
      "curator.provider": "none",
      "exa.apikey": "•••••",
      "candidates.sources": ["llm_web"],
    });
    expect(
      screen.getByText(/needs an AI provider to choose titles/i),
    ).toBeInTheDocument();
  });

  it("AI web search: 'AI provider's own' on a provider that can't self-search (Ollama) warns loudly", () => {
    // Regression: this cell used to show the toggle ON with no prompt while the engine did nothing.
    renderSection({
      "curator.provider": "ollama",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "native",
    });
    expect(
      screen.getByText(/can’t search the web on its own/i),
    ).toBeInTheDocument();
  });

  it("persists an enabled source even when its dependency isn't met yet (intent, not stripped)", async () => {
    renderSection({ "candidates.sources": ["tmdb_similar"] }); // no Trakt key configured
    fireEvent.click(screen.getByLabelText(/Trakt — related titles/i)); // needs a Trakt key
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const sources = putSettings.mock.calls.at(-1)?.[0]?.[
      "candidates.sources"
    ] as string[];
    expect(sources).toContain("trakt"); // kept as intent, NOT stripped for the missing key
  });

  it("AI web search: names the backend and sends you to Connections to change it", () => {
    // The picker and the credential fields moved to the Connections card, so this section must not
    // render a second copy of either — but it still has to say what the source will use.
    renderSection({
      "curator.provider": "ollama",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "searxng",
      "searxng.url": "http://searx.local:8080",
    });
    expect(screen.getByText(/your SearXNG instance/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Exa$/i })).toBeNull();
    expect(screen.queryByLabelText(/Exa API key/i)).toBeNull();
  });

  it("AI web search: never writes llm_web.search_provider — Connections owns it", async () => {
    // This section PUTs its whole object on any change. While it still held a copy of the backend,
    // saving anything here would overwrite a choice just made in Connections with stale state.
    renderSection({
      "curator.provider": "anthropic",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "searxng",
    });
    fireEvent.click(screen.getByLabelText(/TMDB — discover by taste/i));
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(putSettings.mock.calls.at(-1)?.[0]).not.toHaveProperty(
      "llm_web.search_provider",
    );
  });

  it("persists the owner's intent — enabling a source saves it in candidates.sources", async () => {
    renderSection({ "candidates.sources": ["tmdb_similar"] });
    fireEvent.click(screen.getByLabelText(/TMDB — discover by taste/i));
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const sources = putSettings.mock.calls.at(-1)?.[0]?.[
      "candidates.sources"
    ] as string[];
    expect(sources).toContain("tmdb_discover");
  });

  it("auto-saves a change to the watched cap and carries the sources set too", async () => {
    renderSection({ "recommendations.watched_pct": 0.5 });
    const slider = screen.getByRole("slider", { name: /already-watched/i });
    expect(slider).toHaveValue("50");
    fireEvent.change(slider, { target: { value: "55" } });
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const body = putSettings.mock.calls.at(-1)?.[0];
    expect(body?.["recommendations.watched_pct"]).toBe(0.55);
    expect(body).toHaveProperty("candidates.sources");
  });

  it("auto-saves the recent-releases weight as a 0..1 fraction", async () => {
    renderSection({ "recommendations.recency": 0.5 });
    const slider = screen.getByRole("slider", { name: /release date counts/i });
    expect(slider).toHaveValue("50");
    fireEvent.change(slider, { target: { value: "75" } });
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(
      putSettings.mock.calls.at(-1)?.[0]?.["recommendations.recency"],
    ).toBe(0.75);
  });

  it("shows a server that chose to turn it off as off, not as the shipped default", () => {
    // The control must render the STORED value, never the default — otherwise the UI advertises
    // ranking the engine is not doing for anyone who deliberately turned it back down.
    renderSection({ "recommendations.recency": 0 });
    expect(
      screen.getByRole("slider", { name: /release date counts/i }),
    ).toHaveValue("0");
  });

  it("shows a fresh install at the shipped default", () => {
    // No stored row: every server, new or upgraded, follows DEFAULTS (0.5).
    renderSection({});
    expect(
      screen.getByRole("slider", { name: /release date counts/i }),
    ).toHaveValue("50");
  });

  it("keeps recent releases and the rebuild cadence as two separate controls", () => {
    // They are near-synonyms in English and completely different settings here. If one ever
    // replaces the other in this card, the owner silently loses a control.
    renderSection({});
    expect(
      screen.getByRole("slider", { name: /release date counts/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("spinbutton", { name: /how often the row rebuilds/i }),
    ).toBeInTheDocument();
  });

  it("saves the cold-start choice, and says what it will actually do", async () => {
    renderSection({ "recommendations.cold_start": "popular" });
    const select = screen.getByLabelText(/hasn’t watched enough/i);
    expect(select).toHaveValue("popular");

    fireEvent.change(select, { target: { value: "skip" } });

    // The consequence updates with the choice — this is the line that tells an owner the setting
    // REMOVES a row, which the option label alone never says.
    expect(
      screen.getByText(/any row they already have is removed/i),
    ).toBeInTheDocument();
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(
      putSettings.mock.calls.at(-1)?.[0]?.["recommendations.cold_start"],
    ).toBe("skip");
  });

  it("saves the history threshold the cold-start choice hangs off", async () => {
    renderSection({ "recommendations.min_history": 10 });
    const input = screen.getByLabelText(/Enough watch history/i);
    expect(input).toHaveValue(10);

    fireEvent.change(input, { target: { value: "4" } });

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(
      putSettings.mock.calls.at(-1)?.[0]?.["recommendations.min_history"],
    ).toBe(4);
  });
});
