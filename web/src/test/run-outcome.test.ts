import { describe, expect, it } from "vitest";

import { runOutcome } from "@/lib/run-outcome";

describe("runOutcome — a run ends three ways, not two", () => {
  it("reports a cancelled run as stopped, never as failed", () => {
    // The bug this exists to prevent: both the header pill and the wizard computed
    // `status !== "ok"`, so pressing Stop told the owner "The run failed — no rows were built".
    // Cancellation is cooperative — the engine stops taking NEW users but still runs the privacy
    // merge and the promote for everyone already delivered, so their rows are live.
    expect(runOutcome("aborted")).toBe("stopped");
  });

  it("reports a real failure as failed", () => {
    expect(runOutcome("error")).toBe("failed");
  });

  it("reports a clean run as ok", () => {
    expect(runOutcome("ok")).toBe("ok");
  });

  it("treats an unrecognised status as a failure, not a success", () => {
    // If the server ever grows a fourth status, the safe default is the one that makes the owner
    // look, rather than one that silently reports success.
    expect(
      runOutcome("something_new" as Parameters<typeof runOutcome>[0]),
    ).toBe("failed");
  });
});
