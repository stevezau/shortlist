import { Link } from "react-router";

import { cn } from "@/lib/utils";

const SIZES = {
  sm: { tile: "h-7 w-7", text: "text-base" },
  md: { tile: "h-9 w-9", text: "text-lg" },
  lg: { tile: "h-12 w-12", text: "text-2xl" },
} as const;

/** The Shortlist mark: a Plex-gold tile with two drawn sparkles. The same drawing as the website's
 *  logo (docs/assets/img/logo.svg) and the browser-tab icon, so the app, the site and the tab read as
 *  one product. Flat on purpose: no gradient, no glow. */
export function Logo({
  size = "md",
  className,
}: {
  size?: keyof typeof SIZES;
  className?: string;
}) {
  return (
    <svg viewBox="0 0 64 64" aria-hidden="true" className={cn("shrink-0", SIZES[size].tile, className)}>
      <rect width="64" height="64" rx="15" className="fill-plex" />
      <path
        className="fill-plex-foreground"
        d="M36.5 13.5c.5 0 .9.33 1.03.82l1.6 6.05a8.6 8.6 0 0 0 6.1 6.1l6.05 1.6c.49.13.82.53.82 1.03s-.33.9-.82 1.03l-6.05 1.6a8.6 8.6 0 0 0-6.1 6.1l-1.6 6.05a1.07 1.07 0 0 1-2.06 0l-1.6-6.05a8.6 8.6 0 0 0-6.1-6.1l-6.05-1.6a1.07 1.07 0 0 1 0-2.06l6.05-1.6a8.6 8.6 0 0 0 6.1-6.1l1.6-6.05c.13-.49.53-.82 1.03-.82Z"
      />
      <path
        className="fill-plex-foreground"
        d="M20 36.5c.44 0 .82.29.94.71l.86 3.02a4.6 4.6 0 0 0 3.17 3.17l3.02.86a.98.98 0 0 1 0 1.88l-3.02.86a4.6 4.6 0 0 0-3.17 3.17l-.86 3.02a.98.98 0 0 1-1.88 0l-.86-3.02a4.6 4.6 0 0 0-3.17-3.17l-3.02-.86a.98.98 0 0 1 0-1.88l3.02-.86a4.6 4.6 0 0 0 3.17-3.17l.86-3.02c.12-.42.5-.71.94-.71Z"
      />
    </svg>
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
