import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AiTryIt } from "@/components/rows/ai-try-it";
import { ApiError } from "@/lib/api";
import type * as ApiModule from "@/lib/api";
import type { Collection, User } from "@/lib/types";

const api = vi.hoisted(() => ({ startRun: vi.fn(), getRun: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...actual.api,
      ...api,
      posterImageUrl: () => "",
    },
  };
});

const row = { id: 9, slug: "twist-endings", theme_id: 5 } as unknown as Collection;
const sarah = { id: 3, slug: "sarah", username: "sarah", display_name: "Sarah", enabled: true } as unknown as User;
const mike = { id: 4, slug: "mike", username: "mike", display_name: "Mike", enabled: true } as unknown as User;

function runResult(patch: Record<string, unknown> = {}) {
  return {
    id: 77,
    status: "success",
    finished_at: "2026-10-04T03:00:00Z",
    users: [
      {
        slug: "sarah",
        username: "sarah",
        display_name: "Sarah",
        status: "done",
        error: null,
        reason: null,
        picks: [
          { rank: 1, title: "Se7en", reason: "A twist in the last minute", rating_key: 0, sources: ["theme"], affinity: 1, seed_title: null },
          { rank: 2, title: "The Prestige", reason: "Magic, and a twist", rating_key: 0, sources: ["theme"], affinity: 1, seed_title: null },
        ],
        ...patch,
      },
    ],
    shared_rows: [],
  };
}

function renderIt(props: { collection?: Collection | null; users?: User[]; unsaved?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </MemoryRouter>
    );
  }
  return render(
    <AiTryIt
      collection={props.collection === undefined ? row : props.collection}
      users={props.users ?? [sarah, mike]}
      unsaved={props.unsaved ?? false}
    />,
    { wrapper: Wrapper },
  );
}

beforeEach(() => {
  api.startRun.mockReset();
  api.getRun.mockReset();
  api.startRun.mockResolvedValue({ run_id: 77 });
  api.getRun.mockResolvedValue(runResult());
});

describe("AiTryIt", () => {
  it("explains that a row has to be saved before it can be tried", () => {
    renderIt({ collection: null });

    expect(screen.getByText(/add the row first/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try it/i })).not.toBeInTheDocument();
  });

  it("says there is nobody to try it for, and what to do", () => {
    renderIt({ users: [] });

    expect(screen.getByText(/no one to try it for yet/i)).toBeInTheDocument();
  });

  it("runs the row for the chosen person as a dry run, scoped to this row, and shows the picks with reasons", async () => {
    renderIt();
    await userEvent.selectOptions(screen.getByLabelText("Try it for"), "Sarah");

    await userEvent.click(screen.getByRole("button", { name: /^Try it/ }));

    expect(await screen.findByText("Se7en")).toBeInTheDocument();
    expect(api.startRun).toHaveBeenCalledWith({ dry_run: true, user_ids: [3], collection_ids: [9] });
    expect(screen.getByText(/A twist in the last minute/)).toBeInTheDocument();
    expect(screen.getByText("The Prestige")).toBeInTheDocument();
    expect(screen.getByText(/nothing is written to plex/i)).toBeInTheDocument();
  });

  it("runs a switched-off row, since a new AI row starts off and Try it is how it is looked over", async () => {
    renderIt({ collection: { ...row, enabled: false } as Collection });

    await userEvent.click(screen.getByRole("button", { name: /^Try it/ }));

    expect(await screen.findByText("Se7en")).toBeInTheDocument();
    expect(api.startRun).toHaveBeenCalledWith({ dry_run: true, user_ids: [3], collection_ids: [9] });
    expect(screen.getByRole("button", { name: /^Try it/ })).toBeEnabled();
    expect(screen.getByText(/nothing is written to plex/i)).toBeInTheDocument();
  });

  it("runs it for the person picked, not the first", async () => {
    renderIt();
    await userEvent.selectOptions(screen.getByLabelText("Try it for"), "Mike");
    api.getRun.mockResolvedValue({ ...runResult(), users: [{ ...runResult().users[0], slug: "mike", username: "mike" }] });

    await userEvent.click(screen.getByRole("button", { name: /^Try it/ }));

    await waitFor(() => expect(api.startRun).toHaveBeenCalledWith({ dry_run: true, user_ids: [4], collection_ids: [9] }));
  });

  it("shows a skeleton while the run is going", async () => {
    api.getRun.mockResolvedValue({ ...runResult(), finished_at: null, status: "running", users: [] });
    renderIt();

    await userEvent.click(screen.getByRole("button", { name: /^Try it/ }));

    expect(await screen.findByRole("status", { name: /running/i })).toBeInTheDocument();
  });

  it("groups the picks by library with a heading each, ranks restarting in every group", async () => {
    const pick = (rank: number, title: string) => ({
      rank, title, reason: "why", rating_key: 0, sources: ["theme"], affinity: 1, seed_title: null,
    });
    const slice = (library_title: string, picks: ReturnType<typeof pick>[]) => ({
      row_slug: "twists", row_title: "Twists", library_key: library_title, library_title,
      added: [], removed: [], kept: [], deleted: [], created: false, picks,
    });
    api.getRun.mockResolvedValue(
      runResult({
        breakdown: [
          slice("Movies", [pick(1, "Se7en"), pick(2, "The Prestige")]),
          slice("TV Shows", [pick(1, "Dark")]),
        ],
      }),
    );
    renderIt();

    await userEvent.click(screen.getByRole("button", { name: /^Try it/ }));

    const movies = await screen.findByRole("group", { name: "Movies" });
    expect(within(movies).getByText("The Prestige")).toBeInTheDocument();
    expect(within(movies).queryByText("Dark")).toBeNull();
    expect(within(screen.getByRole("group", { name: "TV Shows" })).getByText("Dark")).toBeInTheDocument();
  });

  it("says why nothing was picked, and what to do about it", async () => {
    api.getRun.mockResolvedValue(runResult({ picks: [], reason: "nothing on the server matched the list" }));
    renderIt();

    await userEvent.click(screen.getByRole("button", { name: /^Try it/ }));

    expect(await screen.findByText(/picked nothing for Sarah/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing on the server matched the list/i)).toBeInTheDocument();
  });

  it("says when the run could not be started, and tries again", async () => {
    api.startRun.mockRejectedValueOnce(new ApiError(409, "A run is already going."));
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: /^Try it/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("A run is already going.");
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));

    expect(await screen.findByText("Se7en")).toBeInTheDocument();
  });

  it("says plainly that it couldn't be tried, and never prints the raw run error", async () => {
    api.getRun.mockResolvedValue(runResult({ picks: [], status: "error", error: "Plex didn't answer in time" }));
    renderIt();

    await userEvent.click(screen.getByRole("button", { name: /^Try it/ }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/couldn.t be tried/i);
    expect(alert).toHaveTextContent(/nothing was written to plex/i);
    expect(alert).not.toHaveTextContent(/answer in time/i);
  });

  it("holds Try it back while there are unsaved changes, since it runs the saved row", () => {
    renderIt({ unsaved: true });

    expect(screen.getByRole("button", { name: /^Try it/ })).toBeDisabled();
    expect(screen.getByText(/save the row first/i)).toBeInTheDocument();
  });
});
