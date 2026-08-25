/** `TransferSteps` — the copy-my-history-across step, mounted both on /watching-account and inside
 *  the setup wizard.
 *
 *  The case these cover is the one the wizard hits every time: nothing has ever read the owner's
 *  watch history, so there is nothing to copy. Reported as "Copied 0 titles" that reads as a
 *  working feature that found nothing, which is how the reporter in #88 concluded Shortlist was
 *  broken and wrote their own script. The wizard cannot send them elsewhere to fix it either —
 *  until setup finishes every route redirects back to /setup — so the fix has to be offered here.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { TransferResult, User } from "@/lib/types";
import { TransferSteps } from "@/pages/watching-account";

const {
  getUsers,
  listHomeUsers,
  transferWatchHistory,
  undoWatchTransfer,
  listWatchSnapshots,
  runJob,
} = vi.hoisted(() => ({
  getUsers: vi.fn(),
  listHomeUsers: vi.fn(),
  transferWatchHistory: vi.fn(),
  undoWatchTransfer: vi.fn(),
  listWatchSnapshots: vi.fn(),
  runJob: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      listHomeUsers: () => listHomeUsers(),
      transferWatchHistory: (body: unknown) => transferWatchHistory(body),
      undoWatchTransfer: (body: unknown) => undoWatchTransfer(body),
      listWatchSnapshots: () => listWatchSnapshots(),
      runJob: (kind: string, payload: unknown, background: boolean) =>
        runJob(kind, payload, background),
    },
  };
});

function result(over: Partial<TransferResult>): TransferResult {
  return {
    planned: 0,
    applied: 0,
    unreachable: 0,
    failed: 0,
    marks: 0,
    unmarks: 0,
    offsets_set: 0,
    offsets_cleared: 0,
    removals_preview: [],
    verify_mismatched: 0,
    verify_checked: 0,
    shows_cleared: 0,
    target_unreadable: [],
    events_copied: 0,
    titles_cached: 0,
    snapshot_id: null,
    dry_run: false,
    source_empty: false,
    errors: [],
    ...over,
  };
}

function renderSteps() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TransferSteps />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Pick the one candidate account and press the real (non-preview) button. */
async function transferOnto(name: RegExp) {
  // Preview first, because the UI now REQUIRES it: pressing the real button with no preview used to
  // un-tick a Home user's watch history with nothing shown beforehand.
  await userEvent.click(await screen.findByRole("radio", { name }));
  await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
  await screen.findByText(/nothing has been changed yet|hasn.t read your watch history/i);
  await userEvent.click(
    screen.getByRole("button", { name: /copy my history across/i }),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // TransferSteps scrolls itself into view on mount; jsdom has no scrollIntoView.
  Element.prototype.scrollIntoView = vi.fn();
  getUsers.mockResolvedValue([
    {
      id: 7,
      username: "steve-tv",
      slug: "steve-tv",
      plex_account_id: 300,
      user_type: "managed",
      enabled: false,
      prefs: {},
    } as unknown as User,
  ]);
  listHomeUsers.mockResolvedValue([
    {
      plex_account_id: 300,
      title: "Steve TV",
      protected: false,
      already_a_shortlist_user: false,
    },
  ]);
  runJob.mockResolvedValue({ id: 1, kind: "sync.history", status: "queued" });
  undoWatchTransfer.mockResolvedValue(result({}));
  listWatchSnapshots.mockResolvedValue([]);
});

