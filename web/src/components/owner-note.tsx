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
          You&rsquo;re on this list too &mdash; and your Home screen shows only
          your own row.
        </p>
        <p className="text-muted-foreground">
          Turn yourself on and you get a Picked-for-You row like anyone else.
          Your Home screen is safe: Plex tracks &ldquo;on the owner&rsquo;s
          Home&rdquo; separately from &ldquo;on a friend&rsquo;s Home&rdquo;, so
          nobody else&rsquo;s row lands there.
        </p>
        <p className="text-muted-foreground">
          Where you <em>do</em> see everyone&rsquo;s rows is the{" "}
          <strong>Collections tab</strong>, and the{" "}
          <strong>Recommended shelf</strong> if you leave{" "}
          <em>Everyone else &rarr; Recommended shelf</em> on for a row. You own
          the server, so there is no share filter to hide them behind &mdash;
          turn that toggle off to clear the shelf.
        </p>
      </div>
    </div>
  );
}
