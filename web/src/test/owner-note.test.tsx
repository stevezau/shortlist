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
      screen.getByRole("link", { name: /see your options/i }),
    ).toHaveAttribute("href", "/watching-account");
  });

  it("still says the owner's own Home is safe", () => {
    // Load-bearing and easy to lose in an edit: Plex splits `promotedToOwnHome` from
    // `promotedToSharedHome`, so nobody else's row reaches the owner's Home. The docs stated the
    // opposite for a while — this pins the true claim to a test.
    renderIn(<OwnerNote />);

    expect(screen.getByText(/your Home screen is safe/i)).toBeInTheDocument();
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
