import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import {
  SharingUntouchedBadge,
  UnhiddenRowsBadge,
} from "@/components/user-badges";
import type { User } from "@/lib/types";

// The badge counts ROWS from the live privacy reading: `missing` holds one entry per row.
const { exposure } = vi.hoisted(() => ({ exposure: { current: 0 } }));
vi.mock("@/lib/api", () => ({
  api: {
    getPrivacyStatus: () =>
      Promise.resolve({
        accounts: [
          {
            user_id: 7,
            state: "missing",
            missing: Array.from({ length: exposure.current }, (_, i) => `shortlist_${i}`),
          },
          {
            user_id: 42,
            state: "missing",
            missing: Array.from({ length: exposure.current }, (_, i) => `shortlist_${i}`),
          },
        ],
        enforcement: {},
      }),
  },
}));

/** `unhidden_rows` is the run's collection count; it is ignored, and the tests set the rows. */
function user(overrides: Partial<User> & { rows?: number } = {}): User {
  const { rows = 0, ...rest } = overrides;
  exposure.current = rows;
  return { id: 7, username: "kid", slug: "kid", unhidden_rows: 99, ...rest } as User;
}

/** The badge links now, so every render needs a router around it. */
function renderBadge(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UnhiddenRowsBadge", () => {
  it("does not appear when nothing is exposed", async () => {
    const { container } = renderBadge(
      <UnhiddenRowsBadge user={user({ rows: 0 })} />,
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container).toBeEmptyDOMElement();
  });

  it("counts a single row in the singular", async () => {
    renderBadge(<UnhiddenRowsBadge user={user({ rows: 1 })} />);
    expect(await screen.findByText("Sees 1 row of others’")).toBeInTheDocument();
  });

  it("never suggests turning the person off, which does not fix it", async () => {
    // The same rule the alert is held to (`test_never_suggests_disabling_the_account_which_does_not
    // _help`): disabling removes THEIR row, not their view of everyone else's. The badge is the
    // surface an owner scans first, and it was the one place still recommending it.
    renderBadge(<UnhiddenRowsBadge user={user({ rows: 3 })} />);

    const tip = (await screen.findByText(/Sees 3 rows/)).getAttribute("title") ?? "";

    expect(tip).toMatch(/Restriction Profile to None/);
    expect(tip).toMatch(/does not fix it/i);
    expect(tip).not.toMatch(/or turn this person off/i);
  });

  it("leads to the person's page, where the fix is written out in full", async () => {
    // Audit finding, Sep 2026: this is the most alarming string in the app and its entire remedy
    // was a `title` — hover-only on a desktop, invisible on a phone. The badge is only ever shown
    // for an account with a restriction profile, which is precisely the account whose own page
    // renders the Off banner with the same remedy as readable text.
    renderBadge(
      <UnhiddenRowsBadge
        user={user({ id: 42, display_name: "Kid", rows: 3 })}
      />,
    );

    const link = await screen.findByRole("link", { name: /how to fix it/i });
    expect(link).toHaveAttribute("href", "/users/42");
    // The count has to survive into the accessible name, or the link reads as generic navigation.
    expect(link.getAttribute("aria-label")).toMatch(/Kid can see 3 rows/);
  });
});

describe("SharingUntouchedBadge", () => {
  it("stays out of the way while Shortlist is managing them", () => {
    const { container } = render(
      <SharingUntouchedBadge user={user({ manage_sharing: true })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("names the state when the owner has left the account alone", () => {
    // This is state that changes who sees what, and it is invisible everywhere else in the list —
    // exactly the kind that gets forgotten and reported as a leak months later.
    render(<SharingUntouchedBadge user={user({ manage_sharing: false })} />);
    expect(screen.getByText("Sharing untouched")).toBeInTheDocument();
  });
});
