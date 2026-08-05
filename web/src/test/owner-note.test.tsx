import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { OwnerNote } from "@/components/owner-note";
import { WatchingAccountLink } from "@/components/watching-account-link";

function renderIn(node: React.ReactNode) {
  return render(<MemoryRouter>{node}</MemoryRouter>);
}

describe("OwnerNote", () => {
  it("points at the guide instead of dead-ending on advice", () => {
    // The note always explained the limitation correctly; what it lacked was a next step, which is
    // why the same question kept arriving. The link IS the change.
    renderIn(<OwnerNote />);

    expect(
      screen.getByRole("link", { name: /see the options/i }),
    ).toHaveAttribute("href", "/watching-account");
  });

  it("leads with the notice, not with the reassurance", () => {
    // Ordering is the point of this component. An earlier version opened with "your Home screen
    // shows only your own row" — true, but it buried the one thing an owner needs to know.
    renderIn(<OwnerNote />);

    const heading = screen.getByText(/you.ll see everyone else.s rows/i);
    const reassurance = screen.getByText(/Not your Home screen/i);
    expect(
      heading.compareDocumentPosition(reassurance) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("still says the owner's own Home is safe", () => {
    // Load-bearing and easy to lose in an edit: Plex splits `promotedToOwnHome` from
    // `promotedToSharedHome`, so nobody else's row reaches the owner's Home. The docs stated the
    // opposite for a while — this pins the true claim to a test.
    renderIn(<OwnerNote />);

    expect(screen.getByText(/Not your Home screen/i)).toBeInTheDocument();
  });

  it("names the recommended fix, and does not promise Shortlist creates the Plex account", () => {
    // Shortlist deliberately has no create-a-Home-user endpoint (plex-safety rule 11), so the copy
    // must say the owner adds it in Plex. "We can do this for you" would be a promise the API
    // cannot keep.
    renderIn(<OwnerNote />);

    expect(screen.getByText(/What we suggest:/i)).toBeInTheDocument();
    expect(screen.getByText(/You add the account in Plex/i)).toBeInTheDocument();
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
