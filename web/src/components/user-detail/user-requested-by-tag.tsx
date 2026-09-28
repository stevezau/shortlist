import { useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { usePatchUser } from "@/lib/queries";
import type { User } from "@/lib/types";

/** The tag THEIR requests already carry in Radarr/Sonarr, for a Your requests row to read them by.
 *  Not {@link UserRequestTag}: that is the tag Shortlist WRITES on titles it requests for them; this
 *  is the one it READS when the row's pattern doesn't render to it (a kid whose requests are tagged
 *  `children`, say). */
export function UserRequestedByTag({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const [tag, setTag] = useState(user.requested_by_tag ?? "");
  const [saved, setSaved] = useState(false);

  // Save on blur only if it actually changed, so tabbing through the field never fires a no-op PATCH.
  const save = () => {
    const next = tag.trim();
    if (next === (user.requested_by_tag ?? "").trim()) return;
    setSaved(false);
    patchUser.mutate(
      { id: user.id, patch: { requested_by_tag: next } },
      { onSuccess: () => setSaved(true) },
    );
  };

  return (
    <Card>
      <CardContent className="space-y-2 pt-6">
        <div className="flex items-center gap-2">
          <Label htmlFor="user-requested-by-tag">
            Their request tag in Radarr/Sonarr
          </Label>
          <SavedIndicator show={saved} />
        </div>
        <Input
          id="user-requested-by-tag"
          value={tag}
          onChange={(event) => setTag(event.target.value)}
          onBlur={save}
          placeholder="e.g. children"
          maxLength={64}
          className="max-w-xs"
        />
        <p className="text-sm text-muted-foreground">
          If their requests carry a tag that doesn&rsquo;t fit the row&rsquo;s
          pattern, put it here, e.g. children.
        </p>
        {patchUser.isError && (
          <p role="alert" className="text-sm text-destructive-text">
            {apiErrorMessage(
              patchUser.error,
              "Couldn’t save this tag. Try again.",
            )}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
