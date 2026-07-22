import { Info } from "lucide-react";

/**
 * The one caveat that comes with the owner having a row of their own: Plex cannot hide a row from
 * the account that owns the server, so the admin sees everyone's rows, not just theirs. Shortlist
 * cannot fix this — it's a Plex limitation — so the honest move is to say it plainly and point at
 * the workaround (watch on a Plex Home user, keep the admin account for administration).
 *
 * Shown on the Users list and on the owner's own page, so it's never more than one click from the
 * switch it's explaining.
 */
export function OwnerNote({ className }: { className?: string }) {
  return (
    <div
      className={`flex gap-3 rounded-lg border bg-muted/40 p-4 text-sm ${className ?? ""}`}
    >
      <Info
        className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
        aria-hidden="true"
      />
      <div className="space-y-1">
        <p className="font-medium">
          You&rsquo;re on this list too &mdash; but Plex can&rsquo;t hide rows
          from the server owner.
        </p>
        <p className="text-muted-foreground">
          Turn yourself on and you get a Picked-for-You row like anyone else.
          What Shortlist can&rsquo;t do is hide <em>other</em> people&rsquo;s
          rows from you: the admin account sees every row on the server. If you
          share this server with others and want a Home screen with only your
          own row, watch on a Plex Home user and keep the admin account for
          administration.
        </p>
      </div>
    </div>
  );
}
