import { type ReactNode, useId, useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, apiErrorMessage } from "@/lib/api";
import { settingNumber } from "@/lib/format";
import {
  usePatchUser,
  useSeerrOptions,
  useSettings,
  useUsers,
} from "@/lib/queries";
import { personName } from "@/lib/user-names";
import type { User } from "@/lib/types";

const selectClass =
  "h-9 w-full rounded-md border bg-elevated px-3 text-sm focus-visible:outline-none " +
  "focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60 " +
  "sm:w-auto sm:min-w-[20rem] sm:max-w-full";

/**
 * Which Overseerr account a "Your requests" row reads this person's requests from.
 *
 * A just-in-case setting: Shortlist links an account by its Plex sign-in on its own, so a person who
 * is linked that way gets one quiet line and a Change button. The picker is open straight away only
 * when nothing links them (a local Overseerr account, a managed profile, or an owner who requests as
 * the admin and watches on another profile), or when the owner already chose one. It never guesses
 * from a name or an email: a wrong guess puts one person's requests in another person's private row.
 */
export function UserSeerrAccount({ user }: { user: User }) {
  const options = useSeerrOptions(true);
  const users = useUsers();
  const settings = useSettings();
  const patchUser = usePatchUser();
  const selectId = useId();
  const headingId = useId();
  const [changing, setChanging] = useState(false);
  const [saved, setSaved] = useState(false);
  // The heading lives here, not on the page, so a server with no Overseerr shows no empty section.
  const frame = (body: ReactNode) => (
    <section aria-labelledby={headingId} className="space-y-3">
      <h2 id={headingId} className="text-lg font-semibold">Overseerr account</h2>
      {body}
    </section>
  );

  if (options.isPending) {
    return frame(<Skeleton data-testid="seerr-account-loading" className="h-24 w-full" />);
  }
  if (options.isError) {
    // 409 = Overseerr isn't connected: there is nothing to choose, so say nothing.
    if (options.error instanceof ApiError && options.error.status === 409) {
      return null;
    }
    return frame(
      <Card className="shadow-none">
        <CardContent className="space-y-2 p-4">
          <p role="alert" className="text-sm text-destructive-text">
            {apiErrorMessage(
              options.error,
              "Couldn’t read the accounts from Overseerr.",
            )}
          </p>
          <Button variant="outline" size="sm" onClick={() => void options.refetch()}>
            Try again
          </Button>
        </CardContent>
      </Card>,
    );
  }

  const accounts = options.data.users;
  const chosenId = user.seerr_user_id;
  const automatic = accounts.find(
    (a) => a.plex_id != null && a.plex_id === user.plex_account_id,
  );
  const requestAsId = settingNumber(
    settings.data ?? {},
    "requests.overseerr.request_as_user_id",
    0,
  );
  const others = (users.data ?? []).filter((u) => u.id !== user.id);
  // Whoever ELSE the owner handed an account to: their claim beats the account's own Plex link.
  const chosenFor = (accountId: number): User | undefined =>
    others.find((u) => u.seerr_user_id === accountId);
  const handedTo = automatic ? chosenFor(automatic.id) : undefined;
  const optionName = (account: (typeof accounts)[number]): string => {
    const holder = chosenFor(account.id);
    if (holder) return `${account.name} (linked to ${personName(holder)})`;
    if (account.plex_id == null || account.plex_id === user.plex_account_id) return account.name;
    const person = others.find((u) => u.plex_account_id === account.plex_id);
    return person ? `${account.name} (signs in as ${personName(person)})` : account.name;
  };
  // An account chosen earlier and since deleted in Overseerr: without its own option the select
  // would show "Automatic", which is not what a run uses.
  const chosenMissing = chosenId !== 0 && !accounts.some((a) => a.id === chosenId);

  const save = (id: number) => {
    setSaved(false);
    patchUser.mutate(
      { id: user.id, patch: { seerr_user_id: id } },
      {
        onSuccess: () => {
          setSaved(true);
          setChanging(false);
        },
      },
    );
  };

  const quiet = chosenId === 0 && automatic !== undefined && !handedTo && !changing;

  return frame(
    <Card className="shadow-none">
      <CardContent className="space-y-2 break-words p-4">
        {quiet ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <p className="text-sm">Linked automatically as {automatic.name}</p>
            <SavedIndicator show={saved} />
            <Button variant="outline" size="sm" onClick={() => setChanging(true)}>
              Change
            </Button>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <Label htmlFor={selectId}>Their Overseerr account</Label>
              <SavedIndicator show={saved} />
              {patchUser.isPending && (
                <span className="ml-auto text-xs text-muted-foreground">Saving…</span>
              )}
            </div>
            {chosenId === 0 && automatic && handedTo && (
              <p className="text-sm">
                {automatic.name} is linked to {personName(handedTo)} instead
              </p>
            )}
            {accounts.length === 0 && !chosenMissing ? (
              <p className="text-sm text-muted-foreground">
                Overseerr has no accounts to choose from yet. Add one there, then come back.
              </p>
            ) : (
              <select
                id={selectId}
                className={selectClass}
                disabled={patchUser.isPending}
                value={chosenId}
                onChange={(event) => save(Number(event.target.value))}
              >
                <option value={0}>Automatic (their Plex sign-in)</option>
                {chosenMissing && (
                  <option value={chosenId}>Account {chosenId} (no longer in Overseerr)</option>
                )}
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {optionName(account)}
                  </option>
                ))}
              </select>
            )}
            {chosenId !== 0 && chosenId === requestAsId && (
              <p role="alert" className="text-sm text-warning">
                Shortlist files its own requests as this account, so none of its
                requests count as theirs.
              </p>
            )}
          </>
        )}
        <p className="text-sm text-muted-foreground">
          Only needed when they use a local Overseerr account, are a managed
          profile, or you request as one account and watch on another.
        </p>
        {patchUser.isError && (
          <p role="alert" className="text-sm text-destructive-text">
            {apiErrorMessage(
              patchUser.error,
              "Couldn’t save this account. Try again.",
            )}
          </p>
        )}
      </CardContent>
    </Card>,
  );
}
