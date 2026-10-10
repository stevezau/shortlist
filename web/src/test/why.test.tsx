import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Why } from "@/components/why";

describe("Why", () => {
  it("hides the explanation until asked", () => {
    render(<Why text="Because it was rated low." />);
    expect(screen.queryByText("Because it was rated low.")).toBeNull();
    expect(screen.getByRole("button", { name: "Why?" })).toHaveAttribute("aria-expanded", "false");
  });

  it("shows the text on click and hides it on a second click", async () => {
    const user = userEvent.setup();
    render(<Why text="Because it was rated low." />);
    await user.click(screen.getByRole("button", { name: "Why?" }));
    expect(screen.getByText("Because it was rated low.")).toBeInTheDocument();
    const hide = screen.getByRole("button", { name: "Hide why" });
    expect(hide).toHaveAttribute("aria-expanded", "true");
    await user.click(hide);
    expect(screen.queryByText("Because it was rated low.")).toBeNull();
  });

  it("toggles from the keyboard", async () => {
    const user = userEvent.setup();
    render(<Why text="Reason." />);
    await user.tab();
    await user.keyboard("{Enter}");
    expect(screen.getByText("Reason.")).toBeInTheDocument();
  });
});
