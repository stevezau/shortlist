import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { MobileNavigation } from "@/components/layout/mobile-navigation";

it("traps keyboard focus, closes on Escape or a link, and restores the trigger", async () => {
  render(<><MobileNavigation><a href="#rows">Rows</a><a href="#users">Users</a></MobileNavigation><button>Outside</button></>);
  const trigger = screen.getByRole("button", { name: "Open menu" });
  await userEvent.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "Main menu" });
  for (let i = 0; i < 6; i++) {
    await userEvent.tab();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
  }
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  await userEvent.click(trigger);
  await userEvent.click(screen.getByRole("link", { name: "Rows" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});
