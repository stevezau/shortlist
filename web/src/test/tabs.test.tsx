import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { Tabs, TabPanel } from "@/components/ui/tabs";

function Example() {
  const [value, setValue] = useState("rows");
  return <><Tabs id="details" ariaLabel="Details" value={value} onChange={setValue} options={[{ value: "rows", label: "Rows" }, { value: "history", label: "History" }, { value: "settings", label: "Settings" }]} /><TabPanel id="details" value={value}>{value} content</TabPanel></>;
}

describe("Tabs", () => {
  it("keeps one tab in the tab order and connects the selected panel", async () => {
    render(<Example />);
    const rows = screen.getByRole("tab", { name: "Rows" });
    rows.focus();
    await userEvent.keyboard("{ArrowRight}");
    const history = screen.getByRole("tab", { name: "History" });
    expect(history).toHaveFocus();
    expect(history).toHaveAttribute("aria-selected", "true");
    expect(rows).toHaveAttribute("tabindex", "-1");
    expect(history).toHaveAttribute("aria-controls", screen.getByRole("tabpanel").id);
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("History");
    await userEvent.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Settings" })).toHaveFocus();
    await userEvent.keyboard("{ArrowRight}");
    expect(rows).toHaveFocus();
    await userEvent.keyboard("{End}{Home}");
    expect(rows).toHaveFocus();
  });
});
