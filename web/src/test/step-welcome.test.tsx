import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StepWelcome } from "@/pages/setup/step-welcome";

function renderStep() {
  render(<StepWelcome data={{}} update={vi.fn()} next={vi.fn()} complete={vi.fn()} />);
}

describe("StepWelcome", () => {
  it("states the owner limitation in its own note before Get started", () => {
    renderStep();

    const note = screen.getByRole("note");
    expect(note).toHaveTextContent("Plex cannot hide other people’s rows from the server owner");
    const getStarted = screen.getByRole("button", { name: "Get started" });
    // DOCUMENT_POSITION_FOLLOWING: the button comes after the note in reading order.
    expect(note.compareDocumentPosition(getStarted) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
