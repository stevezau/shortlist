import { useLocation } from "react-router";

import {
  activeSectionId,
  SETTINGS_SECTIONS,
} from "@/components/settings/sections";
import { cn } from "@/lib/utils";

/**
 * The Settings section list, nested under "Settings" in the MAIN sidebar. Shown only while the
 * Settings page is open, so the page itself is a single full-width column (no middle rail eating
 * horizontal space). Each entry SWITCHES the page to that one section rather than scrolling a long
 * stack — the stack made every section's own sub-headings ("Title sources", "AI enhancement") read
 * as siblings of the section above them, so "what belongs to Finding titles?" had no visible answer.
 * The `#id` anchors are kept as the selector, so existing deep links like `/settings#connections`
 * still land on the right pane.
 */
export function SettingsSubNav() {
  const { pathname, hash } = useLocation();
  const onSettings =
    pathname === "/settings" || pathname.startsWith("/settings/");
  const active = activeSectionId(hash);

  if (!onSettings) return null;

  return (
    <div className="ml-4 mt-1 hidden border-l border-border/60 pl-2 md:block">
      {SETTINGS_SECTIONS.map(({ id, label, icon: Icon, group }, i) => {
        const current = active === id;
        // Emit a small group heading whenever the group changes, so the flat list reads as clusters.
        const startsGroup = SETTINGS_SECTIONS[i - 1]?.group !== group;
        return (
          <div key={id}>
            {startsGroup && (
              <p
                className={cn(
                  "px-2.5 pb-1 text-[0.7rem] font-semibold uppercase tracking-wide text-muted-foreground/70",
                  i > 0 && "pt-3",
                )}
              >
                {group}
              </p>
            )}
            <a
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
          </div>
        );
      })}
    </div>
  );
}
