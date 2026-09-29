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
    <Card className="shadow-none">
      <CardContent className="space-y-2 p-4">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Label htmlFor="user-request-tag">Request tag (optional)</Label>
          <SavedIndicator show={saved} />
          <span className="ml-auto text-xs text-muted-foreground">{patchUser.isPending ? "Saving…" : "Saves when you leave the field"}</span>
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
        <p className="break-words text-sm text-muted-foreground">
          Added in Sonarr/Radarr to titles Shortlist requests for this person,
          alongside your global and row tags. Leave blank for none.
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
