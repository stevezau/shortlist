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
    <Card>
      <CardContent className="space-y-2 pt-6">
        <div className="flex items-center gap-2">
          <Label htmlFor="user-nickname">Nickname (optional)</Label>
          <SavedIndicator show={saved} />
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
        <p className="text-sm text-muted-foreground">
          Used wherever a row title says <code>{"{user}"}</code> — so “
          {"{user}'s picks"}” becomes “
          {(nickname.trim() || fallback) + "’s picks"}”. Leave blank to use{" "}
          {fallbackSource === "Tautulli"
            ? "their Tautulli name"
            : "their Plex username"}{" "}
          ({fallback}). Existing rows are renamed on Plex when you save; their
          privacy is unaffected.
        </p>
        {patchUser.isError && (
          <p role="alert" className="text-sm text-destructive">
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
