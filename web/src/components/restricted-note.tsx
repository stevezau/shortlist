import { ShieldAlert } from "lucide-react";

/**
 * Explains why a restricted (parental-controlled) account doesn't get a row. Shown on their user
 * detail page so the admin understands the limitation and knows the fix (remove the age restriction
 * in Plex if they want this person to get recommendations).
 */
export function RestrictedNote({ className }: { className?: string }) {
  return (
    <div
      className={`flex gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm ${className ?? ""}`}
    >
      <ShieldAlert
        className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
        aria-hidden="true"
      />
      <div className="space-y-1">
        <p className="font-medium">
          This account has Plex parental controls — no row is built for them.
        </p>
        <p className="text-muted-foreground">
          Plex hides all collections from accounts with age restrictions,
          including any row Shortlist would create. Building one they can never
          see would waste a run slot and show as an empty row in their library.
          To give this person recommendations, remove the age restriction in
          Plex&rsquo;s user settings — their row will be built on the next run
          automatically.
        </p>
      </div>
    </div>
  );
}
