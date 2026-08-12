/** Where Help / Report-a-bug send people: the project's GitHub. */
export const GITHUB_REPO = "https://github.com/stevezau/shortlist";

/** A "new issue" URL with the bug body pre-filled — the build and browser are the two facts every
 *  report needs and users never think to include, so we add them for free. Everything else is a
 *  prompt they fill in. GitHub reads `body`/`labels` from the query string.
 *
 *  Takes the whole version payload rather than a bare version STRING, because on a `:dev` build the
 *  version alone is wrong in the place it does the most damage: `current_version` is the last
 *  released version, so a report from five commits past 1.4.0 would be triaged against 1.4.0. On a
 *  pre-release the commit is what identifies the build, so that is what gets stamped. */
export function newBugReportUrl(
  info?: {
    current_version?: string;
    git_branch?: string;
    git_sha?: string;
  } | null,
): string {
  const branch = info?.git_branch;
  const version = info?.current_version;
  const build =
    branch && !branch.startsWith("v")
      ? `${branch} ${info?.git_sha?.slice(0, 7) ?? ""}`.trim()
      : version;
  const body = [
    "**What happened?**",
    "",
    "",
    "**What did you expect instead?**",
    "",
    "",
    "**Steps to reproduce**",
    "1. ",
    "2. ",
    "",
    "**Diagnostics** (Have an issue? → Copy the full report — it's secrets-free)",
    "",
    "",
    "---",
    `Shortlist build: \`${build || "unknown"}\``,
    `Browser: \`${typeof navigator === "undefined" ? "unknown" : navigator.userAgent}\``,
  ].join("\n");
  const params = new URLSearchParams({ labels: "bug", body });
  return `${GITHUB_REPO}/issues/new?${params.toString()}`;
}
