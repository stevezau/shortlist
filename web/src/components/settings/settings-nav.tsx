import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";

import { SETTINGS_SECTIONS } from "@/components/settings/sections";
import { cn } from "@/lib/utils";

/**
 * The Settings section list, nested under "Settings" in the MAIN sidebar. Shown only while the
 * Settings page is open, so the page itself is a single full-width column (no middle rail eating
 * horizontal space). Jumps use native `#id` anchors — always valid because the sub-nav only renders
 * on `/settings`, where those anchors exist — and the active item tracks the section in view via
 * IntersectionObserver (progressive enhancement; degrades to "first section" where unavailable, e.g. jsdom).
 */
export function SettingsSubNav() {
  const { pathname } = useLocation();
  const onSettings =
    pathname === "/settings" || pathname.startsWith("/settings/");
  const [active, setActive] = useState(SETTINGS_SECTIONS[0]?.id ?? "");

  useEffect(() => {
    if (!onSettings || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        const inView = entries
          .filter((e) => e.isIntersecting)
          .sort(
            (a, b) => a.boundingClientRect.top - b.boundingClientRect.top,
          )[0];
        if (inView) setActive(inView.target.id);
      },
      { rootMargin: "-15% 0px -75% 0px" },
    );
    const seen = SETTINGS_SECTIONS.map((s) =>
      document.getElementById(s.id),
    ).filter((el): el is HTMLElement => el !== null);
    seen.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [onSettings]);

  if (!onSettings) return null;

  return (
    <div className="ml-4 mt-1 hidden border-l border-border/60 pl-2 md:block">
      {SETTINGS_SECTIONS.map(({ id, label, icon: Icon }) => {
        const current = active === id;
        return (
          <a
            key={id}
            href={`#${id}`}
            aria-current={current ? "true" : undefined}
            className={cn(
              "flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm transition-colors",
              current
                ? "font-medium text-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            {label}
          </a>
        );
      })}
    </div>
  );
}
