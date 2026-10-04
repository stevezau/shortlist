import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  BookOpen,
  CircleAlert,
  CircleCheck,
  Coffee,
  Gauge,
  Inbox,
  LifeBuoy,
  ListChecks,
  Loader2,
  LogOut,
  Rows3,
  Settings as SettingsIcon,
  ShieldCheck,
  Star,
  Users as UsersIcon,
  type LucideIcon,
} from "lucide-react";
import { Suspense } from "react";
import { NavLink, Outlet, useNavigate } from "react-router";

import { HomeWordmark } from "@/components/brand";
import { ActivityPill } from "@/components/layout/activity-pill";
import { ActivityIndicator } from "@/components/layout/activity-indicator";
import { MobileNavigation } from "@/components/layout/mobile-navigation";
import { NotificationBell } from "@/components/layout/notification-bell";
import { WhatsNewDialog } from "@/components/layout/whats-new-dialog";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { buildLabel, settingBool } from "@/lib/format";
import { privacyNeedsAttention, usePrivacyGlance } from "@/lib/privacy-attention";
import { useCollections, useSession, useSettings, useUsers, useVersion } from "@/lib/queries";
import { clearCachedReports } from "@/lib/report-cache";
import { selectedVerticalClass } from "@/lib/selected";
import { COFFEE_URL, DOCS_URL, STAR_URL } from "@/lib/support";
import { Toaster } from "sonner";

import { cn } from "@/lib/utils";

type NavKey = "dashboard" | "rows" | "users" | "privacy" | "runs" | "requests" | "activity" | "settings";

const NAV_ITEMS: { key: NavKey; to: string; label: string; icon: LucideIcon; end: boolean }[] = [
  { key: "dashboard", to: "/", label: "Dashboard", icon: Gauge, end: true },
  { key: "rows", to: "/rows", label: "Rows", icon: Rows3, end: false },
  { key: "users", to: "/users", label: "Users", icon: UsersIcon, end: false },
  { key: "privacy", to: "/privacy", label: "Privacy", icon: ShieldCheck, end: false },
  { key: "runs", to: "/runs", label: "Runs", icon: ListChecks, end: false },
  { key: "requests", to: "/requests", label: "Requests", icon: Inbox, end: false },
  { key: "activity", to: "/activity", label: "Activity", icon: Activity, end: false },
  { key: "settings", to: "/settings", label: "Settings", icon: SettingsIcon, end: false },
];

/** One row of the rail. Shared by the main items and the bottom block, so both read as one list. */
const navLinkClass =
  "flex items-center gap-2.5 rounded-lg border border-transparent px-3 py-2 text-sm font-medium transition-colors";
const navLinkIdleClass = "text-muted-foreground hover:bg-elevated hover:text-foreground";

/**
 * What the rail says beside its items, from answers the app already has.
 *
 * The Rows and Users counts fetch on first render (each shares its page's cache entry), so the
 * numbers are there before anyone opens either page. The Users list used to stay cache-only because
 * `/api/users` ran two queries per person; it is now one grouped query per metric, so it is cheap
 * enough to load with the app. The privacy status and settings are fetched too (the
 * privacy one is held for five minutes — see `usePrivacyGlance`), because a warning that only appears after visiting the page it warns about is
 * no warning.
 */
function useNavBadges() {
  const rows = useCollections();
  const users = useUsers();
  const privacy = usePrivacyGlance();
  const settings = useSettings();
  return {
    rowsCount: rows.data?.length,
    usersCount: users.data?.length,
    privacyAttention: privacyNeedsAttention(privacy.data),
    // Only once the answer is in: a dimmed item that brightens a second later reads as a glitch.
    requestsOff: settings.data !== undefined && !settingBool(settings.data, "requests.enabled"),
  };
}

/** Help, and one door for everything that goes wrong.
 *
 *  "Report a bug" and "Copy diagnostics" used to sit here as two separate actions, which asked the
 *  person to know that a bug report wants diagnostics attached and that the copy button is where
 *  they come from. Both now live on the "Have an issue?" page, along with the checks that answer
 *  most reports before they are filed. */
