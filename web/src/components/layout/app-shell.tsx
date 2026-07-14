import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Gauge,
  Inbox,
  ListChecks,
  LogOut,
  Rows3,
  Settings as SettingsIcon,
  Users as UsersIcon,
} from "lucide-react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { Wordmark } from "@/components/brand";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useSession } from "@/lib/queries";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: Gauge, end: true },
  { to: "/rows", label: "Rows", icon: Rows3, end: false },
  { to: "/users", label: "Users", icon: UsersIcon, end: false },
  { to: "/runs", label: "Runs", icon: ListChecks, end: false },
  { to: "/requests", label: "Requests", icon: Inbox, end: false },
  { to: "/settings", label: "Settings", icon: SettingsIcon, end: false },
];

/** Signed-in owner + a sign-out button, pinned to the bottom of the sidebar. */
function SessionFooter() {
  const session = useSession();
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
    <div className="mt-auto space-y-2 px-3 py-4 md:border-t">
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
      <p className="px-1 text-xs text-muted-foreground">Shortlist · beta</p>
    </div>
  );
}

export function AppShell() {
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="border-b bg-card/40 backdrop-blur md:sticky md:top-0 md:flex md:h-screen md:w-60 md:shrink-0 md:flex-col md:border-b-0 md:border-r">
        <div className="px-4 py-4 md:px-5 md:py-5">
          <Wordmark />
        </div>
        <nav
          aria-label="Main"
          className="flex gap-1 px-2 pb-2 md:flex-1 md:flex-col md:px-3 md:pb-0"
        >
          {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
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
                  {/* Left accent bar on the active item — a clearer "you are here" than color alone. */}
                  <span
                    aria-hidden="true"
                    className={cn(
                      "absolute left-0 top-1/2 hidden h-5 -translate-y-1/2 rounded-r-full bg-primary transition-all md:block",
                      isActive ? "w-1 opacity-100" : "w-0 opacity-0",
                    )}
                  />
                  <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                  {label}
                </>
              )}
            </NavLink>
          ))}
        </nav>
        <SessionFooter />
      </aside>
      <main className="flex-1 px-4 py-6 md:px-8 md:py-8">
        <div className="mx-auto max-w-5xl animate-fade-in">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
