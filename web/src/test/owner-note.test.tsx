import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  OWNER_SHELF_ALERT_ID,
  OWNER_SHELF_NOTE_ID,
  OwnerNote,
} from "@/components/owner-note";
import { WatchingAccountLink } from "@/components/watching-account-link";
import type * as ApiModule from "@/lib/api";
import type { NotificationsPage } from "@/lib/types";

const { getNotifications, dismissNotification } = vi.hoisted(() => ({
  getNotifications: vi.fn(() =>
    Promise.resolve({ notifications: [], dismissed: [] } as NotificationsPage),
  ),
  dismissNotification: vi.fn((_id: string) => Promise.resolve({ ok: true })),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getNotifications: () => getNotifications(),
      dismissNotification: (id: string) => dismissNotification(id),
    },
  };
});

function renderIn(node: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getNotifications.mockResolvedValue({
    notifications: [],
    dismissed: [],
  } as NotificationsPage);
});

describe("OwnerNote", () => {
  it("points at the guide instead of dead-ending on advice", async () => {
    // The note always explained the limitation correctly; what it lacked was a next step, which is
    // why the same question kept arriving. The link IS the change.
    renderIn(<OwnerNote />);

    expect(
      await screen.findByRole("link", { name: /see the options/i }),
    ).toHaveAttribute("href", "/watching-account");
  });

  it("leads with the notice, not with the reassurance", async () => {
    // Ordering is the point of this component. An earlier version opened with "your Home screen
    // shows only your own row" — true, but it buried the one thing an owner needs to know.
    renderIn(<OwnerNote />);

    const heading = await screen.findByText(/you.ll see everyone else.s rows/i);
    const reassurance = screen.getByText(/Not your Home screen/i);
    expect(
      heading.compareDocumentPosition(reassurance) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("still says the owner's own Home is safe", async () => {
    // Load-bearing and easy to lose in an edit: Plex splits `promotedToOwnHome` from
    // `promotedToSharedHome`, so nobody else's row reaches the owner's Home. The docs stated the
    // opposite for a while — this pins the true claim to a test.
    renderIn(<OwnerNote />);

    expect(
      await screen.findByText(/Not your Home screen/i),
    ).toBeInTheDocument();
  });

  it("names the recommended fix, and does not promise Shortlist creates the Plex account", async () => {
    // Shortlist deliberately has no create-a-Home-user endpoint (plex-safety rule 11), so the copy
    // must say the owner adds it in Plex. "We can do this for you" would be a promise the API
    // cannot keep.
    renderIn(<OwnerNote />);

    expect(await screen.findByText(/What we suggest:/i)).toBeInTheDocument();
    expect(
      screen.getByText(/You add the account in Plex/i),
    ).toBeInTheDocument();
  });

  it("retires the bell alert as well, so the same message stops arriving twice", async () => {
    // "Got it — don't show this again", clicked while reading the full explanation, is a considered
    // gesture: leaving the identical message sitting in the bell afterwards is nagging someone who
    // has already said they understand. The asymmetry with the test below is the whole design — the
    // strong gesture clears both, the light one clears only itself.
    renderIn(<OwnerNote />);

    await userEvent.click(
      await screen.findByRole("button", { name: /don.t show this again/i }),
    );

    expect(dismissNotification).toHaveBeenCalledWith(OWNER_SHELF_NOTE_ID);
    await waitFor(() =>
      expect(dismissNotification).toHaveBeenCalledWith(OWNER_SHELF_ALERT_ID),
    );
  });

  it("survives the bell alert being dismissed — the coupling runs ONE way", async () => {
    // The bug the split ids fix, and the reason the test above is not simply symmetric. Clearing a
    // bell alert is "yep, seen it"; retiring an inline explainer is a deliberate choice. Coupling
    // them BOTH ways meant one casual bell click permanently deleted the explanation from the Users
    // page, with no way in the UI to bring it back — within an hour of shipping, on the maintainer's
    // own server.
    getNotifications.mockResolvedValue({
      notifications: [],
      dismissed: [OWNER_SHELF_ALERT_ID],
    } as NotificationsPage);
    renderIn(<OwnerNote />);

    expect(
      await screen.findByText(/you.ll see everyone else.s rows/i),
    ).toBeInTheDocument();
  });

  it("stays hidden once the server confirms its own id was dismissed", async () => {
    getNotifications.mockResolvedValue({
      notifications: [],
      dismissed: [OWNER_SHELF_NOTE_ID],
    } as NotificationsPage);
    renderIn(<OwnerNote />);

    await waitFor(() =>
      expect(
        screen.queryByText(/you.ll see everyone else.s rows/i),
      ).not.toBeInTheDocument(),
    );
  });

  it("stays visible when the dismissal fails, rather than pretending it saved", async () => {
    // Hiding on click would be quicker but would also hide a FAILED write, leaving the owner
    // believing they silenced something that will be back on the next reload.
    dismissNotification.mockRejectedValue(new Error("boom"));
    renderIn(<OwnerNote />);

    await userEvent.click(
      await screen.findByRole("button", { name: /don.t show this again/i }),
    );

    expect(await screen.findByText(/Couldn.t save that/i)).toBeInTheDocument();
    expect(
      screen.getByText(/you.ll see everyone else.s rows/i),
    ).toBeInTheDocument();
  });
});

describe("WatchingAccountLink", () => {
  it("is one component so every warning offers the same escape hatch", () => {
    renderIn(<WatchingAccountLink />);

    expect(
      screen.getByRole("link", { name: /see your options/i }),
    ).toHaveAttribute("href", "/watching-account");
  });
});
