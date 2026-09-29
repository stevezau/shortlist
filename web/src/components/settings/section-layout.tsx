import { useEffect, type ReactNode } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

import { useActiveSettingsSection } from "@/components/settings/active-section";
import { SETTINGS_SECTIONS } from "@/components/settings/sections";

/** One continuous page keeps every form and unfinished connection edit mounted. */
export function SettingsSections({ content }: { content: Record<string, ReactNode> }) {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const active = useActiveSettingsSection();
  const { hash, key } = useLocation();
  useEffect(() => {
    if (!hash) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    // The same anchor may be clicked again after manual scrolling. Each navigation has a new
    // location key even when its hash is unchanged; keep drafts mounted and repeat the jump.
    document.getElementById(hash.slice(1))?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }, [hash, key]);

  return (
    <div className="mt-6 min-w-0">
      <div className="sticky top-14 z-20 -mx-1 mb-6 border-b bg-background/95 px-1 py-3 backdrop-blur-sm md:hidden">
        <select
          aria-label="Settings section"
          value={active}
          onChange={(event) => void navigate({ search: params.toString(), hash: event.target.value })}
          className="h-10 w-full rounded-md border border-primary/30 bg-primary/5 px-3 text-sm text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {SETTINGS_SECTIONS.map((section) => <option key={section.id} value={section.id}>{section.label}</option>)}
        </select>
      </div>
      {SETTINGS_SECTIONS.map(({ id }) => (
        <section key={id} id={id}
          className="mb-10 scroll-mt-36 border-b border-border/60 pb-10 last:min-h-[calc(100svh-8rem)] last:border-0 md:scroll-mt-8 [&_h2]:text-xl [&_h2]:tracking-tight">
          {content[id]}
        </section>
      ))}
    </div>
  );
}
