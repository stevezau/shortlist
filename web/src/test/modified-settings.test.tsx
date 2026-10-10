import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, expect, it, vi } from "vitest";

import { DefaultsSection } from "@/components/settings/defaults-section";
import { RecommendationsSection } from "@/components/settings/recommendations-section";
import type { Settings } from "@/lib/types";

const { putSettings, getSettingDefaults } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
  getSettingDefaults: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: { putSettings, getSettingDefaults, previewWebPrompt: vi.fn(), testConnection: vi.fn() },
}));

const DEFAULTS: Settings = {
  "candidates.sources": ["tmdb_similar", "tmdb_discover"],
  "recommendations.refresh_days": 8,
  "recommendations.watched_pct": 0,
  "recommendations.recency": 0.5,
  "row.name_template": "✨ {library_name} Picked for You",
  "row.size": 15,
};

function renderWith(node: React.ReactNode) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  putSettings.mockClear();
  getSettingDefaults.mockResolvedValue(DEFAULTS);
});

it("marks only the settings that differ from their default, with the default and a count", async () => {
  renderWith(
    <RecommendationsSection
      settings={{ ...DEFAULTS, "recommendations.refresh_days": 30, "recommendations.recency": 0.5 }}
    />,
  );

  expect(await screen.findByText("· 1 modified")).toBeInTheDocument();
  expect(screen.getAllByText("Modified")).toHaveLength(1);
  expect(screen.getByText(/default: 8 days/)).toBeInTheDocument();
});

it("marks nothing while the defaults are unknown, and nothing when every value is the default", async () => {
  renderWith(<RecommendationsSection settings={DEFAULTS} />);

  await waitFor(() => expect(getSettingDefaults).toHaveBeenCalled());
  expect(screen.queryByText("Modified")).not.toBeInTheDocument();
  expect(screen.queryByText(/modified$/)).not.toBeInTheDocument();
});

it("resets a modified setting through its own state, so the normal auto-save writes the default", async () => {
  renderWith(<DefaultsSection settings={{ ...DEFAULTS, "row.size": 25 }} />);

  await userEvent.click(await screen.findByRole("button", { name: "Reset How many titles to default" }));

  await waitFor(() => expect(putSettings).toHaveBeenCalledWith(expect.objectContaining({ "row.size": 15 })));
  expect(screen.queryByText("Modified")).not.toBeInTheDocument();
});
