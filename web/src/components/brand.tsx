import { Sparkles } from "lucide-react";
import { Link } from "react-router";

import { cn } from "@/lib/utils";

const SIZES = {
  sm: { tile: "h-7 w-7 rounded-md", icon: "h-3.5 w-3.5", text: "text-base" },
  md: { tile: "h-9 w-9 rounded-lg", icon: "h-5 w-5", text: "text-lg" },
  lg: { tile: "h-12 w-12 rounded-xl", icon: "h-6 w-6", text: "text-2xl" },
} as const;

/** The Shortlist mark: a gold gradient tile with a sparkle. A real logo, not a bare emoji. */
export function Logo({
  size = "md",
  className,
}: {
  size?: keyof typeof SIZES;
  className?: string;
}) {
  const s = SIZES[size];
  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-grid place-items-center bg-gradient-to-br from-primary to-plex text-primary-foreground shadow-glow",
        s.tile,
        className,
      )}
    >
      <Sparkles className={s.icon} strokeWidth={2.25} />
    </span>
  );
}

/** The mark plus the wordmark — the app's identity lockup.
 *
 *  Not a link itself: the login and setup screens show it while signed out, where a link to the
 *  dashboard would go nowhere useful. `app-shell` wraps it in one via {@link HomeWordmark}. */
export function Wordmark({
  size = "md",
  className,
}: {
  size?: keyof typeof SIZES;
  className?: string;
}) {
  const s = SIZES[size];
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <Logo size={size} />
      <span className={cn("font-semibold tracking-tight", s.text)}>
        Shortlist
      </span>
    </span>
  );
}

/** The wordmark as a link to the dashboard — the convention everywhere else on the web, so people
 *  try it. Used in all three chrome slots (mobile bar, drawer, desktop rail); inside the drawer it
 *  needs no close handler, since the drawer already closes on any `<a>` tapped within it.
 *
 *  Kept distinct from {@link Wordmark} because the login and setup screens render the plain mark
 *  while signed out, where a dashboard link would go nowhere useful. */
export function HomeWordmark({ size }: { size?: keyof typeof SIZES }) {
  return (
    <Link
      to="/"
      aria-label="Shortlist — go to dashboard"
      className="rounded-md transition-opacity hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <Wordmark size={size} />
    </Link>
  );
}