describe("TransferSteps with nothing to copy", () => {
  it("says the history has not been read yet rather than reporting a copy of nothing", async () => {
    transferWatchHistory.mockResolvedValue(
      result({ planned: 0, source_empty: true }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);

    expect(
      await screen.findByText(/hasn.t read your watch history yet/i),
    ).toBeInTheDocument();
    // The bare count is what made this look like a working feature that found nothing.
    expect(screen.queryByText(/copied/i)).not.toBeInTheDocument();
  });

  it("offers to read the history here, because the wizard cannot reach the Jobs page", async () => {
    transferWatchHistory.mockResolvedValue(
      result({ planned: 0, source_empty: true }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);
    await userEvent.click(
      await screen.findByRole("button", { name: /read my watch history/i }),
    );

    // Background: a full history read takes minutes on a large library, and holding the request
    // open only ever ends in a proxy timeout that reads as a failed job.
    expect(runJob).toHaveBeenCalledWith("sync.history", {}, true);
    expect(await screen.findByText(/try the copy again/i)).toBeInTheDocument();
  });

  it("names a way out, because the job reports success even when it read nothing", async () => {
    // `sync.history` returns a successful detail when a run is already holding the lock, and
    // `sync_watched` catches everything internally — so a Plex that cannot be reached still lands
    // as a finished job. Telling someone only to "try again" puts them back in the #88 loop.
    transferWatchHistory.mockResolvedValue(
      result({ planned: 0, source_empty: true }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);
    await userEvent.click(
      await screen.findByRole("button", { name: /read my watch history/i }),
    );

    expect(
      await screen.findByText(/still comes up empty/i),
    ).toBeInTheDocument();
  });

  it("still reports a real copy normally", async () => {
    transferWatchHistory.mockResolvedValue(
      result({ applied: 12, marks: 12, source_empty: false }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);

    expect(await screen.findByText(/12/)).toBeInTheDocument();
    expect(
      screen.queryByText(/hasn.t read your watch history yet/i),
    ).not.toBeInTheDocument();
  });
});

/** The destructive half. Copying is additive and needs no ceremony; un-ticking someone's watches
 *  does, and the preview is the only place it is visible before it happens. */
describe("TransferSteps when the copy would remove things", () => {
  async function preview(over: Partial<TransferResult>) {
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, ...over }));
    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
  }

  it("names what it would un-tick rather than only counting it", async () => {
    // A count is not something anyone can check against their own account.
    await preview({ unmarks: 2, removals_preview: ["Jaws", "Alien"] });

    expect(await screen.findByText("Jaws")).toBeInTheDocument();
    expect(screen.getByText("Alien")).toBeInTheDocument();
  });

  it("blocks the real run until the removals are acknowledged", async () => {
    await preview({ unmarks: 3, removals_preview: ["Jaws"] });

    expect(
      await screen.findByRole("button", { name: /copy my history across/i }),
    ).toBeDisabled();
  });

  it("unblocks once the box is ticked", async () => {
    await preview({ unmarks: 3, removals_preview: ["Jaws"] });
    await userEvent.click(
      await screen.findByRole("checkbox", { name: /will be un-ticked/i }),
    );

    expect(
      screen.getByRole("button", { name: /copy my history across/i }),
    ).toBeEnabled();
  });

  it("needs no acknowledgement when nothing would be removed", async () => {
    // The common path — a fresh account — stays two clicks.
    await preview({ marks: 40, unmarks: 0 });

    expect(
      await screen.findByRole("button", { name: /copy my history across/i }),
    ).toBeEnabled();
  });

  it("says how many more it would remove than it listed", async () => {
    await preview({ unmarks: 60, removals_preview: ["Jaws"] });

    expect(await screen.findByText(/59 more/)).toBeInTheDocument();
  });
});

describe("TransferSteps after a real copy", () => {
  it("offers an undo that restores from the snapshot", async () => {
    transferWatchHistory.mockResolvedValue(
      result({ applied: 12, marks: 12, snapshot_id: 55 }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);
    await userEvent.click(
      await screen.findByRole("button", { name: /undo this/i }),
    );

    expect(undoWatchTransfer).toHaveBeenCalledWith({
      snapshot_id: 55,
      dry_run: false,
    });
  });

  it("offers no undo when no snapshot was taken", async () => {
    // Nothing was written, so there is nothing to restore — an undo button here would do nothing
    // and imply something had changed.
    transferWatchHistory.mockResolvedValue(
      result({ applied: 0, snapshot_id: null }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);

    expect(await screen.findByText(/switch to that account/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /undo this/i }),
    ).not.toBeInTheDocument();
  });

  it("says so when the check afterwards found writes that did not take", async () => {
    // Plex accepting a write is not the same as the write taking effect, and the old version
    // reported counts it had never checked.
    transferWatchHistory.mockResolvedValue(
      result({ applied: 10, marks: 10, verify_mismatched: 3, snapshot_id: 1 }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);

    expect(
      await screen.findByText(/didn.t take effect when shortlist checked/i),
    ).toBeInTheDocument();
  });

  it("confirms a clean run was verified, not just attempted", async () => {
    transferWatchHistory.mockResolvedValue(
      result({ applied: 10, marks: 10, verify_mismatched: 0, snapshot_id: 1 }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);

    expect(
      await screen.findByText(/now matches yours/i),
    ).toBeInTheDocument();
  });
});

/** The gate on the destructive path. Both halves were live bugs. */
describe("TransferSteps guards the real run", () => {
  it("will not run for real before a preview has been seen", async () => {
    // `blocked` keyed only on `removals > 0`, so NO preview read as "nothing to remove" — pressing
    // the real button first un-ticked a Home user's history with no listing, count or tick-box.
    transferWatchHistory.mockResolvedValue(result({ marks: 40 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));

    expect(
      screen.getByRole("button", { name: /copy my history across/i }),
    ).toBeDisabled();
    expect(screen.getByText(/press preview first/i)).toBeInTheDocument();
  });

  it("re-blocks when the account is changed after previewing", async () => {
    // An acknowledgement given for account A must not authorise a real run against account B.
    listHomeUsers.mockResolvedValue([
      {
        plex_account_id: 300,
        title: "Steve TV",
        protected: false,
        already_a_shortlist_user: false,
      },
      {
        plex_account_id: 301,
        title: "Spare TV",
        protected: false,
        already_a_shortlist_user: false,
      },
    ]);
    getUsers.mockResolvedValue([
      { id: 7, plex_account_id: 300, username: "steve-tv" } as unknown as User,
      { id: 8, plex_account_id: 301, username: "spare-tv" } as unknown as User,
    ]);
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 5 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);
    expect(
      screen.getByRole("button", { name: /copy my history across/i }),
    ).toBeEnabled();

    await userEvent.click(screen.getByRole("radio", { name: /spare tv/i }));

    expect(
      screen.getByRole("button", { name: /copy my history across/i }),
    ).toBeDisabled();
  });
});

/** The undo has to be reachable when the request that created it never came back — the one case the
 *  durable queue exists for — and it is itself destructive, so it gets the same gate the transfer
 *  got. It used to be a single unguarded click. */
describe("TransferSteps offers an undo from the server", () => {
  const PENDING = {
    id: 9,
    user_id: 7,
    username: "steve-tv",
    taken_at: null,
    entries: 412,
    complete: true,
  };

  it("shows a pending undo with no transfer in this session", async () => {
    listWatchSnapshots.mockResolvedValue([PENDING]);

    renderSteps();

    expect(
      await screen.findByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    ).toBeInTheDocument();
  });

  it("says how much it would put back", async () => {
    listWatchSnapshots.mockResolvedValue([PENDING]);

    renderSteps();

    expect(
      await screen.findByText(/would put back 412 titles/i),
    ).toBeInTheDocument();
  });

  it("previews before restoring, rather than restoring on one click", async () => {
    // Restoring is a mirror too: it un-ticks everything watched on that account since the copy.
    listWatchSnapshots.mockResolvedValue([PENDING]);
    undoWatchTransfer.mockResolvedValue(
      result({ dry_run: true, unmarks: 3, removals_preview: ["Jaws"] }),
    );

    renderSteps();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    );

    expect(undoWatchTransfer).toHaveBeenCalledWith({
      snapshot_id: 9,
      dry_run: true,
    });
    expect(await screen.findByText("Jaws")).toBeInTheDocument();
    expect(screen.getByText(/un-ticks/i)).toBeInTheDocument();
  });

  it("restores only after the preview has been seen", async () => {
    listWatchSnapshots.mockResolvedValue([PENDING]);
    undoWatchTransfer.mockResolvedValue(
      result({ dry_run: true, unmarks: 3, removals_preview: ["Jaws"] }),
    );

    renderSteps();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /^restore it$/i }),
    );

    expect(undoWatchTransfer).toHaveBeenLastCalledWith({
      snapshot_id: 9,
      dry_run: false,
    });
  });

  it("refuses an incomplete snapshot and says why", async () => {
    // Restoring from a partial snapshot would un-mark every watch it never recorded.
    listWatchSnapshots.mockResolvedValue([{ ...PENDING, complete: false }]);

    renderSteps();

    expect(
      await screen.findByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    ).toBeDisabled();
    expect(screen.getByText(/wasn.t readable/i)).toBeInTheDocument();
  });
});

