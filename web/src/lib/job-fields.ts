/** Plain-English labels for the payload and result keys the server's job handlers use
 *  (`shortlist/server/services/jobs.py`). A key not listed falls back to a humanised version. */
const LABELS: Record<string, string> = {
  dry_run: "Preview only",
  confirmed: "Confirmed",
  scheduled: "Scheduled",
  reason: "Reason",
  slug: "Row",
  row: "Row",
  build: "Row type",
  template: "Template",
  scope: "Scope",
  label: "Label",
  max_keep: "Backups to keep",
  only_user_ids: "Only people",
  in_sections: "Only sections",
  from_user_id: "Copy from person",
  to_user_id: "Copy to person",
  snapshot_id: "Snapshot",
  item: "Message",
  fixed: "Rows fixed",
  orphans: "Orphans removed",
  swept: "Rows swept",
  converged: "Filters brought in line",
  standing: "Still standing",
  skipped: "Skipped",
  added: "New accounts",
  updated: "Accounts updated",
  total: "Accounts",
  path: "Backup file",
  users_credited: "People credited",
  targets: "Themes checked",
  promoted: "Promoted",
  discarded: "Discarded",
  skipped_paused: "Skipped (paused)",
  failed: "Failed",
  topped_up: "Topped up",
  runs: "Runs removed",
  events: "Events removed",
  cache_rows: "Cache rows removed",
  assistant: "Assistant records removed",
  removed: "Removed",
  hidden: "Hidden",
  restored: "Restored",
  changed: "Changed",
  collections: "Collections touched",
  sent: "Sent",
};

/** Result keys that are not worth a row: `detail` is already the summary line, `quiet` is
 *  bookkeeping for the notification bell. */
const HIDDEN = new Set(["detail", "quiet"]);

export function humanizeKey(key: string): string {
  const spaced = key.replace(/[_.]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function fieldLabel(key: string): string {
  return LABELS[key] ?? humanizeKey(key);
}

/** A value as a person would say it: Yes/No, numbers as numbers, lists joined, nested objects as
 *  short "key: value" text. Empty values render as "" so the caller can skip them. */
export function formatFieldValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return value.toLocaleString();
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(formatFieldValue).join(", ");
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => [humanizeKey(k).toLowerCase(), formatFieldValue(v)])
      .filter(([, v]) => v !== "")
      .map(([k, v]) => `${k}: ${v}`)
      .join(", ");
  }
  return String(value);
}

/** The labelled rows for one payload or result object, in key order. */
export function fieldRows(
  data: Record<string, unknown> | null | undefined,
  { skipHidden = false }: { skipHidden?: boolean } = {},
): [string, string][] {
  const rows: [string, string][] = [];
  for (const [key, value] of Object.entries(data ?? {})) {
    if (skipHidden && HIDDEN.has(key)) continue;
    const rendered = formatFieldValue(value);
    if (rendered) rows.push([fieldLabel(key), rendered]);
  }
  return rows;
}
