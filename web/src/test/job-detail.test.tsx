import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { JobDetail } from "@/components/jobs/job-history";
import { fieldRows, formatFieldValue } from "@/lib/job-fields";
import type { Job } from "@/lib/types";

function job(patch: Partial<Job>): Job {
  return {
    id: 1,
    kind: "sync.check",
    status: "done",
    attempts: 1,
    max_attempts: 3,
    detail: "",
    error: null,
    payload: {},
    result: {},
    created_at: "2026-08-12T10:00:00Z",
    started_at: "2026-08-12T10:00:01Z",
    finished_at: "2026-08-12T10:00:05Z",
    ...patch,
  };
}

describe("formatFieldValue", () => {
  it("reads booleans, numbers, lists and nested objects as plain text", () => {
    expect(formatFieldValue(true)).toBe("Yes");
    expect(formatFieldValue(false)).toBe("No");
    expect(formatFieldValue(1234)).toBe((1234).toLocaleString());
    expect(formatFieldValue(["a", "b"])).toBe("a, b");
    expect(formatFieldValue({ cache_rows: 3, skipped: false })).toBe(
      "cache rows: 3, skipped: No",
    );
    expect(formatFieldValue(null)).toBe("");
  });
});

describe("fieldRows", () => {
  it("labels known keys, humanises unknown ones, and hides the summary line", () => {
    expect(
      fieldRows(
        { fixed: 2, orphans_removed: 1, detail: "x", quiet: true },
        { skipHidden: true },
      ),
    ).toEqual([
      ["Rows fixed", "2"],
      ["Orphans removed", "1"],
    ]);
  });
});

describe("JobDetail", () => {
  it("shows payload and result as labelled rows, never as JSON", () => {
    const { container } = render(
      <JobDetail
        job={job({
          payload: { dry_run: true, slug: "alice" },
          result: { fixed: 3, standing: ["a", "b"], detail: "done" },
        })}
      />,
    );
    expect(screen.getByText("Asked: preview only")).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument();
    expect(screen.getByText("Rows fixed")).toBeInTheDocument();
    expect(screen.getByText("a, b")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/[{}"]/);
    expect(screen.queryByText("detail")).not.toBeInTheDocument();
  });

  it("keeps the error box and the retry wording", () => {
    render(
      <JobDetail job={job({ status: "queued", attempts: 1, error: "boom" })} />,
    );
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.getByText("Last attempt")).toBeInTheDocument();
    expect(screen.getByText("1 of 3")).toBeInTheDocument();
  });
});