/** State that got stuck. All three were real: the list vanishing, a stale "already undone", and an
 *  error attributed to the wrong row. */
describe("TransferSteps keeps the undo reachable and correctly attributed", () => {
  const PENDING = {
    id: 9,
    user_id: 7,
    username: "steve-tv",
    taken_at: null,
    entries: 412,
    complete: true,
  };

  it("still shows pending undos after a PREVIEW", async () => {
    // A dry run sets `isSuccess` too, so gating the list on it removed the only route back to a
    // completed destructive run the moment Preview was pressed.
    listWatchSnapshots.mockResolvedValue([PENDING]);
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 5 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);

    expect(
      screen.getByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    ).toBeInTheDocument();
  });

  it("does not mark a new copy's undo as already done", async () => {
    // `undo.isSuccess` is mutation-wide: restoring an OLDER snapshot marked the next copy's Undo as
    // done — disabled, and claiming a Plex write that never happened.
    //
    // The first version of this test asserted only that "Copy my history across" was enabled, which
    // is a property of the preview gate, not of `undoneThisOne` — reverting the fix left it green.
    // It asserts the Undo button and its caption now.
    listWatchSnapshots.mockResolvedValue([PENDING]);
    undoWatchTransfer.mockResolvedValue(result({ dry_run: true, unmarks: 1 }));

    renderSteps();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    );
    undoWatchTransfer.mockResolvedValue(result({}));
    await userEvent.click(await screen.findByRole("button", { name: /^restore it$/i }));

    // Now a REAL copy, whose snapshot (77) is a different one.
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 3 }));
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);
    transferWatchHistory.mockResolvedValue(
      result({ applied: 3, marks: 3, snapshot_id: 77 }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /copy my history across/i }),
    );

    const undoButton = await screen.findByRole("button", { name: /undo this/i });
    expect(undoButton).toBeEnabled();
    expect(
      screen.getByText(/puts that account back as it was before/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/put back exactly as it was/i),
    ).not.toBeInTheDocument();
  });

  it("does not offer an undo when the saved state is incomplete", async () => {
    // `undo_transfer` refuses an incomplete snapshot and answers 200 with the reason in `errors`, so
    // offering the button meant the refusal fired onSuccess and the panel claimed success.
    transferWatchHistory.mockResolvedValue(
      result({ applied: 3, marks: 3, snapshot_id: 5, target_unreadable: ["12"] }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);

    expect(await screen.findByText(/can.t be undone/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /undo this/i }),
    ).not.toBeInTheDocument();
  });

  it("treats a 200 carrying errors as a refusal and SAYS WHY", async () => {
    // Asserting only the absence of "put back exactly as it was" was satisfied by showing nothing —
    // which is exactly the bug that followed. The reason has to reach the screen: it comes back as a
    // 200, so React Query has no error object and the generic fallback ("please try again") invited
    // a retry of something that can never succeed.
    transferWatchHistory.mockResolvedValue(
      result({ applied: 3, marks: 3, snapshot_id: 5 }),
    );
    undoWatchTransfer.mockResolvedValue(
      result({
        errors: ["that account is no longer one of your own Plex Home users"],
      }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);
    await userEvent.click(await screen.findByRole("button", { name: /undo this/i }));

    expect(
      await screen.findByText(/no longer one of your own plex home users/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/put back exactly as it was/i),
    ).not.toBeInTheDocument();
  });

  it("does not claim a restore when safe mode forced a dry run", async () => {
    // Safe mode forces `dry_run` on server-side even when the real button was pressed, so the report
    // comes back clean-but-unwritten. The transfer half of this page already guarded on it.
    transferWatchHistory.mockResolvedValue(
      result({ applied: 3, marks: 3, snapshot_id: 5 }),
    );
    undoWatchTransfer.mockResolvedValue(result({ dry_run: true, applied: 1 }));

    renderSteps();
    await transferOnto(/steve tv/i);
    await userEvent.click(await screen.findByRole("button", { name: /undo this/i }));

    expect(await screen.findByText(/safe mode is on/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/put back exactly as it was/i),
    ).not.toBeInTheDocument();
  });

  it("attributes an undo failure to the snapshot it was for", async () => {
    listWatchSnapshots.mockResolvedValue([
      PENDING,
      { ...PENDING, id: 10, username: "spare-tv" },
    ]);
    undoWatchTransfer.mockRejectedValue(new Error("Plex is busy"));

    renderSteps();
    await userEvent.click(
      await screen.findByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    );

    // One row failed, so exactly one message — not one under every snapshot.
    expect(await screen.findAllByText(/couldn.t undo it/i)).toHaveLength(1);
  });
});

