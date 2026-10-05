import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SaveBar, SaveBarProvider } from "@/components/settings/save-bar";
import { useSaveBarReport } from "@/components/settings/save-bar-context";
import type { AutosavedSettings } from "@/lib/autosave";

const idle: AutosavedSettings = { isPending: false, isError: false, error: null, saved: false, retry: () => {} };

function Section({ id, state }: { id: string; state: AutosavedSettings }) {
  const inBar = useSaveBarReport(id, state);
  return <p>{inBar ? `${id} reports to the bar` : `${id} shows its own status`}</p>;
}

function renderBar(a: AutosavedSettings, b: AutosavedSettings) {
  return render(
    <SaveBarProvider>
      <Section id="a" state={a} />
      <Section id="b" state={b} />
      <SaveBar />
    </SaveBarProvider>,
  );
}

describe("the Settings save bar", () => {
  it("is where sections inside it report, and a section on its own keeps its own readout", () => {
    renderBar(idle, idle);
    expect(screen.getByText("a reports to the bar")).toBeInTheDocument();
    render(<Section id="alone" state={idle} />);
    expect(screen.getByText("alone shows its own status")).toBeInTheDocument();
  });

  it("says nothing is pending until something changes", () => {
    renderBar(idle, idle);
    expect(screen.getByText("Every change here saves on its own.")).toBeVisible();
  });

  it("says Saving… while any section is saving, then Saved", () => {
    const { rerender } = renderBar({ ...idle, saved: true }, { ...idle, isPending: true });
    expect(screen.getByText("Saving…")).toBeVisible();
    rerender(
      <SaveBarProvider>
        <Section id="a" state={{ ...idle, saved: true }} />
        <Section id="b" state={{ ...idle, saved: true }} />
        <SaveBar />
      </SaveBarProvider>,
    );
    expect(screen.getByText("Saved")).toBeVisible();
  });

  it("shows a failed save over everything else, and retries THAT section", async () => {
    const retryA = vi.fn();
    const retryB = vi.fn();
    renderBar({ ...idle, saved: true, retry: retryA }, { ...idle, isError: true, error: new Error("nope"), retry: retryB });
    expect(screen.getByText(/Not saved/)).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(retryB).toHaveBeenCalledTimes(1);
    expect(retryA).not.toHaveBeenCalled();
  });
});
