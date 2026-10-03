import { ChevronDown } from "lucide-react";
import { useState, type ReactNode } from "react";

import { PeopleBrowser } from "@/components/rows/people-browser";
import { Segmented } from "@/components/segmented";
import { UserAvatar } from "@/components/user-avatar";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import type { CollectionInput, User } from "@/lib/types";
import { cn } from "@/lib/utils";

type AudiencePatch = Pick<CollectionInput, "audience" | "audience_user_ids">;

/**
 * "Who gets this row?" — Everyone vs a hand-picked subset, and (when subset) the per-user toggle
 * list. The list can be a long scroll on a big server, so it's tucked behind a disclosure: expanded
 * when you first choose "Choose people", collapsed to a one-line summary when you reopen the editor.
 * Emits a patch the row editor merges into its draft.
 */
export function AudiencePicker({
  audience,
  audienceUserIds,
  users,
  onChange,
  children,
}: {
  audience: CollectionInput["audience"];
  audienceUserIds: number[];
  users: User[];
  onChange: (patch: AudiencePatch) => void;
  /** The row editor supplies its table of actual library and privacy information. */
  children?: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const chosen = audienceUserIds.length;
  const enabledCount = users.filter((user) => user.enabled).length;
  const people = children ?? (
    <PeopleBrowser users={users}>
      {(visible) => <ul aria-label="People" className="divide-y">
        {visible.map((user) => (
          <li key={user.id} className="flex min-w-0 items-center justify-between gap-3 py-2">
            <span className="flex min-w-0 items-center gap-2 text-sm">
              <UserAvatar name={user.username} size="sm" />
              <span className="min-w-0 break-words">
                {user.display_name || user.username}
                {!user.enabled && <span className="block text-xs text-muted-foreground">Shortlist disabled</span>}
              </span>
            </span>
            {audience === "subset" && <Switch
              checked={audienceUserIds.includes(user.id)}
              onCheckedChange={(checked) => onChange({
                audience: "subset",
                audience_user_ids: checked ? [...audienceUserIds, user.id] : audienceUserIds.filter((id) => id !== user.id),
              })}
              aria-label={user.username}
            />}
          </li>
        ))}
      </ul>}
    </PeopleBrowser>
  );

  return (
    <div className="space-y-2">
      <Label>Who gets it?</Label>
      <Segmented
        value={audience}
        onChange={(next) => {
          if (next === "subset") setExpanded(true); // choosing people opens the list to pick them
          onChange({ audience: next, audience_user_ids: audienceUserIds });
        }}
        options={[
          { value: "everyone", label: "Everyone" },
          { value: "subset", label: "Choose people" },
        ]}
      />
      {/* "Everyone" means every ENABLED user, not every Plex account — spell out the real reach so a
          mostly-disabled roster doesn't look like the row is broken. */}
      {audience === "everyone" && users.length > 0 && (
        <p
          className={cn(
            "text-xs",
            enabledCount === 0 ? "text-warning" : "text-muted-foreground",
          )}
        >
          {enabledCount === 0
            ? "No users are enabled, so this reaches nobody yet — enable people on the Users page."
            : `Reaches everyone with Shortlist enabled — ${enabledCount} of ${users.length} ${users.length === 1 ? "user is" : "users are"} enabled right now.`}
          {enabledCount > 0 &&
            enabledCount < users.length &&
            " Enable more on the Users page."}
        </p>
      )}
      <div className="mt-2 border-t">
          {audience === "subset" && <button
            type="button"
            onClick={() => setExpanded((open) => !open)}
            aria-expanded={expanded}
            className="flex w-full items-center justify-between gap-2 rounded-md py-2 text-left text-sm hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className={cn(chosen === 0 && "text-warning")}>
              {users.length === 0
                ? "No users yet"
                : chosen === 0
                  ? "Nobody chosen — pick at least one person"
                  : `${chosen} of ${users.length} ${users.length === 1 ? "person" : "people"} chosen`}
            </span>
            <ChevronDown
              aria-hidden="true"
              className={cn(
                "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
                expanded && "rotate-180",
              )}
            />
          </button>}
          {(audience === "everyone" || expanded) && (
            <div className="space-y-3 pt-2">
              {audience === "subset" && <p className="text-xs text-muted-foreground">Choices stay selected when you search or change pages. Disabled or paused people get no copy until they are enabled and unpaused.</p>}
              {people}
            </div>
          )}
      </div>
    </div>
  );
}
