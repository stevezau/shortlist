import { Eye } from "lucide-react";
import { Link } from "react-router";

import { reachedUsers } from "@/components/rows/row-facts";
import { UserAvatar } from "@/components/user-avatar";
import type { AccountPrivacy, CollectionInput, PlexLibrary, User } from "@/lib/types";
import { cn } from "@/lib/utils";

type HidesAnswer = { text: string; tone: "ok" | "warn" | "muted"; link?: boolean };

/**
 * "Their account hides other rows", in the words of the Privacy page's own states
 * (`api/privacy.py::_account`). It REPORTS what plex.tv said when the privacy endpoint last read it;
 * nothing here judges or changes it.
 */
function hidesAnswer(account: AccountPrivacy | undefined, privacy: "loading" | "error" | "ready"): HidesAnswer {
  if (privacy === "loading") return { text: "Checking…", tone: "muted" };
  if (privacy === "error" || !account) return { text: "Not checked", tone: "muted" };
  switch (account.state) {
    case "hiding":
      return { text: "Yes", tone: "ok" };
    case "missing":
      return { text: "No, hide rules missing", tone: "warn", link: true };
    case "unreadable_filter":
      return { text: "No, Plex can't read its restrictions", tone: "warn", link: true };
    case "refused_by_plex":
      return {
        text: account.restriction_profile
          ? `No, ${account.restriction_profile} restriction profile`
          : "No, Plex won't accept hide rules",
        tone: "warn",
        link: true,
      };
    case "left_alone":
      return { text: "No, left alone by choice", tone: "muted" };
    default:
      return { text: "Not checked", tone: "muted" };
  }
}

function Hides({ answer }: { answer: HidesAnswer }) {
  const dot = (
    <span
      aria-hidden="true"
      className={cn(
        "size-1.5 shrink-0 rounded-full",
        answer.tone === "ok" ? "bg-success" : answer.tone === "warn" ? "bg-warning" : "bg-faint-foreground",
      )}
    />
  );
  if (answer.link) {
    return (
      <Link
        to="/privacy"
        className="inline-flex items-center gap-1.5 rounded-sm text-warning underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {dot}
        {answer.text} →
      </Link>
    );
  }
  return (
    <span className={cn("inline-flex items-center gap-1.5", answer.tone === "muted" && "text-muted-foreground")}>
      {dot}
      {answer.text}
    </span>
  );
}

/**
 * Who the row reaches, one line per person: where they get a copy, and whether their account keeps
 * everyone else's rows off their Home. The owner can't see as these people (PRODUCT.md principle 2),
 * so this is the closest the editor gets to "what will Sarah see".
 */
export function RowAudienceTable({
  input,
  users,
  libraries,
  accounts,
  privacy,
}: {
  input: Pick<CollectionInput, "audience" | "audience_user_ids">;
  users: User[];
  /** The libraries this row lands in, already resolved from its picker. */
  libraries: PlexLibrary[] | null;
  accounts: AccountPrivacy[];
  privacy: "loading" | "error" | "ready";
}) {
  // The owner is the line under the table, not a row in it: Plex can't restrict them, so "does
  // their account hide other rows" has only one answer, and it is the caveat itself.
  const reached = reachedUsers(input, users);
  const people = reached.filter((user) => user.user_type !== "owner");
  // Shortlist doesn't know which libraries each share opens, only which ones the row lands in, so
  // every line names the same libraries and the note under the table says what decides the rest.
  const where =
    libraries === null
      ? "Every library of its type"
      : libraries.length === 0
        ? "No library yet"
        : libraries.map((library) => library.title).join(", ");

  return (
    <div className="space-y-3">
      {people.length === 0 ? (
        <p className={cn("text-sm", reached.length === 0 ? "text-warning" : "text-muted-foreground")}>
          {reached.length === 0
            ? "Nobody gets this row yet: no one enabled is in its audience."
            : "Only you get this row: no one else enabled is in its audience."}
        </p>
      ) : (
        <div className="min-w-0 overflow-hidden rounded-md border">
          <table className="w-full table-fixed text-sm">
            <thead className="border-b bg-elevated text-left text-xs text-muted-foreground">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">
                  Person
                </th>
                <th scope="col" className="hidden px-3 py-2 font-medium sm:table-cell sm:w-[30%]">
                  Gets a copy in
                </th>
                <th scope="col" className="w-[40%] px-3 py-2 font-medium sm:w-[28%]">
                  <span className="sm:hidden">Hides other rows</span>
                  <span className="hidden sm:inline">Their account hides other rows</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {people.map((user) => {
                const name = user.display_name || user.username;
                return (
                  <tr key={user.id}>
                    <td className="px-3 py-2.5 align-middle">
                      <div className="flex min-w-0 items-center gap-2">
                        <UserAvatar name={user.username} size="sm" />
                        <div className="min-w-0">
                          <p className="truncate font-medium" title={name}>
                            {name} <span className="text-xs font-normal text-muted-foreground">{user.user_type}</span>
                          </p>
                          <p className="text-xs text-muted-foreground sm:hidden">{where}</p>
                        </div>
                      </div>
                    </td>
                    <td className="hidden px-3 py-2.5 align-middle sm:table-cell">{where}</td>
                    <td className="px-3 py-2.5 align-middle">
                      <Hides
                        answer={hidesAnswer(
                          accounts.find((account) => account.user_id === user.id),
                          privacy,
                        )}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        A person only sees the copies in libraries their share opens.
        {privacy === "error" && " Couldn't read each account's hide rules from plex.tv just now."}
      </p>
      <p className="flex items-start gap-2 text-sm text-muted-foreground">
        <Eye aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
        You see every person&rsquo;s copy on your own Home. Plex can&rsquo;t
        restrict the owner, so your view isn&rsquo;t what they see.
      </p>
    </div>
  );
}
