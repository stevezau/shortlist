import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api";
import type { RunDetail } from "@/lib/types";
import { RunUserTracePage } from "@/pages/run-user-trace";

function renderRowTrace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/runs/12/trace/row/popular"]}>
        <Routes>
          <Route path="/runs/:id/trace/row/:rowSlug" element={<RunUserTracePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function run(sharedRows: RunDetail["shared_rows"]): RunDetail {
  return { id: 12, status: "ok", shared_rows: sharedRows, users: [] } as unknown as RunDetail;
}

describe("Row trace — a row the run never built", () => {
  beforeEach(() => {
    vi.spyOn(api, "listCollections").mockResolvedValue([]);
    vi.spyOn(api, "getRunSharedRowTrace").mockRejectedValue(
      new ApiError(404, "no such shared row in this run"),
    );
  });
  afterEach(() => vi.restoreAllMocks());

  it("says the run built no shared rows, links back to it, and offers no Retry", async () => {
    vi.spyOn(api, "getRun").mockResolvedValue(run([]));
    renderRowTrace();

    expect(await screen.findByText("This run built no shared rows")).toBeInTheDocument();
    expect(screen.queryByText(/no such shared row/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to run #12" })).toHaveAttribute("href", "/runs/12");
  });

  it("says this row was not part of the run when the run did build other shared rows", async () => {
    vi.spyOn(api, "getRun").mockResolvedValue(
      run([{ collection_slug: "family" }] as unknown as RunDetail["shared_rows"]),
    );
    renderRowTrace();

    expect(await screen.findByText("This row was not part of run #12")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });

  it("keeps Retry for a failure that is not a 404", async () => {
    vi.spyOn(api, "getRunSharedRowTrace").mockRejectedValue(new ApiError(500, "Server error"));
    vi.spyOn(api, "getRun").mockResolvedValue(run([]));
    renderRowTrace();

    expect(await screen.findByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});
