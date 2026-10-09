import type { Modified } from "@/components/settings/modified-marks";

/** A dot and "Modified", beside a setting's name. */
export function ModifiedBadge({ modified }: { modified?: Modified }) {
  if (!modified) return null;
  return (
    <span className="ml-2 inline-flex items-center gap-1.5 align-middle text-xs font-normal text-muted-foreground">
      <span aria-hidden="true" className="size-1.5 rounded-full bg-foreground" />
      Modified
    </span>
  );
}

/** "default: 12 titles · Reset to default", under a modified setting. */
export function ModifiedDefault({ modified, name }: { modified?: Modified; name: string }) {
  if (!modified) return null;
  return (
    <p className="text-xs text-muted-foreground">
      default: {modified.defaultLabel} ·{" "}
      <button
        type="button"
        onClick={modified.reset}
        aria-label={`Reset ${name} to default`}
        className="rounded-sm underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Reset to default
      </button>
    </p>
  );
}