/** The consent surface. It authorises deleting a Home user's watch history, so what it promises has
 *  to be true — the transfer itself was going to break this one. */
describe("TransferSteps is honest about whether the copy can be undone", () => {
  it("warns on the tick-box when the copy will NOT be undoable", async () => {
    // A target that can't see one of the libraries gets an incomplete snapshot, and `undo_transfer`
    // refuses to restore from one. The tick-box said "so this can be undone" regardless.
    transferWatchHistory.mockResolvedValue(
      result({
        dry_run: true,
        marks: 10,
        unmarks: 412,
        removals_preview: ["Jaws"],
        target_unreadable: ["12"],
      }),
    );

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    expect(await screen.findByText(/will NOT be undoable/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/so this can be undone/i),
    ).not.toBeInTheDocument();
  });

  it("still promises the undo when every library is readable", async () => {
    transferWatchHistory.mockResolvedValue(
      result({
        dry_run: true,
        marks: 10,
        unmarks: 412,
        removals_preview: ["Jaws"],
        target_unreadable: [],
      }),
    );

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    expect(await screen.findByText(/so this can be undone/i)).toBeInTheDocument();
    expect(screen.queryByText(/will NOT be undoable/i)).not.toBeInTheDocument();
  });

  it("says which libraries that account cannot see", async () => {
    transferWatchHistory.mockResolvedValue(
      result({ dry_run: true, marks: 10, target_unreadable: ["12", "2"] }),
    );

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    expect(
      await screen.findByText(/can.t see 2 of your libraries/i),
    ).toBeInTheDocument();
  });
});

