import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SharingUntouchedBadge } from "@/components/user-badges";
import type { User } from "@/lib/types";

function user(overrides: Partial<User> = {}): User {
  return { id: 7, username: "kid", slug: "kid", ...overrides } as User;
}

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
