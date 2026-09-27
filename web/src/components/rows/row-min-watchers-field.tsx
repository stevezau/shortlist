import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { CollectionInput, User } from "@/lib/types";

/**
 * A shared row is built only from titles SEVERAL people have watched, so one whose audience holds
 * fewer enabled people than its threshold can never produce anything — it just reports "skipped"
 * every run. Say so here, where it can still be fixed, rather than leaving someone to read a silent
 * skip as a broken app (issue #3).
 */
function SharedRowReachWarning({
  users,
  audience,
  audienceUserIds,
  minWatchers,
}: {
  users: User[];
  audience: "everyone" | "subset";
  audienceUserIds: number[];
  minWatchers: number;
}) {
  // Unknown user list (still loading) — say nothing rather than cry wolf.
  if (users.length === 0) return null;
  // The engine's audience is enabled AND not paused (a paused user is dropped before any row is
  // built), so counting only `enabled` would stay silent on a row that genuinely cannot build.
  const reach = users.filter(
    (user) =>
      user.enabled &&
      !user.prefs?.paused &&
      (audience === "everyone" || audienceUserIds.includes(user.id)),
  ).length;
  if (reach >= minWatchers) return null;
  return (
    <p
      role="status"
      className="rounded-md border border-warning/40 bg-warning/5 p-3 text-sm"
    >
      This row can’t build yet: it needs {minWatchers} people with viewing in
      common, but{" "}
      {reach === 0
        ? "nobody in its audience is active in runs"
        : `only ${reach} of them ${reach === 1 ? "is" : "are"} active in runs`}{" "}
      (enabled and not paused). Add more people to the audience, or make this a
      per-person row so each of them gets their own.
    </p>
  );
}

/** "Only titles watched by at least [N] people" (`min_watchers`), with the reach warning. */
export function MinWatchersField({
  input,
  set,
  users,
}: {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
  users: User[];
}) {
  return (
    <div data-setting="min_watchers" className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Label htmlFor="min-watchers">Only titles watched by at least</Label>
        <Input
          id="min-watchers"
          type="number"
          min={2}
          max={50}
          value={input.min_watchers}
          onChange={(event) =>
            set({ min_watchers: Math.max(2, Number(event.target.value) || 2) })
          }
          className="w-20"
        />
        <span className="text-sm font-medium">people</span>
      </div>
      <p className="text-sm text-muted-foreground">
        Keeps one person’s viewing from ever showing up in a shared row. 2 is a
        good default.
      </p>
      <SharedRowReachWarning
        users={users}
        audience={input.audience}
        audienceUserIds={input.audience_user_ids}
        minWatchers={input.min_watchers}
      />
    </div>
  );
}
