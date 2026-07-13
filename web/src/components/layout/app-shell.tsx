import {
  Gauge,
  ListChecks,
  Rows3,
  Settings as SettingsIcon,
  Users as UsersIcon,
} from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";

import { Wordmark } from "@/components/brand";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: Gauge, end: true },
  { to: "/rows", label: "Rows", icon: Rows3, end: false },
  { to: "/users", label: "Users", icon: UsersIcon, end: false },
  { to: "/runs", label: "Runs", icon: ListChecks, end: false },
  { to: "/settings", label: "Settings", icon: SettingsIcon, end: false },
];

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
        <div className="hidden px-5 py-4 text-xs text-muted-foreground md:block">
          Rowarr · beta
        </div>
      </aside>
      <main className="flex-1 px-4 py-6 md:px-8 md:py-8">
        <div className="mx-auto max-w-5xl animate-fade-in">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