/** Present-tense claims that stopped being true when the state around them changed. Each of these
 *  put two contradictory statements about one Plex account on screen at once. */
describe("TransferSteps does not contradict itself", () => {
  it("stops claiming the account matches yours once the undo has landed", async () => {
    // The verify line describes the account's CURRENT state, and an undo reverses exactly that.
    transferWatchHistory.mockResolvedValue(
      result({ applied: 3, marks: 3, snapshot_id: 5, verify_mismatched: 0 }),
    );
    undoWatchTransfer.mockResolvedValue(result({ applied: 3 }));

    renderSteps();
    await transferOnto(/steve tv/i);
    expect(screen.getByText(/now matches yours/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /undo this/i }));

    expect(await screen.findByText(/back to how it was/i)).toBeInTheDocument();
    expect(screen.queryByText(/now matches yours/i)).not.toBeInTheDocument();
  });

  it("does not tell you to re-run a copy you have just undone", async () => {
    // The mismatch branch said "Run it again — it only writes what's still missing", which after an
    // undo is an instruction to redo the copy the owner just reversed.
    transferWatchHistory.mockResolvedValue(
      result({ applied: 3, marks: 3, snapshot_id: 5, verify_mismatched: 2 }),
    );
    undoWatchTransfer.mockResolvedValue(result({ applied: 3 }));

    renderSteps();
    await transferOnto(/steve tv/i);
    await userEvent.click(screen.getByRole("button", { name: /undo this/i }));

    expect(screen.queryByText(/run it again/i)).not.toBeInTheDocument();
  });

  it("clears a previous undo failure when the retry succeeds", async () => {
    transferWatchHistory.mockResolvedValue(
      result({ applied: 3, marks: 3, snapshot_id: 5 }),
    );
    undoWatchTransfer.mockRejectedValueOnce(new Error("Plex is busy"));

    renderSteps();
    await transferOnto(/steve tv/i);
    await userEvent.click(screen.getByRole("button", { name: /undo this/i }));
    await screen.findByText(/couldn.t undo it/i);

    undoWatchTransfer.mockResolvedValue(result({ applied: 3 }));
    await userEvent.click(screen.getByRole("button", { name: /undo this/i }));

    expect(await screen.findByText(/put back exactly as it was/i)).toBeInTheDocument();
    expect(screen.queryByText(/couldn.t undo it/i)).not.toBeInTheDocument();
  });

  it("says so when safe mode turned the real copy into a dry run", async () => {
    // The server forces `dry_run` on. The page used to reset itself and say nothing at all.
    transferWatchHistory.mockResolvedValueOnce(
      result({ dry_run: true, marks: 5 }),
    );

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);

    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 5 }));
    await userEvent.click(
      screen.getByRole("button", { name: /copy my history across/i }),
    );

    expect(await screen.findByText(/safe mode is on/i)).toBeInTheDocument();
    // The banner is only half the fix. Keying the reset on the LOCAL `dryRun` also wiped the
    // preview and re-disabled the button, so the page told you safe mode was on AND threw away the
    // preview you would need to try again once it was off. Asserting the sentence alone passed with
    // that half reverted.
    expect(
      screen.getByText(/nothing has been changed yet/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /copy my history across/i }),
    ).toBeEnabled();
  });
});

