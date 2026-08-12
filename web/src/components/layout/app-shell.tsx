import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CircleAlert,
  CircleCheck,
  Loader2,
  BookOpen,
  Gauge,
  Inbox,
  LifeBuoy,
  ListChecks,
  ScrollText,
  LogOut,
  Menu,
  Rows3,
  Settings as SettingsIcon,
  Users as UsersIcon,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router";

import { HomeWordmark } from "@/components/brand";
import { ActivityPill } from "@/components/layout/activity-pill";
import { ActivityIndicator } from "@/components/layout/activity-indicator";
import { NotificationBell } from "@/components/layout/notification-bell";
import { SettingsSubNav } from "@/components/settings/settings-nav";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { buildLabel } from "@/lib/format";
import { useSession, useVersion } from "@/lib/queries";
import { GITHUB_REPO } from "@/lib/support";
import { Toaster } from "sonner";

import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: Gauge, end: true },
  { to: "/rows", label: "Rows", icon: Rows3, end: false },
  { to: "/users", label: "Users", icon: UsersIcon, end: false },
  { to: "/runs", label: "Runs", icon: ListChecks, end: false },
  { to: "/logs", label: "Logs", icon: ScrollText, end: false },
  { to: "/requests", label: "Requests", icon: Inbox, end: false },
  { to: "/jobs", label: "Jobs", icon: Wrench, end: false },
  { to: "/settings", label: "Settings", icon: SettingsIcon, end: false },
];

/** Help, and one door for everything that goes wrong.
 *
 *  "Report a bug" and "Copy diagnostics" used to sit here as two separate actions, which asked the
 *  person to know that a bug report wants diagnostics attached and that the copy button is where
 *  they come from. Both now live on the "Have an issue?" page, along with the checks that answer
 *  most reports before they are filed. */
export function HelpLinks() {
  const linkClass =
    "flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground";

  return (
    <div className="space-y-1 px-3">
      <a
        href={`${GITHUB_REPO}#readme`}
        target="_blank"
        rel="noopener noreferrer"
        className={linkClass}
      >
        <BookOpen className="h-4 w-4 shrink-0" aria-hidden="true" />
        Help &amp; docs
      </a>
      <NavLink
        to="/issue"
        className={({ isActive }) =>
          cn(linkClass, isActive && "bg-accent text-accent-foreground")
        }
      >
        <LifeBuoy className="h-4 w-4 shrink-0" aria-hidden="true" />
        Have an issue?
      </NavLink>
    </div>
  );
}

/** Signed-in owner + a sign-out button, pinned to the bottom of the nav. */
function SessionFooter() {
  const session = useSession();
  const version = useVersion();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      queryClient.clear(); // drop every cached query so no stale owner data lingers
      navigate("/login");
    },
  });

  return (
    <div className="mt-auto space-y-2 border-t px-3 py-4">
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
      {/* The full commit on hover — the short one fits the sidebar, but a bug report wants all of it. */}
      <p
        className="px-1 text-xs break-all text-muted-foreground"
        title={version.data?.git_sha || undefined}
      >
        {buildLabel(version.data)}
      </p>
    </div>
  );
}

/** The nav body — links, the live activity pill, and the session footer. Shared by the desktop
 *  sidebar and the mobile slide-out drawer, so both always show exactly the same navigation. */
function NavBody() {
  return (
    <>
      <nav
        aria-label="Main"
        className="flex flex-1 flex-col gap-1 overflow-y-auto px-3 pb-3"
      >
        {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
          <div key={to}>
            <NavLink
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "group relative flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )
              }
            >
              {({ isActive }) => (
                <>
                  {/* Left accent bar on the active item — a clearer "you are here" than colour alone. */}
                  <span
                    aria-hidden="true"
                    className={cn(
                      "absolute left-0 top-1/2 h-5 -translate-y-1/2 rounded-r-full bg-primary transition-all",
                      isActive ? "w-1 opacity-100" : "w-0 opacity-0",
                    )}
                  />
                  <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                  {label}
                </>
              )}
            </NavLink>
            {/* Settings' sections nest here, so the page needs no middle rail. Shown only on /settings. */}
            {to === "/settings" && <SettingsSubNav />}
          </div>
        ))}
      </nav>
      <ActivityPill />
      <HelpLinks />
      <SessionFooter />
    </>
  );
}

export function AppShell() {
  const [menuOpen, setMenuOpen] = useState(false);

  // Close the drawer on Escape, and lock body scroll behind it — a phone shouldn't scroll the page
  // under the open menu.
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [menuOpen]);

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
      {/* Mobile top bar: wordmark + hamburger. Hidden once the sidebar appears at md. */}
      <header className="sticky top-0 z-30 flex items-center justify-between border-b bg-card/80 px-4 py-3 backdrop-blur md:hidden">
        <HomeWordmark />
        <div className="flex items-center gap-1">
          <ActivityIndicator align="right" />
          <NotificationBell align="right" />
          <Button
            variant="ghost"
            size="icon"
            aria-label="Open menu"
            aria-expanded={menuOpen}
            aria-controls="mobile-nav"
            onClick={() => setMenuOpen(true)}
          >
            <Menu aria-hidden="true" />
          </Button>
        </div>
      </header>

      {/* Mobile slide-out drawer. A backdrop + a left panel; any link tap, the backdrop, Escape, or
          the close button dismisses it. */}
      {menuOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 bg-black/50 motion-safe:animate-fade-in"
            onClick={() => setMenuOpen(false)}
          />
          <aside
            id="mobile-nav"
            role="dialog"
            aria-modal="true"
            aria-label="Main menu"
            className="absolute inset-y-0 left-0 flex w-72 max-w-[85%] flex-col bg-card shadow-xl motion-safe:animate-slide-in-left"
            // Delegate: any link tapped inside the drawer closes it, main nav and Settings sections alike.
            onClick={(event) => {
              if ((event.target as HTMLElement).closest("a"))
                setMenuOpen(false);
            }}
          >
            <div className="flex items-center justify-between border-b px-4 py-3">
              <HomeWordmark />
              <Button
                variant="ghost"
                size="icon"
                aria-label="Close menu"
                autoFocus
                onClick={() => setMenuOpen(false)}
              >
                <X aria-hidden="true" />
              </Button>
            </div>
            <NavBody />
          </aside>
        </div>
      )}

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
        <div className="mx-auto max-w-[1800px] animate-fade-in">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
