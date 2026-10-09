import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { RunProgress } from "@/components/runs/run-progress";
import type { RunDetail, RunLogEntry } from "@/lib/types";

function fixture(): RunDetail {
  return { id: 1, status: "running", began_at: "2026-09-29T00:00:00Z", finished_at: null,
    stats: { expected_users: [{ slug: "done" }, { slug: "failed" }, { slug: "skipped" }, { slug: "active" }] },
    users: [{ slug: "done", username: "Alex", status: "ok", error: null },
      { slug: "failed", username: "Sam", status: "error", error: "Plex timeout" },
      { slug: "skipped", username: "Jess", status: "skipped", error: null },
      { slug: "active", username: "Robin", status: "pending", error: null }],
  } as unknown as RunDetail;
}
const entries = [{ seq: 1, user: "active", stage: "delivering", counts: { row: "✨ {library_name} Picked for You", library: "Movies", added: 20, removed: 9 } }] as RunLogEntry[];
function show(run: RunDetail) { render(<MemoryRouter><RunProgress run={run} entries={entries} /></MemoryRouter>); }
describe("Live run progress", () => {
  it("shows every processed person, including distinct failures and skips, without disclosure", () => {
    show(fixture());
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "3");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuemax", "4");
    expect(screen.getByRole("link", { name: /Alex/ })).toBeVisible();
    expect(screen.getByRole("link", { name: /Sam · failed/ })).toBeVisible();
    expect(screen.getByRole("link", { name: /Jess · skipped/ })).toBeVisible();
    expect(screen.getByText("✨ Movies Picked for You")).toBeVisible();
    expect(screen.getByRole("link", { name: /Open Robin’s row and library details/ })).toHaveAttribute("href", "/?user=active#run-rows");
  });
  it("labels a queued run as waiting rather than in progress", () => {
    const run = fixture(); run.status = "queued"; run.began_at = null; run.users = []; show(run);
    expect(screen.getByText("Waiting to start")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Queued — waiting to start" })).toBeVisible();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
  });
  it("does not invent a denominator for older runs without a roster", () => {
    const run = fixture();run.stats = { users_ok: 0, users_error: 0 };show(run);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
  it.each(["ok", "error", "aborted"])("does not show live progress for a finished %s run", (status) => {
    const run = fixture();run.status = status as RunDetail["status"];run.finished_at = "2026-09-29T00:01:00Z";show(run);
    expect(screen.queryByRole("region", { name: "Live run progress" })).not.toBeInTheDocument();
  });
});
