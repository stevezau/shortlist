import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RequestHoldsField } from "@/components/request-holds-field";
import { readHoldTags, writeHoldTags } from "@/lib/request-holds";
import type { HoldPreview, SeasonTag } from "@/lib/types";

const { previewHolds } = vi.hoisted(() => ({ previewHolds: vi.fn() }));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    previewHolds: (body: unknown) => previewHolds(body),
    getTmdbTags: () => Promise.resolve([]),
  },
}));

function Harness({ genres = [], tags = [] }: { genres?: number[]; tags?: SeasonTag[] }) {
  const [g, setG] = useState(genres);
  const [t, setT] = useState(tags);
  return <RequestHoldsField genres={g} tags={t} onGenres={setG} onTags={setT} />;
}

function renderField(props: { genres?: number[]; tags?: SeasonTag[] } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Harness {...props} />
    </QueryClientProvider>,
  );
}

const PREVIEW: HoldPreview = {
  checked: 14,
  held: [
    { tmdb_id: 1, title: "BTS: Permission to Dance on Stage", year: 2022, reason: "tag “concert”", story: false },
    { tmdb_id: 2, title: "A Star Is Born", year: 2018, reason: "tag “concert”", story: true },
  ],
  unread: 1,
};

describe("RequestHoldsField", () => {
  beforeEach(() => {
    previewHolds.mockReset();
    previewHolds.mockResolvedValue(PREVIEW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("asks nothing of the server until something is picked", async () => {
    vi.useFakeTimers();
    renderField();
    expect(screen.getByText(/Nothing picked, so any movie can be requested automatically/i)).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(previewHolds).not.toHaveBeenCalled();
  });

  it("toggles a genre and previews it against the inbox", async () => {
    renderField();
    const music = screen.getByRole("button", { name: "Music" });
    expect(music).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(music);
    expect(music).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(previewHolds).toHaveBeenCalledWith({ genres: [10402], tags: [] }));
    expect(await screen.findByText(/Would hold 2 of the 14 movies waiting in your inbox/i)).toBeInTheDocument();
  });

  it("flags a held story film so a too-broad pick shows itself", async () => {
    renderField({ tags: [{ id: 6029, name: "concert" }] });
    expect(await screen.findByText(/1 of these is a story film/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Story film")).toBeInTheDocument();
    expect(screen.getByText(/TMDB didn.t answer for 1 movie/i)).toBeInTheDocument();
  });

  it("adds a suggested tag, then stops suggesting it, and removes it again", async () => {
    renderField();
    await userEvent.click(screen.getByRole("button", { name: "Add tag concert film" }));
    expect(screen.queryByRole("button", { name: "Add tag concert film" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Remove tag concert film/i }));
    expect(screen.getByRole("button", { name: "Add tag concert film" })).toBeInTheDocument();
  });

  it("says plainly when the picks hold nothing waiting", async () => {
    previewHolds.mockResolvedValue({ checked: 14, held: [], unread: 0 });
    renderField({ genres: [37] });
    expect(await screen.findByText(/Holds none of the 14 movies waiting in your inbox/i)).toBeInTheDocument();
  });

  it("offers a retry when the preview fails", async () => {
    previewHolds.mockRejectedValue(new Error("boom"));
    renderField({ genres: [37] });
    expect(await screen.findByText(/Couldn.t check your inbox just now/i)).toBeInTheDocument();
    previewHolds.mockResolvedValue({ checked: 3, held: [], unread: 0 });
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/Holds none of the 3 movies/i)).toBeInTheDocument();
  });
});

describe("hold tag storage", () => {
  it("round-trips the saved {id: name} map and drops anything malformed", () => {
    const tags = [
      { id: 156205, name: "concert film" },
      { id: 9716, name: "stand-up comedy" },
    ];
    // A JS object lists integer-like keys in numeric order, so the round trip keeps the tags, not their order.
    expect(readHoldTags(writeHoldTags(tags))).toEqual([...tags].sort((a, b) => a.id - b.id));
    expect(readHoldTags({ abc: "x", "5": 7, "6": "ok" })).toEqual([{ id: 6, name: "ok" }]);
    expect(readHoldTags(["156205"])).toEqual([]);
    expect(readHoldTags(undefined)).toEqual([]);
  });
});
