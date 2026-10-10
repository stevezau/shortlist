import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { NotFoundPage } from "@/pages/not-found";

describe("NotFoundPage", () => {
  it("shows the address that was tried and puts the way out next to the message", () => {
    render(
      <MemoryRouter initialEntries={["/rows/nope?tab=x"]}>
        <NotFoundPage />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByText("/rows/nope?tab=x")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Dashboard" })).toHaveAttribute("href", "/");
  });
});
