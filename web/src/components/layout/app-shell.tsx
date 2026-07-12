import {
  Gauge,
  ListChecks,
  Settings as SettingsIcon,
  Users as UsersIcon,
} from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";

import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: Gauge, end: true },
  { to: "/users", label: "Users", icon: UsersIcon, end: false },
  { to: "/runs", label: "Runs", icon: ListChecks, end: false },
  { to: "/settings", label: "Settings", icon: SettingsIcon, end: false },
];

export function AppShell() {
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="border-b md:sticky md:top-0 md:h-screen md:w-56 md:shrink-0 md:border-b-0 md:border-r">
        <div className="flex items-center gap-2 px-4 py-4">
          <span aria-hidden="true" className="text-lg">
            ✨
          </span>
          <span className="text-lg font-semibold tracking-tight text-primary">
            Rowarr
          </span>
        </div>
        <nav
          aria-label="Main"
          className="flex gap-1 px-2 pb-2 md:flex-col md:pb-0"
        >
          {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )
              }
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="flex-1 px-4 py-6 md:px-8">
        <Outlet />
      </main>
    </div>
  );
}
