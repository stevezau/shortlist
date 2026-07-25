import { useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { usePatchUser } from "@/lib/queries";
import type { User } from "@/lib/types";

/** Per-user request tag: the label added in Sonarr/Radarr to titles requested for this person. */
export function UserRequestTag({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const [tag, setTag] = useState(user.request_tag ?? "");
  const [saved, setSaved] = useState(false);

  // Save on blur only if it actually changed, so tabbing through the field never fires a no-op PATCH.
  const save = () => {
    const next = tag.trim();
    if (next === (user.request_tag ?? "").trim()) return;
    setSaved(false);
    patchUser.mutate(
      { id: user.id, patch: { request_tag: next } },
      { onSuccess: () => setSaved(true) },
    );
  };

  return (
    <Card>
      <CardContent className="space-y-2 pt-6">
        <div className="flex items-center gap-2">
          <Label htmlFor="user-request-tag">Request tag (optional)</Label>
          <SavedIndicator show={saved} />
        </div>
        <Input
          id="user-request-tag"
          value={tag}
          onChange={(event) => setTag(event.target.value)}
          onBlur={save}
          placeholder="e.g. sarah"
          maxLength={64}
          className="max-w-xs"
        />
        <p className="text-sm text-muted-foreground">
          When Requests are on, titles asked for because{" "}
          {user.display_name || user.username} wanted them get this tag in
          Sonarr/Radarr — on top of your global tag and each row’s own tag.
          Leave blank for none.
        </p>
        {patchUser.isError && (
          <p role="alert" className="text-sm text-destructive">
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
