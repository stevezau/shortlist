import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { NotFoundPage } from "@/pages/not-found";

describe("NotFoundPage", () => {
  it("offers a way out, instead of pointing at navigation that may be off-screen", () => {
    // "Use the navigation on the left" is wrong on a phone: `AppShell` renders the nav as a
    // slide-in drawer behind a hamburger, so the one instruction on the page names something the
    // reader cannot see at the moment they are told to use it.
    render(
      <MemoryRouter>
        <NotFoundPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: /Dashboard/i })).toHaveAttribute(
      "href",
      "/",
    );
    expect(screen.queryByText(/navigation on the left/i)).toBeNull();
  });
});
