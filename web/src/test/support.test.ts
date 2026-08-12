import { describe, expect, it } from "vitest";

import { GITHUB_REPO, newBugReportUrl } from "@/lib/support";

describe("newBugReportUrl", () => {
  it("targets the project's new-issue page with the bug label", () => {
    const url = new URL(newBugReportUrl({ current_version: "1.2.3" }));
    expect(`${url.origin}${url.pathname}`).toBe(`${GITHUB_REPO}/issues/new`);
    expect(url.searchParams.get("labels")).toBe("bug");
  });

  it("pre-fills a release's version so every report carries it", () => {
    const body =
      new URL(
        newBugReportUrl({ current_version: "9.9.9", git_branch: "v9.9.9" }),
      ).searchParams.get("body") ?? "";
    expect(body).toContain("Shortlist build: `9.9.9`");
  });

  it("stamps the COMMIT on a dev build, where the version would misdirect triage", () => {
    // `current_version` is the last released version, so a report from five commits past 1.4.0
    // would otherwise be triaged against 1.4.0 — the version the reporter is not running.
    const body =
      new URL(
        newBugReportUrl({
          current_version: "1.4.0",
          git_branch: "dev",
          git_sha: "2ee14f8c43954588eb720d4b0d1fab4fa50f7013",
        }),
      ).searchParams.get("body") ?? "";
    expect(body).toContain("Shortlist build: `dev 2ee14f8`");
    expect(body).not.toContain("1.4.0");
  });

  it("says 'unknown' rather than leaving the build blank", () => {
    const body = new URL(newBugReportUrl(undefined)).searchParams.get("body") ?? "";
    expect(body).toContain("Shortlist build: `unknown`");
  });
});