/** The run the page itself tells you to make. */
describe("TransferSteps keeps the undo reachable after a converged re-run", () => {
  it("still lists an earlier copy when the re-run wrote nothing", async () => {
    // "Run it again — it only writes what's still missing" produces `planned: 0`, which takes no
    // snapshot and returns `snapshot_id: null` — so the success panel renders no Undo. Hiding the
    // list on top of that left no route back to the earlier copy at all.
    listWatchSnapshots.mockResolvedValue([
      {
        id: 9,
        user_id: 7,
        username: "steve-tv",
        taken_at: null,
        entries: 412,
        complete: true,
      },
    ]);
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 0 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);

    transferWatchHistory.mockResolvedValue(
      result({ planned: 0, applied: 0, snapshot_id: null }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /copy my history across/i }),
    );

    expect(
      await screen.findByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    ).toBeInTheDocument();
  });

  it("stops claiming a match once that earlier copy is restored", async () => {
    // `undoneThisOne` was keyed on THIS transfer's snapshot id — null for a converged re-run, which
    // is precisely the case the fix above makes the list reachable in. So restoring from the list
    // left the panel still asserting "that account now matches yours".
    listWatchSnapshots.mockResolvedValue([
      {
        id: 9,
        user_id: 7,
        username: "steve-tv",
        taken_at: null,
        entries: 412,
        complete: true,
      },
    ]);
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 0 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);
    transferWatchHistory.mockResolvedValue(
      result({ planned: 0, applied: 0, snapshot_id: null, verify_mismatched: 0 }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /copy my history across/i }),
    );

    undoWatchTransfer.mockResolvedValue(result({ dry_run: true, unmarks: 1 }));
    await userEvent.click(
      await screen.findByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    );
    undoWatchTransfer.mockResolvedValue(result({ applied: 5 }));
    await userEvent.click(await screen.findByRole("button", { name: /^restore it$/i }));

    expect(await screen.findByText(/back to how it was/i)).toBeInTheDocument();
    expect(screen.queryByText(/now matches yours/i)).not.toBeInTheDocument();
  });
});

/** The snapshot list offers EVERY account's snapshots, so "was this panel's copy reversed?" cannot
 *  be answered by "did any restore happen?" */
