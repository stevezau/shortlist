import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { UserTabs } from "@/components/runs/user-tabs";
import type { RunUserResult } from "@/lib/types";

const results = [
  { slug: "failed", username: "Failed Person", status: "error", error: "Write failed" },
  { slug: "ok", username: "Finished Person", status: "ok", error: null },
  { slug: "skipped", username: "Skipped Person", status: "skipped", error: null },
  { slug: "pending", username: "Waiting Person", status: "pending", error: null },
] as RunUserResult[];

describe("Run person navigation", () => {
  it("describes the non-error filter honestly, including waiting and skipped people", async () => {
    render(<UserTabs results={results} selected="ok" onSelect={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "No errors 3" }));
    expect(screen.queryByRole("tab", { name: /Failed Person/ })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Waiting Person/ })).toBeVisible();
    expect(screen.getByRole("tab", { name: /Skipped Person/ })).toBeVisible();
  });
  it("moves through people with the vertical arrow keys and Home/End", () => {
    const select = vi.fn();
    render(<UserTabs results={results} selected="failed" onSelect={select} />);
    const failed = screen.getByRole("tab", { name: /Failed Person/ });
    failed.focus(); fireEvent.keyDown(failed, { key: "ArrowDown" });
    expect(screen.getByRole("tab", { name: /Finished Person/ })).toHaveFocus();
    expect(select).toHaveBeenLastCalledWith("ok");
    fireEvent.keyDown(document.activeElement!, { key: "End" });
    expect(screen.getByRole("tab", { name: /Waiting Person/ })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(failed).toHaveFocus();
  });
});
