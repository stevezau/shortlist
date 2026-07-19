/** Where Help / Report-a-bug send people: the project's GitHub. */
export const GITHUB_REPO = "https://github.com/stevezau/shortlist";

/** A "new issue" URL with the bug body pre-filled — the version and browser are the two facts every
 *  report needs and users never think to include, so we add them for free. Everything else is a
 *  prompt they fill in. GitHub reads `body`/`labels` from the query string. */
export function newBugReportUrl(version: string): string {
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
    "**Diagnostics** (paste from the sidebar → Copy diagnostics — it's secrets-free)",
    "",
    "",
    "---",
    `Shortlist version: \`${version || "unknown"}\``,
    `Browser: \`${typeof navigator === "undefined" ? "unknown" : navigator.userAgent}\``,
  ].join("\n");
  const params = new URLSearchParams({ labels: "bug", body });
  return `${GITHUB_REPO}/issues/new?${params.toString()}`;
}
