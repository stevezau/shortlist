import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Segmented } from "@/components/segmented";

const OPTIONS = [
  { value: "a", label: "Apple" },
  { value: "b", label: "Banana" },
];

describe("Segmented", () => {
  it("marks the active option pressed and the rest not", () => {
    render(<Segmented value="a" options={OPTIONS} onChange={() => {}} />);

    expect(screen.getByRole("button", { name: "Apple" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Banana" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("calls onChange with the clicked option's value", async () => {
    const onChange = vi.fn();
    render(<Segmented value="a" options={OPTIONS} onChange={onChange} />);

    await userEvent.click(screen.getByRole("button", { name: "Banana" }));

    expect(onChange).toHaveBeenCalledExactlyOnceWith("b");
  });

  it("wraps the buttons in a labelled fieldset when a legend is given", () => {
    render(
      <Segmented
        legend="Fruit"
        value="a"
        options={OPTIONS}
        onChange={() => {}}
      />,
    );

    expect(screen.getByRole("group", { name: "Fruit" })).toBeInTheDocument();
  });

  it("exposes an aria-label group when there is no visible legend", () => {
    render(
      <Segmented
        ariaLabel="Fruit choice"
        value="a"
        options={OPTIONS}
        onChange={() => {}}
      />,
    );

    expect(
      screen.getByRole("group", { name: "Fruit choice" }),
    ).toBeInTheDocument();
  });
});
