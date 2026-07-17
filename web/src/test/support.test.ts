import { describe, expect, it } from "vitest";

import { GITHUB_REPO, newBugReportUrl } from "@/lib/support";

describe("newBugReportUrl", () => {
  it("targets the project's new-issue page with the bug label", () => {
    const url = new URL(newBugReportUrl("1.2.3"));
    expect(`${url.origin}${url.pathname}`).toBe(`${GITHUB_REPO}/issues/new`);
    expect(url.searchParams.get("labels")).toBe("bug");
  });

  it("pre-fills the version so every report carries it", () => {
    const body =
      new URL(newBugReportUrl("9.9.9")).searchParams.get("body") ?? "";
    expect(body).toContain("Shortlist version: `9.9.9`");
  });

  it("says 'unknown' rather than leaving the version blank", () => {
    const body = new URL(newBugReportUrl("")).searchParams.get("body") ?? "";
    expect(body).toContain("Shortlist version: `unknown`");
  });
});
