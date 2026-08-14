import { TriangleAlert, X } from "lucide-react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { useDismissNotification, useNotifications } from "@/lib/queries";

/** The bell notification's id. Dismissing THAT clears the alert, nothing more. */
export const OWNER_SHELF_ALERT_ID = "owner-sees-all-rows";

/** This note's own id, deliberately separate from the bell's.
 *
 *  They were one id at first, so that acknowledging the fact anywhere silenced it everywhere. That
 *  was wrong: clearing a bell alert is a light "yep, seen it", while retiring an inline explainer is
 *  a deliberate choice you make once. Coupling them meant one casual bell click permanently deleted
 *  the explanation from the Users page, with no way to bring it back — which is exactly what
 *  happened on the maintainer's own server within an hour of shipping. */
export const OWNER_SHELF_NOTE_ID = "owner-sees-all-rows-note";

/**
 * The one caveat that comes with owning the server: Plex cannot hide anyone's row from the admin
 * account, so you see everybody's. Shortlist cannot fix it — it is a Plex limitation — so the honest
 * move is to lead with the notice, say exactly where it shows, why, and what to do about it.
 *
 * Ordered notice → where → why → recommendation, NOT reassurance-first. An earlier version opened
 * with "your Home screen shows only your own row", which is true, load-bearing, and completely
 * buried the thing an owner needs to know; it also offered "turn that toggle off" as though it were
 * the only option.
 *
 * **This note owns its own dismissal.** Clearing the bell alert does NOT retire it, because those
 * are different gestures — see `OWNER_SHELF_NOTE_ID`. The guide's "Leave it — I don't mind seeing
 * them" DOES retire both, since that one is a decision about the fact itself rather than about an
 * alert. It stays reachable afterwards either way: the row editor's placement grid still links to
 * the guide, and /watching-account always renders.
 *
 * Shown on the Users list and on the owner's own page, so it is never more than one click from the
 * switch it is explaining.
 */
export function OwnerNote({ className }: { className?: string }) {
  const notifications = useNotifications();
  const dismiss = useDismissNotification();

  // Hidden only once the server CONFIRMS the dismissal. Hiding on click would be quicker but would
  // also hide it when the write failed, leaving the owner believing they had silenced something that
  // will be back next reload.
  if (notifications.data?.dismissed?.includes(OWNER_SHELF_NOTE_ID)) return null;

  return (
    <div
      className={`flex gap-3 rounded-lg border border-warning/40 bg-warning/5 p-4 text-sm ${className ?? ""}`}
    >
      <TriangleAlert
        className="mt-0.5 h-4 w-4 shrink-0 text-warning"
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1 space-y-2">
        <p className="font-medium">
          Watching on this admin account? You&rsquo;ll see everyone else&rsquo;s
          rows, not just yours.
        </p>

        <p className="text-muted-foreground">
          <strong className="text-foreground">Where:</strong> each
          library&rsquo;s <strong>Collections</strong> tab, and its{" "}
          <strong>Recommended</strong> shelf for any row you&rsquo;ve left{" "}
          <em>Everyone else &rarr; Recommended shelf</em> switched on for.{" "}
          <span className="text-foreground">Not your Home screen</span> &mdash;
          Plex keeps the owner&rsquo;s Home separate, so nobody else&rsquo;s row
          ever lands there.
        </p>

        <p className="text-muted-foreground">
          <strong className="text-foreground">Why:</strong> Shortlist hides each
          person&rsquo;s row from everyone else through the share they have with
          your server. You own it, so you have no share with yourself &mdash;
          there is nothing for Plex to hide them behind, and no Plex setting
          changes that.
        </p>

        <p className="text-muted-foreground">
          <strong className="text-foreground">What we suggest:</strong> do your
          watching on a separate Plex Home account and keep this one for
          administering the server. You add the account in Plex; Shortlist
          copies your watch history across so its picks are right from the first
          run. Prefer not to? You can take the rows off the library shelf
          instead, or simply live with it.
        </p>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pt-1">
          <Link
            to="/watching-account"
            className="inline-flex items-center gap-1 rounded-sm font-medium text-foreground underline underline-offset-4 hover:no-underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            See the options &rarr;
          </Link>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2 text-xs text-muted-foreground hover:text-foreground"
            disabled={dismiss.isPending}
            // Retires the BELL ALERT too, and the asymmetry is deliberate. "Got it — don't show this
            // again" is a considered gesture made while reading the full explanation; leaving the
            // same message sitting in the bell afterwards is just nagging someone who has already
            // said they understand. The other direction still does NOT hold: a bell click is a light
            // "seen it", and letting it delete this explainer is what wiped it off the maintainer's
            // own Users page within an hour of shipping (see OWNER_SHELF_NOTE_ID).
            //
            // Sequential, not parallel: the second write reads the dismissed list the first one
            // wrote, so firing them together loses one to a last-write-wins race.
            onClick={async () => {
              // `mutateAsync` REJECTS on failure, unlike `mutate` — so without this the first failed
              // write becomes an unhandled rejection, which vitest fails the whole run on even
              // though every test passes. The user-visible handling is already correct without it:
              // `dismiss.isError` drives the "couldn't save that" line below, and the note stays.
              //
              // Both attempted, and the second only after the first resolves: the write reads the
              // dismissed list the previous one wrote, so firing them together loses one to a
              // last-write-wins race.
              try {
                await dismiss.mutateAsync(OWNER_SHELF_NOTE_ID);
                await dismiss.mutateAsync(OWNER_SHELF_ALERT_ID);
              } catch {
                // Surfaced by `dismiss.isError`, not swallowed silently.
              }
            }}
          >
            <X className="h-3 w-3" aria-hidden="true" />
            Got it &mdash; don&rsquo;t show this again
          </Button>
        </div>

        {dismiss.isError && (
          <p className="text-xs text-destructive">
            Couldn&rsquo;t save that &mdash; the note will be back on the next
            reload. Try again.
          </p>
        )}
      </div>
    </div>
  );
}
