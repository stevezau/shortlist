import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { HomeWordmark, Logo, Wordmark } from "@/components/brand";

/**
 * The wordmark in the app chrome links to the dashboard — the convention everywhere else on the
 * web, so people try it. The bare `Logo`/`Wordmark` must NOT link on their own: the login and setup
 * screens render them while signed out, where a dashboard link goes nowhere useful.
 */
describe("brand marks", () => {
  it("does not wrap the wordmark in a link on its own", () => {
    const { container } = render(
      <MemoryRouter>
        <Wordmark />
      </MemoryRouter>,
    );
    expect(container.querySelector("a")).toBeNull();
  });

  it("does not wrap the bare logo in a link (login screen renders it signed out)", () => {
    const { container } = render(
      <MemoryRouter>
        <Logo size="lg" />
      </MemoryRouter>,
    );
    expect(container.querySelector("a")).toBeNull();
  });

  it("HomeWordmark points at the dashboard with an accessible name", () => {
    // Asserting href AND accessible name together: the mark is aria-hidden, so without the label
    // the link would only announce by accident of the wordmark's text contents.
    render(
      <MemoryRouter>
        <HomeWordmark />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", {
      name: "Shortlist — go to dashboard",
    });
    expect(link).toHaveAttribute("href", "/");
  });
});
