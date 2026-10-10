import { useState, type ReactNode } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { usePatchUser } from "@/lib/queries";
import type { User } from "@/lib/types";

/** The free-text columns of a user that save from their own card on the person's page. */
type BlurSaveKey = "nickname" | "request_tag" | "requested_by_tag";

/**
 * A card with one text input that PATCHes the user when the field loses focus.
 *
 * Shared by the nickname and the two request tags, which differ only in their label, column, limit
 * and help text.
 */
export function BlurSaveField({
  user,
  field,
  id,
  label,
  placeholder,
  maxLength,
  help,
  errorText,
  helpClassName = "break-words text-sm text-muted-foreground",
}: {
  user: User;
  field: BlurSaveKey;
  id: string;
  label: ReactNode;
  placeholder: string;
  maxLength: number;
  help: ReactNode;
  errorText: string;
  helpClassName?: string;
}) {
  const patchUser = usePatchUser();
  const [value, setValue] = useState(user[field] ?? "");
  const [saved, setSaved] = useState(false);

  // Save on blur only if it actually changed, so tabbing through never fires a no-op PATCH.
  const save = () => {
    const next = value.trim();
    if (next === (user[field] ?? "").trim()) return;
    setSaved(false);
    patchUser.mutate({ id: user.id, patch: { [field]: next } }, { onSuccess: () => setSaved(true) });
  };

  return (
    <Card className="shadow-none">
      <CardContent className="space-y-2 p-4">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Label htmlFor={id}>{label}</Label>
          <SavedIndicator show={saved} />
          <span className="ml-auto text-xs text-muted-foreground">
            {patchUser.isPending ? "Saving…" : "Saved as you go"}
          </span>
        </div>
        <Input
          id={id}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onBlur={save}
          placeholder={placeholder}
          maxLength={maxLength}
          className="max-w-xs"
        />
        <p className={helpClassName}>{help}</p>
        {patchUser.isError && (
          <p role="alert" className="text-sm text-destructive-text">
            {apiErrorMessage(patchUser.error, errorText)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
