import { useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { usePatchUser } from "@/lib/queries";
import type { User } from "@/lib/types";

/**
 * What to call this person in a row title.
 *
 * Plex usernames are often an email or a handle nobody actually uses, and `{user}` put that straight
 * onto a Home screen. The nickname always wins; blank falls back to whatever Tautulli calls them,
 * then their Plex username. It never touches their slug, so their row's label — and the share
 * filters that hide it from everyone else — are unaffected.
 */
export function UserNickname({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const [nickname, setNickname] = useState(user.nickname ?? "");
  const [saved, setSaved] = useState(false);

  const fallback = user.friendly_name || user.username;
  const fallbackSource = user.friendly_name ? "Tautulli" : "Plex";

  // Save on blur only if it actually changed, so tabbing through never fires a no-op PATCH.
  const save = () => {
    const next = nickname.trim();
    if (next === (user.nickname ?? "").trim()) return;
    setSaved(false);
    patchUser.mutate(
      { id: user.id, patch: { nickname: next } },
      { onSuccess: () => setSaved(true) },
    );
  };

  return (
    <Card className="shadow-none">
      <CardContent className="space-y-2 p-4">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Label htmlFor="user-nickname">Nickname (optional)</Label>
          <SavedIndicator show={saved} />
          <span className="ml-auto text-xs text-muted-foreground">{patchUser.isPending ? "Saving…" : "Saves when you leave the field"}</span>
        </div>
        <Input
          id="user-nickname"
          value={nickname}
          onChange={(event) => setNickname(event.target.value)}
          onBlur={save}
          placeholder={fallback}
          maxLength={255}
          className="max-w-xs"
        />
        <p className="break-words text-sm text-muted-foreground">
          Used for <code>{"{user}"}</code> in row names. Leave blank to use{" "}
          {fallbackSource === "Tautulli"
            ? "their Tautulli name"
            : "their Plex username"}
          . Saving renames existing Plex rows; privacy is unchanged.
        </p>
        {patchUser.isError && (
          <p role="alert" className="text-sm text-destructive-text">
            {apiErrorMessage(
              patchUser.error,
              "Couldn’t save this nickname. Try again.",
            )}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
