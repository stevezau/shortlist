import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { SettingsNumberField } from "@/components/settings/number-field";

it("lets an owner replace a number before bounds and autosave apply", async () => {
  const commit = vi.fn();
  render(<SettingsNumberField id="count" value={30} min={5} max={100} onCommit={commit} />);
  const field = screen.getByRole("spinbutton");
  await userEvent.clear(field);
  await userEvent.type(field, "12");
  expect(field).toHaveValue(12);
  expect(commit).not.toHaveBeenCalled();
  await userEvent.tab();
  expect(commit).toHaveBeenCalledWith(12);
});
it("does not save an empty draft", async () => {
  const commit = vi.fn();
  render(<SettingsNumberField id="count" value={30} min={5} max={100} onCommit={commit} />);
  await userEvent.clear(screen.getByRole("spinbutton"));
  await userEvent.tab();
  expect(commit).not.toHaveBeenCalled();
  expect(screen.getByRole("spinbutton")).toHaveValue(30);
});