describe("TransferSteps attributes a restore to the right account", () => {
  const MINE = {
    id: 9,
    user_id: 7,
    username: "steve-tv",
    taken_at: null,
    entries: 412,
    complete: true,
  };
  const SOMEONE_ELSES = {
    id: 10,
    user_id: 99,
    username: "kids-tv",
    taken_at: null,
    entries: 8,
    complete: true,
  };

  it("does not claim this account was restored when a different one's snapshot was", async () => {
    // A converged re-run takes no snapshot, so there is no id to match on — and accepting any
    // restore in that case meant restoring kids-tv flipped steve-tv's panel to "back to how it was".
    listWatchSnapshots.mockResolvedValue([MINE, SOMEONE_ELSES]);
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 0 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);
    transferWatchHistory.mockResolvedValue(
      result({ planned: 0, applied: 0, snapshot_id: null, verify_mismatched: 0 }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /copy my history across/i }),
    );
    expect(await screen.findByText(/now matches yours/i)).toBeInTheDocument();

    undoWatchTransfer.mockResolvedValue(result({ dry_run: true, unmarks: 1 }));
    await userEvent.click(
      screen.getByRole("button", {
        name: /preview undoing the copy onto kids-tv/i,
      }),
    );
    undoWatchTransfer.mockResolvedValue(result({ applied: 8 }));
    await userEvent.click(await screen.findByRole("button", { name: /^restore it$/i }));

    // kids-tv was restored; this panel is about steve-tv and must not claim otherwise.
    expect(await screen.findByText(/now matches yours/i)).toBeInTheDocument();
    expect(screen.queryByText(/back to how it was/i)).not.toBeInTheDocument();
  });

  it("does still claim it when THIS account's snapshot is the one restored", async () => {
    listWatchSnapshots.mockResolvedValue([MINE, SOMEONE_ELSES]);
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 0 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);
    transferWatchHistory.mockResolvedValue(
      result({ planned: 0, applied: 0, snapshot_id: null, verify_mismatched: 0 }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /copy my history across/i }),
    );

    undoWatchTransfer.mockResolvedValue(result({ dry_run: true, unmarks: 1 }));
    await userEvent.click(
      screen.getByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    );
    undoWatchTransfer.mockResolvedValue(result({ applied: 412 }));
    await userEvent.click(await screen.findByRole("button", { name: /^restore it$/i }));

    expect(await screen.findByText(/back to how it was/i)).toBeInTheDocument();
  });
});

/** The radio moves; the success panel does not. Anyone setting up a kids account next hits this. */
describe("TransferSteps does not re-claim a restored copy when the selection changes", () => {
  it("keeps saying restored after another Home account is selected", async () => {
    // `target` is the LIVE radio selection, not the account the copy ran against — so switching
    // users flipped the panel back to "that account now matches yours" about an account that had
    // just been reversed.
    listHomeUsers.mockResolvedValue([
      {
        plex_account_id: 300,
        title: "Steve TV",
        protected: false,
        already_a_shortlist_user: false,
      },
      {
        plex_account_id: 301,
        title: "Kids TV",
        protected: false,
        already_a_shortlist_user: false,
      },
    ]);
    getUsers.mockResolvedValue([
      { id: 7, plex_account_id: 300, username: "steve-tv" } as unknown as User,
      { id: 8, plex_account_id: 301, username: "kids-tv" } as unknown as User,
    ]);
    listWatchSnapshots.mockResolvedValue([
      {
        id: 9,
        user_id: 7,
        username: "steve-tv",
        taken_at: null,
        entries: 412,
        complete: true,
      },
    ]);
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 0 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);
    transferWatchHistory.mockResolvedValue(
      result({ planned: 0, applied: 0, snapshot_id: null, verify_mismatched: 0 }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /copy my history across/i }),
    );

    undoWatchTransfer.mockResolvedValue(result({ dry_run: true, unmarks: 1 }));
    await userEvent.click(
      await screen.findByRole("button", {
        name: /preview undoing the copy onto steve-tv/i,
      }),
    );
    undoWatchTransfer.mockResolvedValue(result({ applied: 412 }));
    await userEvent.click(await screen.findByRole("button", { name: /^restore it$/i }));
    expect(await screen.findByText(/back to how it was/i)).toBeInTheDocument();

    // Selecting a DIFFERENT Home user must not change what is true about the copy already made.
    await userEvent.click(screen.getByRole("radio", { name: /kids tv/i }));

    expect(screen.getByText(/back to how it was/i)).toBeInTheDocument();
    expect(screen.queryByText(/now matches yours/i)).not.toBeInTheDocument();
  });
});

