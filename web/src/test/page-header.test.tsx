/**
 * One page header everywhere: a title, one line under it, and the actions on the right.
 *
 * Every header used to open with the same 40px icon tile, which repeated the nav icon beside it and
 * pushed the title off the left edge the rest of the page aligns to. Two pages hid the tile with a
 * `[&>div>span]:hidden` selector; it is gone for all of them now.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageHeader } from "@/components/page-header";

describe("PageHeader", () => {
  it("renders the title as the page's h1, with the subtitle and actions beside it", () => {
    render(
      <PageHeader
        title="Rows"
        subtitle="The strips Shortlist builds on your users’ Plex home screens."
        actions={<button type="button">Add a row</button>}
      />,
    );

    expect(screen.getByRole("heading", { level: 1, name: "Rows" })).toBeInTheDocument();
    expect(screen.getByText(/strips Shortlist builds/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a row" })).toBeInTheDocument();
  });

  it("draws no icon tile", () => {
    const { container } = render(<PageHeader title="Users" subtitle="Who’s getting recommendations." />);

    expect(container.querySelector("[aria-hidden]")).toBeNull();
    expect(container.querySelector("svg")).toBeNull();
  });

  it("accepts a rich title, so a row name can carry its placeholder chips", () => {
    render(
      <PageHeader
        title={
          <>
            Rename <span data-testid="chip">library name</span>
          </>
        }
      />,
    );

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Rename library name");
    expect(screen.getByTestId("chip")).toBeInTheDocument();
  });

  it("leaves out the actions slot when there are none", () => {
    const { container } = render(<PageHeader title="Settings" />);

    expect(container.querySelector("header")?.children).toHaveLength(1);
  });
});
