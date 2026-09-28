/**
 * The "Your requests" row's own block in the editor (issue #127): the window field, the sources
 * panel, and the own-tags preview.
 *
 * The panel is what tells an owner whether the row CAN know who asked for what, so what it pins is
 * that every source's state is shown as the server read it (never inferred from `problems`), that
 * the panel loads once and then only on Check (the endpoint makes dozens of external calls), and
 * that the tag preview says plainly when a tag fits more than one person.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { YourRequestsBlock } from "@/components/rows/row-kind-settings";
import type * as ApiModule from "@/lib/api";
import { blankInput } from "@/lib/collections";
import { visibleSettings } from "@/lib/row-kinds";
import type { CollectionInput, RowSources } from "@/lib/types";
import { CTX } from "@/test/row-kind-fixtures";

const { getRequestRowSources } = vi.hoisted(() => ({
  getRequestRowSources: vi.fn<(pattern: string) => Promise<RowSources>>(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getRequestRowSources: (pattern: string) => getRequestRowSources(pattern),
    },
  };
});

const CONNECTED: RowSources = {
  overseerr: "connected",
  radarr: "off",
  sonarr: "off",
  complete: true,
  problems: [],
  seerr_requests: 7,
  seerr_requesters: 6,
  seerr_linked: 6,
  servers: [],
  tagged_movies: 0,
  tagged_shows: 0,
  people: [],
  tags: [],
};

const NOTHING: RowSources = {
  ...CONNECTED,
  overseerr: "off",
  complete: false,
  seerr_requests: 0,
  seerr_requesters: 0,
  seerr_linked: 0,
};

/** The block on a live form: `set` merges into the row, as the editor's does. */
function Harness({ initial, set }: { initial: CollectionInput; set: (patch: Partial<CollectionInput>) => void }) {
  const [input, setInput] = useState(initial);
  return (
    <YourRequestsBlock
      input={input}
      set={(patch) => {
        set(patch);
        setInput((current) => ({ ...current, ...patch }));
      }}
      ctx={CTX}
      shown={visibleSettings(input, CTX)}
      hidden={[]}
      settings={undefined}
      users={[]}
    />
  );
}

function renderBlock(input: CollectionInput) {
  const set = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <Harness initial={input} set={set} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return set;
}

const requestsRow = (patch: Partial<CollectionInput> = {}): CollectionInput => ({
  ...blankInput(),
  requests_row: true,
  requests_window_days: 90,
  ...patch,
});

beforeEach(() => {
  getRequestRowSources.mockReset();
  getRequestRowSources.mockResolvedValue(CONNECTED);
});

describe("YourRequestsBlock", () => {
  it("shows each source's state and the window field", async () => {
    renderBlock(requestsRow());
    expect(screen.getByLabelText("Show titles that landed in the last")).toHaveValue(90);
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByText(/7 requests/)).toBeInTheDocument();
    expect(screen.getAllByText("Off")).toHaveLength(2);
    expect(
      screen.getByText(
        "Older arrivals drop off, so a request they’ve lost interest in doesn’t sit there for good. 0 keeps every title until they’ve watched it.",
      ),
    ).toBeInTheDocument();
  });

  it("writes the window back as a whole number of days, never below 0", () => {
    const set = renderBlock(requestsRow());
    const field = screen.getByLabelText("Show titles that landed in the last");
    fireEvent.change(field, { target: { value: "30" } });
    expect(set).toHaveBeenLastCalledWith({ requests_window_days: 30 });
    expect(field).toHaveValue(30);
    fireEvent.change(field, { target: { value: "-4" } });
    expect(set).toHaveBeenLastCalledWith({ requests_window_days: 0 });
    fireEvent.change(field, { target: { value: "2.6" } });
    expect(set).toHaveBeenLastCalledWith({ requests_window_days: 3 });
  });

  it("reads the sources once on mount with the saved pattern, and again only on Check", async () => {
    renderBlock(requestsRow({ requests_tag_pattern: "req-{username}" }));
    await screen.findByText("Connected");
    expect(getRequestRowSources).toHaveBeenCalledTimes(1);
    expect(getRequestRowSources).toHaveBeenCalledWith("req-{username}");

    const pattern = screen.getByLabelText("Tag pattern");
    await userEvent.type(pattern, "-x");
    expect(pattern).toHaveValue("req-{username}-x");
    expect(getRequestRowSources).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Check" }));
    await waitFor(() => expect(getRequestRowSources).toHaveBeenCalledTimes(2));
    expect(getRequestRowSources).toHaveBeenLastCalledWith("req-{username}-x");

    // The same pattern again is still a fresh read, not a cache hit.
    await userEvent.click(await screen.findByRole("button", { name: "Check" }));
    await waitFor(() => expect(getRequestRowSources).toHaveBeenCalledTimes(3));
  });

  it("lists the tags it found, and says when one fits more than one person", async () => {
    getRequestRowSources.mockResolvedValue({
      ...CONNECTED,
      tags: [
        { label: "req-sarah", source: "pattern", user_id: 2, display_name: "Sarah", titles: 4, ambiguous: false },
        { label: "req-mike", source: "pattern", user_id: null, display_name: "", titles: 1, ambiguous: true },
      ],
    });
    renderBlock(requestsRow({ requests_tag_pattern: "req-{username}" }));
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]!).getByText("req-sarah")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("Sarah")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("4")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("No one")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("Fits more than one person")).toBeInTheDocument();
  });

  it("repeats the server's problems word for word", async () => {
    getRequestRowSources.mockResolvedValue({
      ...CONNECTED,
      overseerr: "unreachable",
      problems: ["Overseerr: connection refused"],
    });
    renderBlock(requestsRow());
    expect(await screen.findByText("Unreachable")).toBeInTheDocument();
    expect(screen.getByText("Overseerr: connection refused")).toBeInTheDocument();
  });

  it("says when nothing can tell it who asked for what, and where to fix that", async () => {
    getRequestRowSources.mockResolvedValue(NOTHING);
    renderBlock(requestsRow());
    expect(await screen.findAllByText("Off")).toHaveLength(3);
    expect(
      screen.getByText(
        /Needs a way to know who asked for what: an Overseerr or Jellyseerr connection, or Radarr\/Sonarr with request tags\./,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Settings/ })).toHaveAttribute("href", "/settings#connections");
  });

  it("re-reads the sources from Check again in the panel header, with the pattern as it stands", async () => {
    // Check lives inside the folded "Use my own tags" details, so re-checking a source that just
    // came back needed the pattern opened first.
    renderBlock(requestsRow({ requests_tag_pattern: "req-{username}" }));
    await screen.findByText("Connected");
    expect(getRequestRowSources).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(getRequestRowSources).toHaveBeenCalledTimes(2));
    expect(getRequestRowSources).toHaveBeenLastCalledWith("req-{username}");
  });

  it("says when the check itself failed, and Check tries again", async () => {
    getRequestRowSources.mockRejectedValueOnce(new Error("boom"));
    renderBlock(requestsRow());
    expect(await screen.findByText(/Couldn.t check the sources/)).toBeInTheDocument();
    getRequestRowSources.mockResolvedValue(CONNECTED);
    await userEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("Connected")).toBeInTheDocument();
  });

  it("explains that a person with nothing ready gets no row", async () => {
    renderBlock(requestsRow());
    expect(
      screen.getByText(
        "A person with nothing ready gets no row. It appears on the first run after something they asked for lands, and goes again once they’ve watched everything in it.",
      ),
    ).toBeInTheDocument();
  });
});