/** The source picker. The capability existed over the API from the start; the page did not offer it,
 *  which made the maintainer's own case — his watching lives on a shared account, not the admin one
 *  — unreachable from the UI. */
describe("TransferSteps can copy from an account other than the owner", () => {
  beforeEach(() => {
    getUsers.mockResolvedValue([
      {
        id: 7,
        plex_account_id: 300,
        username: "steve-tv",
        user_type: "managed",
        enabled: false,
      } as unknown as User,
      {
        id: 29,
        plex_account_id: 218833834,
        username: "moohouse",
        display_name: "MooHouse",
        user_type: "shared",
        enabled: true,
      } as unknown as User,
    ]);
  });

  it("defaults to the owner and sends no source", async () => {
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 5 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    // No `from_user_id` at all — the server defaults to the owner, and sending an explicit id would
    // make the UI a second place that has to know who the owner is.
    expect(transferWatchHistory).toHaveBeenCalledWith({
      to_user_id: 7,
      dry_run: true,
    });
  });

  it("sends the chosen account when one is picked", async () => {
    transferWatchHistory.mockResolvedValue(result({ dry_run: true, marks: 5 }));

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /copy the history from/i }),
      "29",
    );
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    expect(transferWatchHistory).toHaveBeenCalledWith({
      to_user_id: 7,
      from_user_id: 29,
      dry_run: true,
    });
  });

  it("invalidates the preview when the source changes", async () => {
    // A preview describes ONE pair of accounts. Carrying it across a change of source would let an
    // acknowledgement given for one pair authorise a real run against another.
    transferWatchHistory.mockResolvedValue(
      result({ dry_run: true, marks: 5, unmarks: 2, removals_preview: ["Jaws"] }),
    );

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));
    await screen.findByText(/nothing has been changed yet/i);

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /copy the history from/i }),
      "29",
    );

    expect(
      screen.queryByText(/nothing has been changed yet/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /copy my history across/i }),
    ).toBeDisabled();
  });

  it("hides the picker when the owner is the only possible source", async () => {
    // A one-option control is noise on a fresh server.
    getUsers.mockResolvedValue([
      {
        id: 7,
        plex_account_id: 300,
        username: "steve-tv",
        user_type: "managed",
        enabled: false,
      } as unknown as User,
    ]);

    renderSteps();
    await screen.findByRole("radio", { name: /steve tv/i });

    expect(
      screen.queryByRole("combobox", { name: /copy the history from/i }),
    ).not.toBeInTheDocument();
  });
});

/** The repair case. An account the pre-1.x transfer over-marked has no way to know it is affected. */
describe("TransferSteps explains a large removal count", () => {
  it("says a big un-tick count is likely repairing old damage", async () => {
    // Mirroring fixes those accounts, but only if the owner runs it — and a wall of removals with no
    // explanation reads as damage rather than as the repair it is.
    transferWatchHistory.mockResolvedValue(
      result({
        dry_run: true,
        marks: 400,
        unmarks: 900,
        removals_preview: ["One Piece Ep 401"],
      }),
    );

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    // `findAllBy`: the phrase spans a text node that its container also matches.
    expect(
      (await screen.findAllByText(/count this large almost always means/i)).length,
    ).toBeGreaterThan(0);
  });

  it("does not say it for an ordinary handful of removals", async () => {
    transferWatchHistory.mockResolvedValue(
      result({ dry_run: true, marks: 400, unmarks: 3, removals_preview: ["Jaws"] }),
    );

    renderSteps();
    await userEvent.click(await screen.findByRole("radio", { name: /steve tv/i }));
    await userEvent.click(screen.getByRole("button", { name: /^preview$/i }));

    await screen.findByText(/nothing has been changed yet/i);
    expect(
      screen.queryAllByText(/count this large almost always means/i),
    ).toHaveLength(0);
  });
});
