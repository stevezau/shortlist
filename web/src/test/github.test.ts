import { describe, expect, it } from "vitest";

import { githubIssueSnippet } from "@/lib/github";
import type { RunDetail, RunUserResult } from "@/lib/types";

function makeRun(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 12,
    trigger: "schedule",
    started_at: "2026-07-14T03:30:00Z",
    finished_at: "2026-07-14T03:35:00Z",
    status: "error",
    dry_run: false,
    stats: { users_ok: 4, users_error: 1 },
    users: [],
    ...overrides,
  };
}

function makeResult(overrides: Partial<RunUserResult> = {}): RunUserResult {
  return {
    username: "Sarah",
    slug: "sarah",
    status: "error",
    error: "TMDB 401 Unauthorized",
    duration_ms: 1200,
    llm_tokens: 0,
    diff: {},
    picks: [],
    breakdown: [],
    ...overrides,
  };
}

describe("githubIssueSnippet", () => {
  it("includes the run id, trigger, user, status, and error body", () => {
    const snippet = githubIssueSnippet(makeRun(), makeResult());

    expect(snippet).toContain("- Run: #12 (schedule)");
    expect(snippet).toContain("- User: sarah");
    expect(snippet).toContain("- Status: error");
    expect(snippet).toContain("TMDB 401 Unauthorized");
  });

  it("marks a dry-run in the run line", () => {
    const snippet = githubIssueSnippet(
      makeRun({ dry_run: true }),
      makeResult(),
    );
    expect(snippet).toContain("- Run: #12 (schedule, dry-run)");
  });

  it("substitutes a placeholder when the result carries no error message", () => {
    const snippet = githubIssueSnippet(makeRun(), makeResult({ error: null }));
    expect(snippet).toContain("(no error message)");
  });
});
