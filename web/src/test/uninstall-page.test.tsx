import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { UninstallPage } from "@/pages/uninstall";

const { uninstall } = vi.hoisted(() => ({ uninstall: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { uninstall: (dry: boolean) => uninstall(dry) } };
});

// useSSE opens an EventSource; jsdom has none, so stub a no-op one.
class FakeEventSource {
  addEventListener() {}
  close() {}
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
}
vi.stubGlobal("EventSource", FakeEventSource);

const PREVIEW = {
  filters_restored: 48,
  filters_skipped: [],
  filters_unreachable: [],
  filters_failed: [],
  collections_deleted: ["✨ Movies Picked for You", "✨ TV Shows Picked for You"],
  rows_disabled: 3,
  dry_run: true,
  message: "Preview only — nothing was changed.",
};

/** The page asks for the dry run on mount and the real run on confirm; answer each with its own result. */
function answer(preview: unknown, real: unknown = preview) {
  uninstall.mockImplementation((dry: boolean) => Promise.resolve(dry ? preview : real));
}

async function confirmAndUninstall() {
  // The confirm button is locked until the preview has loaded, so wait for its counts first.
  await screen.findByText(/^Restores /);
  await userEvent.type(screen.getByLabelText(/type/i), "uninstall shortlist");
  await userEvent.click(screen.getByRole("button", { name: /uninstall and restore server/i }));
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UninstallPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UninstallPage", () => {
  beforeEach(() => { uninstall.mockReset(); });

  it("explains a failed preview, keeps the uninstall locked, and lets the owner retry the preview", async () => {
    uninstall.mockRejectedValue(new Error("Preview unavailable"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/preview/i);
    await userEvent.type(screen.getByLabelText(/type/i), "uninstall shortlist");
    expect(screen.getByRole("button", { name: /uninstall and restore server/i })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(uninstall).toHaveBeenCalledTimes(2);
    expect(uninstall).toHaveBeenLastCalledWith(true);
  });

  it("gates the destructive action behind the exact confirm phrase", async () => {
    answer(PREVIEW);
    renderPage();
    await screen.findByText(/^Restores /);
    const button = screen.getByRole("button", {
      name: /uninstall and restore server/i,
    });
    expect(button).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/type/i), "uninstall shortlist");
    expect(button).toBeEnabled();
  });

  it("keeps the confirm button disabled until the preview has loaded", async () => {
    let resolvePreview: (value: unknown) => void = () => {};
    uninstall.mockImplementation(() => new Promise((resolve) => { resolvePreview = resolve; }));
    renderPage();

    await userEvent.type(screen.getByLabelText(/type/i), "uninstall shortlist");
    const button = screen.getByRole("button", { name: /uninstall and restore server/i });
    expect(button).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(/working out what uninstall will change/i);

    resolvePreview(PREVIEW);
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("previews the plan with a dry run on arrival, counts first, with no button to press", async () => {
    answer(PREVIEW);
    renderPage();

    expect(uninstall).toHaveBeenCalledWith(true);
    expect(screen.queryByRole("button", { name: /preview/i })).not.toBeInTheDocument();
    const counts = await screen.findByText(/^Restores /);
    expect(counts).toHaveTextContent(
      "Restores 48 share filters from their snapshots, deletes 2 collections and switches off 3 rows.",
    );
    expect(screen.getByText(/Preview only/i)).toBeInTheDocument();
    // The counts come before the controls that act on them.
    const confirm = screen.getByLabelText(/type/i);
    expect(counts.compareDocumentPosition(confirm) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("uses singular words for a count of one", async () => {
    answer({ ...PREVIEW, filters_restored: 1, collections_deleted: ["✨ Picked for You"], rows_disabled: 1 });
    renderPage();

    expect(await screen.findByText(/^Restores /)).toHaveTextContent(
      "Restores 1 share filter from its snapshot, deletes 1 collection and switches off 1 row.",
    );
  });

  it("shows a completion summary of what it did when the uninstall finishes", async () => {
    uninstall.mockResolvedValue({
      filters_restored: 48,
      filters_skipped: [],
      filters_unreachable: [],
      filters_failed: [],
      collections_deleted: ["a", "b"],
      rows_disabled: 3,
      dry_run: false,
      message: "Your server is as we found it.",
    });
    renderPage();

    await confirmAndUninstall();

    expect(await screen.findByText(/Uninstall complete/i)).toBeInTheDocument();
    // The three counts of what actually happened are surfaced.
    expect(screen.getByText(/48 share filters restored/i)).toBeInTheDocument();
    expect(screen.getByText(/3 rows.*switched off/i)).toBeInTheDocument();
  });
  it("names the accounts that could not be restored, and does not call that complete", async () => {
    // Issue #96: an account that has left Plex, and one plex.tv refused, are different problems —
    // one is nothing the owner can act on, the other is worth retrying. A single count would hide
    // both, and a green "Uninstall complete" over them would be the false claim the fix removed.
    uninstall.mockResolvedValue({
      filters_restored: 46,
      filters_skipped: [
        {
          user: "mike",
          plex_account_id: 839623727,
          reason: "no longer on this Plex server",
        },
      ],
      filters_unreachable: [],
      filters_failed: [
        { user: "sarah", error: "RuntimeError: plex.tv said no" },
      ],
      collections_deleted: ["a"],
      rows_disabled: 3,
      dry_run: false,
      message:
        "Finished, but 1 share filter could not be restored — see the event log.",
    });
    renderPage();

    await confirmAndUninstall();

    expect(await screen.findByText(/left to retry/i)).toBeInTheDocument();
    expect(screen.queryByText(/Uninstall complete/i)).not.toBeInTheDocument();
    // getAllBy: the label is a <span> inside the <p> that also carries the names, so both match.
    expect(
      screen.getAllByText(/Could not be restored/i).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText(/sarah\. Their share filter still carries/i),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(/No longer on this server/i).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText(/mike\. Their Plex\s+account has left/i),
    ).toBeInTheDocument();
  });

  it("says nothing about unrestorable accounts when there are none", async () => {
    uninstall.mockResolvedValue({
      filters_restored: 48,
      filters_skipped: [],
      filters_unreachable: [],
      filters_failed: [],
      collections_deleted: ["a"],
      rows_disabled: 3,
      dry_run: false,
      message: "Your server is as we found it.",
    });
    renderPage();

    await confirmAndUninstall();

    expect(await screen.findByText(/Uninstall complete/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/Could not be restored/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/No longer on this server/i),
    ).not.toBeInTheDocument();
  });
  it("warns in the PREVIEW when plex.tv could not see some accounts", async () => {
    // Rule 8's rehearsal is what the FAQ tells people to trust. An incomplete roster read is the one
    // signal that should stop an operator typing UNINSTALL, so the preview has to carry it.
    uninstall.mockResolvedValue({
      filters_restored: 0,
      filters_skipped: [],
      filters_unreachable: [
        {
          user: "mike",
          plex_account_id: 839623727,
          reason: "plex.tv did not list this account",
        },
      ],
      filters_failed: [],
      collections_deleted: ["a"],
      rows_disabled: 2,
      dry_run: true,
      message:
        "Preview only — nothing was changed. plex.tv listed none of the 1 account on file.",
    });
    renderPage();

    expect(await screen.findByText(/Preview only/i)).toBeInTheDocument();
    expect(screen.getAllByText(/didn.t list/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/mike\. Your records say/i)).toBeInTheDocument();
    // Nothing has been attempted yet, so the preview must not tell them to retry.
    expect(
      screen.queryByText(/Run the uninstall again/i),
    ).not.toBeInTheDocument();
  });
  it("does not call a REAL uninstall complete when plex.tv could not see an account", async () => {
    // The mirror of the preview case, and the cell that split `unreachable` out of `filters_failed`
    // moved out from under the header's test: a green tick directly above "Run the uninstall again
    // to retry" is exactly the false-completion claim this whole flow exists to avoid.
    uninstall.mockResolvedValue({
      filters_restored: 3,
      filters_skipped: [],
      filters_unreachable: [
        {
          user: "mike",
          plex_account_id: 839623727,
          reason: "plex.tv did not list this account",
        },
      ],
      filters_failed: [],
      collections_deleted: ["a"],
      rows_disabled: 2,
      dry_run: false,
      message:
        "Finished, with some accounts left over: plex.tv did not list 1 account.",
    });
    renderPage();

    await confirmAndUninstall();

    expect(await screen.findByText(/left to retry/i)).toBeInTheDocument();
    expect(screen.queryByText(/Uninstall complete/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Run the uninstall again/i)).toBeInTheDocument();
  });
});