export function HelpLinks() {
  return (
    <>
      <a
        href={DOCS_URL}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(navLinkClass, navLinkIdleClass)}
      >
        <BookOpen className="h-4 w-4 shrink-0" aria-hidden="true" />
        Help &amp; docs
      </a>
      <NavLink
        to="/issue"
        className={({ isActive }) =>
          cn(navLinkClass, isActive ? selectedVerticalClass : navLinkIdleClass)
        }
      >
        <LifeBuoy className="h-4 w-4 shrink-0" aria-hidden="true" />
        Have an issue?
      </NavLink>
    </>
  );
}

/** Signed-in owner, a sign-out button, and the build, at the foot of the rail. */
function SessionFooter() {
  const session = useSession();
  const version = useVersion();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      clearCachedReports();
      queryClient.clear(); // drop every cached query so no stale owner data lingers
      navigate("/login");
    },
  });

  return (
    <div className="space-y-1.5 px-3 pb-4 pt-2">
      {session.data?.username && (
        <p className="truncate px-1 text-xs text-muted-foreground">
          Signed in as{" "}
          <span className="font-medium text-foreground">
            {session.data.username}
          </span>
        </p>
      )}
      <Button
        variant="ghost"
        size="sm"
        className="w-full justify-start text-muted-foreground hover:text-foreground"
        onClick={() => logout.mutate()}
        loading={logout.isPending}
      >
        {!logout.isPending && <LogOut aria-hidden="true" />}
        Sign out
      </Button>
      {/* The full commit on hover — the short one fits the sidebar, a bug report wants all of it. */}
      <p
        className="px-1 text-xs break-all text-muted-foreground"
        title={version.data?.git_sha || undefined}
      >
        {buildLabel(version.data)}
      </p>
    </div>
  );
}

/** A star and a coffee, visible in the rail's bottom block beside Help — never behind an About menu
 *  (owner decision, design refresh 2026-10-03). Only the icons take colour: enough to be seen,
 *  while the words stay as quiet as their neighbours. Still not among the main nav items, and never
 *  floating over the page: people self-host to get away from being sold to, and a donate prompt
 *  that follows them around costs more goodwill than it raises. Exported for its test. */
export function SupportLinks() {
  return (
    <>
      <a
        href={STAR_URL}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(navLinkClass, navLinkIdleClass)}
      >
        <Star className="h-4 w-4 shrink-0 fill-primary text-primary" aria-hidden="true" />
        Star on GitHub
      </a>
      <a
        href={COFFEE_URL}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(navLinkClass, navLinkIdleClass)}
      >
        <Coffee className="h-4 w-4 shrink-0 text-support" aria-hidden="true" />
        Buy me a coffee
      </a>
    </>
  );
}

/** The nav body — links, the live activity pill, and the bottom block. Shared by the desktop rail
 *  and the mobile slide-out drawer, so both always show exactly the same navigation. Exported for
 *  the drawer's test. */
export function NavBody() {
  const badges = useNavBadges();
  const counts: Partial<Record<NavKey, number | undefined>> = {
    rows: badges.rowsCount,
    users: badges.usersCount,
  };

  return (
    <>
      <nav
        aria-label="Main"
        className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-3 pb-3"
      >
        {NAV_ITEMS.map(({ key, to, label, icon: Icon, end }) => {
          const count = counts[key];
          const dimmed = key === "requests" && badges.requestsOff;
          const attention = key === "privacy" && badges.privacyAttention;
          // The dot and the dimming are visual, so the name carries them. A label rather than
          // sr-only text: accessible-name engines disagree on the space before a nested span.
          const announced = attention ? `${label} (needs attention)` : dimmed ? `${label} (off)` : undefined;
          return (
            <div key={to}>
              <NavLink
                to={to}
                end={end}
                aria-label={announced}
                title={dimmed ? "Requests are off. Turn them on in Settings → Requests." : undefined}
                className={({ isActive }) =>
                  cn(
                    navLinkClass,
                    isActive ? selectedVerticalClass : navLinkIdleClass,
                    // Faint, not hidden and not disabled: the page explains how to turn requests on.
                    dimmed && !isActive && "text-faint-foreground",
                  )
                }
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                {label}
                {attention && (
                  <span aria-hidden="true" className="ml-auto size-2 shrink-0 rounded-full bg-warning" />
                )}
                {count !== undefined && (
                  <span aria-hidden="true" className="ml-auto text-xs font-normal text-muted-foreground">
                    {count}
                  </span>
                )}
              </NavLink>
            </div>
          );
        })}
      </nav>
      <ActivityPill />
      <div className="mt-auto flex flex-col gap-0.5 border-t px-3 pt-3">
        <HelpLinks />
        <SupportLinks />
      </div>
      <SessionFooter />
    </>
  );
}

