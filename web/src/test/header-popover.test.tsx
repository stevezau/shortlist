import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { HeaderPopover } from "@/components/layout/header-popover";
function Example({ width }: { width?: number }) {
  const [open, setOpen] = useState(false);
  return <HeaderPopover label="Activity" open={open} onOpenChange={setOpen} align="right" width={width} trigger={<button>Open activity</button>}><button>Details</button></HeaderPopover>;
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it("positions on initial portal mount, stays within a narrow viewport, and returns focus", async () => {
  vi.stubGlobal("innerWidth", 390);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 230, y: 12, left: 230, right: 270, top: 12, bottom: 52, width: 40, height: 40, toJSON: () => ({}) });
  render(<Example />);
  const trigger = screen.getByRole("button", { name: "Open activity" });
  await userEvent.click(trigger);
  const panel = screen.getByRole("dialog", { name: "Activity" });
  expect(panel).toHaveStyle({ left: "12px", top: "60px" });
  vi.stubGlobal("innerWidth", 320);fireEvent.resize(window);
  expect(panel).toHaveStyle({ left: "12px" });
  await userEvent.keyboard("{Escape}");
  expect(trigger).toHaveFocus();
});
it("aligns a compact action panel to its trigger instead of reserving the full activity-panel width", async () => {
  vi.stubGlobal("innerWidth", 390);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 230, y: 12, left: 230, right: 270, top: 12, bottom: 52, width: 40, height: 40, toJSON: () => ({}) });
  render(<Example width={192} />);
  await userEvent.click(screen.getByRole("button", { name: "Open activity" }));
  expect(screen.getByRole("dialog", { name: "Activity" })).toHaveStyle({ left: "78px", width: "192px" });
});
