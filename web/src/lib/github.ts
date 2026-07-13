import type { RunDetail, RunUserResult } from "@/lib/types";

/** A copy-pasteable GitHub-issue body for a failed per-user run result. */
export function githubIssueSnippet(
  run: RunDetail,
  result: RunUserResult,
): string {
  return [
    "### Rowarr run error",
    "",
    `- Run: #${run.id} (${run.trigger}${run.dry_run ? ", dry-run" : ""})`,
    `- Started: ${run.started_at}`,
    `- User: ${result.slug}`,
    `- Status: ${result.status}`,
    "",
    "```",
    result.error ?? "(no error message)",
    "```",
  ].join("\n");
}
