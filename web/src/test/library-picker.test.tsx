import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LibraryPicker } from "@/components/rows/library-picker";
import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";

const { getLibraries } = vi.hoisted(() => ({ getLibraries: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { getLibraries: () => getLibraries() } };
});

function renderPicker(
  libraryKeys: string[],
  media: "movie" | "show" | "both" = "both",
  onChange: (next: { library_keys: string[]; media: string }) => void = () => {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <LibraryPicker
        libraryKeys={libraryKeys}
        media={media}
        onChange={onChange}
      />
    </QueryClientProvider>,
  );
}

describe("LibraryPicker", () => {
  // Braces matter: an arrow that RETURNS the mock hands vitest a teardown callback, which it then
  // calls — invoking the rejecting mock with nobody listening.
  beforeEach(() => {
    getLibraries.mockReset();
  });

  it("doesn't claim a targeted row will build everywhere when the library list fails", async () => {
    getLibraries.mockRejectedValue(new ApiError(0, "Plex is unreachable."));
    renderPicker(["2"]);

    const alert = await screen.findByRole("alert");
    // The old copy said "this row will build in all of them" — the opposite of what happens: a row
    // with saved library_keys goes on building ONLY in those.
    expect(alert).toHaveTextContent(/Plex is unreachable/i);
    expect(alert).toHaveTextContent(/keeps building in the one library/i);
    expect(alert).not.toHaveTextContent(/build in all of them/i);
    expect(screen.getByRole("button", { name: /Try again/i })).toBeTruthy();
  });

  it("says the row follows every library when it has no targets and the list fails", async () => {
    getLibraries.mockRejectedValue(new ApiError(0, "Plex is unreachable."));
    renderPicker([]);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /keeps building in every library/i,
    );
  });

  it("retries the library list on demand", async () => {
    getLibraries.mockRejectedValueOnce(new ApiError(0, "Plex is unreachable."));
    getLibraries.mockResolvedValue([
      { key: "1", title: "Movies", type: "movie" },
    ]);
    renderPicker([]);

    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: /Try again/i }));

    expect(await screen.findByText("Movies")).toBeTruthy();
  });

  it("distinguishes an empty Plex server from a failed lookup", async () => {
    getLibraries.mockResolvedValue([]);
    renderPicker([]);

    expect(
      await screen.findByText(/no movie or TV libraries yet/i),
    ).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("LibraryPicker — an empty selection means every library OF THE ROW'S TYPE", () => {
  const BOTH_TYPES = [
    { key: "1", title: "Movies", type: "movie" },
    { key: "2", title: "TV Shows", type: "show" },
  ];

  it("does not tick libraries a typed row will never build in", async () => {
    // A movies-only row rendered its TV libraries ticked while never building there — and the
    // shelf-position list directly below it disagreed, naming only the movie ones.
    getLibraries.mockResolvedValue(BOTH_TYPES);
    renderPicker([], "movie");

    expect(await screen.findByRole("checkbox", { name: /Movies/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /TV Shows/ })).not.toBeChecked();
  });

  it("still ticks everything for a row that covers both", async () => {
    getLibraries.mockResolvedValue(BOTH_TYPES);
    renderPicker([], "both");

    expect(await screen.findByRole("checkbox", { name: /Movies/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /TV Shows/ })).toBeChecked();
  });

  it("keeps the follow-the-server form when a typed row's libraries are all ticked", async () => {
    // Storing explicit keys here would quietly stop the row picking up a library added later.
    // Compared against the libraries of that TYPE, not against every library on the server.
    getLibraries.mockResolvedValue([
      ...BOTH_TYPES,
      { key: "3", title: "4K Movies", type: "movie" },
    ]);
    const onChange = vi.fn();
    renderPicker(["1"], "movie", onChange);

    await screen.findByRole("checkbox", { name: /4K Movies/ });
    await userEvent.click(screen.getByRole("checkbox", { name: /4K Movies/ }));

    expect(onChange).toHaveBeenCalledWith({ library_keys: [], media: "movie" });
  });

  it("switches the row to both when a library of the other type is added", async () => {
    getLibraries.mockResolvedValue(BOTH_TYPES);
    const onChange = vi.fn();
    renderPicker([], "movie", onChange);

    await screen.findByRole("checkbox", { name: /TV Shows/ });
    await userEvent.click(screen.getByRole("checkbox", { name: /TV Shows/ }));

    // Deliberate, and now visible: ticking a TV library on a movies row makes it a both row.
    expect(onChange).toHaveBeenCalledWith({
      library_keys: [],
      media: "both",
    });
  });
});
