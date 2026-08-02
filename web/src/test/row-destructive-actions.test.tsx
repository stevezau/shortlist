import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowDestructiveActions } from "@/components/rows/row-destructive-actions";
import type { Collection } from "@/lib/types";

const cleanupCollection = vi.fn();
const deleteCollection = vi.fn();

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    cleanupCollection: (id: number, dryRun: boolean) =>
      cleanupCollection(id, dryRun),
    deleteCollection: (id: number) => deleteCollection(id),
  },
}));

function collection(patch: Partial<Collection> = {}): Collection {
  return { id: 7, name: "Hidden Gems", ...patch } as Collection;
}

function renderActions(props: Partial<{ onDeleted: () => void }> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RowDestructiveActions collection={collection()} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RowDestructiveActions", () => {
  beforeEach(() => {
    cleanupCollection.mockReset();
    deleteCollection.mockReset();
    cleanupCollection.mockResolvedValue({ removed: [], message: "Done" });
    deleteCollection.mockResolvedValue({});
  });

  it("previews with a DRY RUN before removing anything from Plex", async () => {
    // The whole safety of this control is that opening it only ever asks Plex what WOULD go. If the
    // first call were the real one, the dialog would have already deleted collections by the time it
    // offered you Cancel.
    renderActions();
    await userEvent.click(
      screen.getByRole("button", { name: /Remove Hidden Gems from Plex/i }),
    );

    await waitFor(() => expect(cleanupCollection).toHaveBeenCalled());
    expect(cleanupCollection).toHaveBeenCalledWith(7, true);
    expect(cleanupCollection).toHaveBeenCalledTimes(1);
  });

  it("will not offer removal when the dry run says there is nothing there", async () => {
    renderActions();
    await userEvent.click(
      screen.getByRole("button", { name: /Remove Hidden Gems from Plex/i }),
    );

    expect(await screen.findByText(/Nothing to remove/i)).toBeInTheDocument();
    const confirm = screen
      .getAllByRole("button", { name: "Remove from Plex" })
      .at(-1);
    expect(confirm).toBeDisabled();
  });

  it("previews, THEN removes for real — in that order", async () => {
    // The dry-run test above only proves the FIRST call is a preview. It passes just as happily if
    // the confirm button is also a dry run, which would report success while removing nothing. The
    // ordering assertion is what pins both calls down.
    cleanupCollection.mockResolvedValueOnce({
      removed: ["Sarah / Movies", "Mike / Movies"],
    });
    renderActions();
    await userEvent.click(
      screen.getByRole("button", { name: /Remove Hidden Gems from Plex/i }),
    );
    expect(
      await screen.findByText(/will remove 2 collections/i),
    ).toBeInTheDocument();

    const confirm = screen
      .getAllByRole("button", { name: "Remove from Plex" })
      .at(-1)!;
    await userEvent.click(confirm);

    await waitFor(() =>
      expect(cleanupCollection.mock.calls).toEqual([
        [7, true],
        [7, false],
      ]),
    );
  });

  it("keeps the real removal disabled when the dry run FAILED", async () => {
    // A failed preview says nothing about what is on the server. Offering the destructive button
    // anyway would remove collections with nobody having seen the diff.
    cleanupCollection.mockRejectedValueOnce(new Error("Plex unreachable"));
    renderActions();
    await userEvent.click(
      screen.getByRole("button", { name: /Remove Hidden Gems from Plex/i }),
    );

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: "Remove from Plex" }).at(-1),
    ).toBeDisabled();
  });

  it("asks before deleting, and only then calls the API", async () => {
    renderActions();
    await userEvent.click(
      screen.getByRole("button", { name: /Delete Hidden Gems/i }),
    );

    // Opening the confirmation must not itself delete anything.
    expect(deleteCollection).not.toHaveBeenCalled();
    expect(await screen.findByText(/can’t be undone/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete row" }));
    await waitFor(() => expect(deleteCollection).toHaveBeenCalledWith(7));
  });

  it("does NOT navigate away when the delete fails", async () => {
    // onDeleted takes the owner off the editor. Firing it on failure would bounce them to the rows
    // list with the row still there and the error unmounted before they could read it.
    deleteCollection.mockRejectedValueOnce(new Error("boom"));
    const onDeleted = vi.fn();
    renderActions({ onDeleted });

    await userEvent.click(
      screen.getByRole("button", { name: /Delete Hidden Gems/i }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Delete row" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Couldn’t delete this row/i,
    );
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("tells the caller once the row is gone, so the editor can leave the page", async () => {
    const onDeleted = vi.fn();
    renderActions({ onDeleted });

    await userEvent.click(
      screen.getByRole("button", { name: /Delete Hidden Gems/i }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Delete row" }));

    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
  });
});
