import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UnhiddenRowsBadge } from "@/components/user-badges";
import type { User } from "@/lib/types";

function user(overrides: Partial<User> = {}): User {
  return { username: "kid", slug: "kid", ...overrides } as User;
}

describe("UnhiddenRowsBadge", () => {
  it("does not appear when nothing is exposed", () => {
    const { container } = render(
      <UnhiddenRowsBadge user={user({ unhidden_rows: 0 })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("counts a single row in the singular", () => {
    render(<UnhiddenRowsBadge user={user({ unhidden_rows: 1 })} />);
    expect(screen.getByText("Sees 1 row of others’")).toBeInTheDocument();
  });

  it("never suggests turning the person off, which does not fix it", () => {
    // The same rule the alert is held to (`test_never_suggests_disabling_the_account_which_does_not
    // _help`): disabling removes THEIR row, not their view of everyone else's. The badge is the
    // surface an owner scans first, and it was the one place still recommending it.
    render(<UnhiddenRowsBadge user={user({ unhidden_rows: 3 })} />);

    const tip = screen.getByText(/Sees 3 rows/).getAttribute("title") ?? "";

    expect(tip).toMatch(/Restriction Profile to None/);
    expect(tip).toMatch(/does not fix it/i);
    expect(tip).not.toMatch(/or turn this person off/i);
  });
});
