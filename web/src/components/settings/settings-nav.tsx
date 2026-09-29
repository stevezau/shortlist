import { useEffect, useRef } from "react";
import { Link, useLocation } from "react-router";

import { SETTINGS_SECTIONS } from "@/components/settings/sections";
import { useActiveSettingsSection } from "@/components/settings/active-section";
import { cn } from "@/lib/utils";

/** Settings section links live beneath Settings in the main sidebar and mobile drawer. */
export function SettingsSubNav() {
  const { pathname, search } = useLocation();
  const onSettings = pathname === "/settings";
  const active = useActiveSettingsSection();
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const current = navRef.current?.querySelector<HTMLElement>('[aria-current="true"]');
    const scroller = navRef.current?.parentElement?.parentElement;
    if (!current || !scroller) return;
    const item = current.getBoundingClientRect();
    const bounds = scroller.getBoundingClientRect();
    if (item.bottom > bounds.bottom) scroller.scrollTop += item.bottom - bounds.bottom + 8;
    else if (item.top < bounds.top) scroller.scrollTop -= bounds.top - item.top + 8;
  }, [active]);

  if (!onSettings) return null;

  return (
    <nav ref={navRef} aria-label="Settings sections" className="mb-2 ml-5 mt-2 border-l border-border/70 pl-2">
      {SETTINGS_SECTIONS.map(({ id, label, icon: Icon, group }, i) => {
        const current = active === id;
        // Emit a small group heading whenever the group changes, so the flat list reads as clusters.
        const startsGroup = SETTINGS_SECTIONS[i - 1]?.group !== group;
        return (
          <div key={id}>
            {startsGroup && (
              <p
                className={cn(
                  // Full muted-foreground: at /70 these 11px caps measured 4.32:1 on the rail, under AA.
                  "px-2 pb-1.5 text-[0.6rem] font-semibold uppercase tracking-wide text-muted-foreground",
                  i > 0 && "pt-3",
                )}
              >
                {group}
              </p>
            )}
            <Link
              to={{ pathname: "/settings", search, hash: `#${id}` }}
              aria-current={current ? "true" : undefined}
              className={cn(
                "flex items-center gap-2.5 rounded-md border border-transparent px-2 py-1.5 text-xs transition-colors",
                current
                  ? "border-primary/25 bg-primary/10 font-medium text-primary"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              {label}
            </Link>
          </div>
        );
      })}
    </nav>
  );
}
