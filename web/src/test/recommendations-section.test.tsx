import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RecommendationsSection } from "@/components/settings/recommendations-section";
import type { Settings } from "@/lib/types";

const { putSettings } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: { putSettings },
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
  beforeEach(() => {
    putSettings.mockClear();
  });

  it("blocks the AI-from-library source until a curator is configured", () => {
    renderSection({});
    expect(screen.getByLabelText(/suggests from your library/i)).toBeDisabled();
    // Both AI sources (library + web search) show the curator-needed hint.
    expect(screen.getAllByText(/Needs an AI curator/i).length).toBeGreaterThan(
      0,
    );
  });

  it("allows the AI-from-library source once a curator is set", () => {
    renderSection({ "curator.provider": "anthropic" });
    expect(
      screen.getByLabelText(/suggests from your library/i),
    ).not.toBeDisabled();
    expect(screen.queryByText(/Needs an AI curator/i)).toBeNull();
  });

  it("defaults the watched cap to 0% (all fresh) when the setting is unset", () => {
    renderSection({});
    expect(
      screen.getByRole("slider", { name: /already-watched/i }),
    ).toHaveValue("0");
  });

  it("preselects the saved watched cap and auto-saves a change to recommendations.watched_pct", async () => {
    renderSection({ "recommendations.watched_pct": 0.5 });
    const slider = screen.getByRole("slider", { name: /already-watched/i });
    // The stored fraction (0.5) is shown as 50%.
    expect(slider).toHaveValue("50");

    fireEvent.change(slider, { target: { value: "55" } });

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const body = putSettings.mock.calls.at(-1)?.[0];
    expect(body?.["recommendations.watched_pct"]).toBe(0.55);
    // The section owns both keys, so its save carries the sources set too.
    expect(body).toHaveProperty("candidates.sources");
  });
});