export function AppShell() {
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      {/* One Toaster for the whole app — background work announces itself from the header's
          ActivityIndicator, which is the single observer of the job queue.

          Themed to the app's own tokens rather than left on sonner's defaults, which render a WHITE
          card on a dark-only app. `richColors` is deliberately off: it paints success/error in
          sonner's palette, which does not match ours. */}
      <Toaster
        position="bottom-right"
        closeButton
        theme="dark"
        gap={8}
        toastOptions={{
          classNames: {
            toast:
              "!bg-elevated !border-border !text-foreground !rounded-lg !shadow-xl !gap-3 !px-4 !py-3 !text-sm",
            title: "!text-sm !font-medium !leading-tight",
            description: "!text-xs !text-muted-foreground !leading-snug",
            icon: "!m-0 !self-start !mt-0.5",
            closeButton:
              "!bg-elevated !border-border !text-muted-foreground hover:!text-foreground",
          },
        }}
        icons={{
          // The app's own spinner, so a running toast matches every other "in flight" indicator
          // instead of introducing a second visual language for the same idea.
          loading: (
            <Loader2
              className="h-4 w-4 animate-spin text-primary"
              aria-hidden
            />
          ),
          success: <CircleCheck className="h-4 w-4 text-success" aria-hidden />,
          error: (
            <CircleAlert
              className="h-4 w-4 text-destructive-text"
              aria-hidden
            />
          ),
        }}
      />
      <WhatsNewDialog />
      {/* Mobile top bar: wordmark + hamburger. Hidden once the sidebar appears at md. */}
      <header className="sticky top-0 z-30 flex items-center justify-between border-b bg-card/80 px-4 py-3 backdrop-blur md:hidden">
        <HomeWordmark />
        <div className="flex items-center gap-1">
          <ActivityIndicator align="right" />
          <NotificationBell align="right" />
          <MobileNavigation><NavBody /></MobileNavigation>
        </div>
      </header>

      {/* Desktop sidebar. Hidden on mobile (the drawer replaces it). `z-30` matters: the sidebar's
          `backdrop-blur` opens its own stacking context, and the notification panel (w-80) overflows
          the w-60 rail into <main>. Without a z-index here, <main> — a later sibling — paints its text
          OVER that overflow (the "text shows behind the panel" bug). Elevating the whole rail fixes it. */}
      <aside className="sticky top-0 z-30 hidden h-screen w-60 shrink-0 flex-col border-r bg-card/40 backdrop-blur md:flex">
        <div className="flex items-center justify-between px-5 py-5">
          <HomeWordmark />
          <ActivityIndicator align="left" />
          <NotificationBell align="left" />
        </div>
        <NavBody />
      </aside>

      {/* `min-w-0` is load-bearing: a flex child defaults to `min-width:auto`, so without it `main`
          can never shrink below its widest unbreakable child and the WHOLE PAGE gains a horizontal
          scrollbar. One `whitespace-nowrap` button ("Run all rows now") did exactly that. */}
      <main className="min-w-0 flex-1 px-4 py-6 md:px-8 md:py-8">
        {/* Fill the width next to the left nav — dense pages (Runs, Requests, Users) were wasting half
            the screen at max-w-6xl. A high cap keeps line lengths sane on an ultrawide without floating
            a narrow block in the middle. Individual pages that want to stay narrow cap their own content. */}
        <div className="mx-auto max-w-[1800px] motion-safe:animate-fade-in">
          {/* Pages load on first visit (App.tsx), and this boundary keeps the rail on screen while one
              does — the app-level fallback would blank the whole shell. */}
          <Suspense fallback={<Skeleton className="h-96 w-full" />}>
            <Outlet />
          </Suspense>
        </div>
      </main>
    </div>
  );
}
