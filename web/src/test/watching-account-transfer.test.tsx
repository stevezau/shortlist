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

const { getUsers, listHomeUsers, transferWatchHistory, runJob } = vi.hoisted(
  () => ({
    getUsers: vi.fn(),
    listHomeUsers: vi.fn(),
    transferWatchHistory: vi.fn(),
    runJob: vi.fn(),
  }),
);

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      listHomeUsers: () => listHomeUsers(),
      transferWatchHistory: (body: unknown) => transferWatchHistory(body),
      runJob: (kind: string, payload: unknown, background: boolean) =>
        runJob(kind, payload, background),
    },
  };
});

function result(over: Partial<TransferResult>): TransferResult {
  return {
    copied: 0,
    already_present: 0,
    scrobbled: 0,
    scrobble_skipped: 0,
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
  await userEvent.click(await screen.findByRole("radio", { name }));
  await userEvent.click(
    screen.getByRole("button", { name: /move my history across/i }),
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
});

describe("TransferSteps with nothing to copy", () => {
  it("says the history has not been read yet rather than reporting a copy of nothing", async () => {
    transferWatchHistory.mockResolvedValue(
      result({ copied: 0, source_empty: true }),
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
      result({ copied: 0, source_empty: true }),
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
      result({ copied: 0, source_empty: true }),
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
      result({ copied: 12, source_empty: false }),
    );

    renderSteps();
    await transferOnto(/steve tv/i);

    expect(await screen.findByText(/12/)).toBeInTheDocument();
    expect(
      screen.queryByText(/hasn.t read your watch history yet/i),
    ).not.toBeInTheDocument();
  });
});
