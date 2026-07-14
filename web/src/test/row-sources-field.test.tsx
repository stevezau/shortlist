import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RowSourcesField } from "@/components/rows/row-sources-field";

vi.mock("@/lib/api", () => ({
  api: { getSettings: () => Promise.resolve({}) },
}));

function renderField(value: string[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RowSourcesField value={value} onChange={() => {}} />
    </QueryClientProvider>,
  );
}

describe("RowSourcesField", () => {
  it("shows the inherit message and no source switches when empty (global default)", () => {
    renderField([]);
    expect(
      screen.getByText(/uses the sources you enabled in Settings/i),
    ).toBeTruthy();
    expect(screen.queryByLabelText(/for this row/i)).toBeNull();
  });

  it("reveals per-source switches when the row overrides sources", () => {
    renderField(["tmdb_similar"]);
    // The custom mode renders a switch per known source, each labelled "…for this row".
    expect(
      screen.getByLabelText(/Enable TMDB — similar titles for this row/i),
    ).toBeTruthy();
    expect(
      screen.getByLabelText(/Enable Trakt — related titles for this row/i),
    ).toBeTruthy();
  });
});
