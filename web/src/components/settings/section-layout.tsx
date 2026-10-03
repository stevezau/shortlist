import { type ReactNode, useEffect } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { useActiveSection } from "@/components/settings/active-section";
import {
  isSettingsTab,
  resolveSettingsAnchor,
  SETTINGS_TABS,
  type SettingsTab,
  settingsTabForHash,
} from "@/components/settings/sections";
import { TabPanel, Tabs } from "@/components/ui/tabs";
import { selectedClass, unselectedClass } from "@/lib/selected";
import { cn } from "@/lib/utils";

/** The tab the address names, or — for `/settings` and old `/settings#…` links — the one it means. */
function useSettingsTab(): SettingsTab {
  const { tab } = useParams();
  const { hash } = useLocation();
  return isSettingsTab(tab) ? tab : settingsTabForHash(hash);
}

/** A hand-typed `#%zz` must not take the page down; treat it as the literal text it is. */
function safeDecode(fragment: string): string {
  try {
    return decodeURIComponent(fragment);
  } catch {
    return fragment;
  }
}

/** Where an anchor lives: by name for the ones Settings knows, otherwise by finding the element in
 *  whichever tab panel holds it (a field deep inside a tab). */
function locate(id: string): { tab: SettingsTab; anchor: string } | null {
  const known = resolveSettingsAnchor(id);
  if (known) return known;
  const owner = document.getElementById(id)?.closest<HTMLElement>("[data-settings-tab]")?.dataset.settingsTab;
  return isSettingsTab(owner) ? { tab: owner, anchor: id } : null;
}

/**
 * The three Settings tabs, all mounted, one shown.
 *
 * Every tab stays mounted so an unsaved connection edit or a half-typed row name survives a trip to
 * another tab — the same promise the single continuous page made. The address decides which one
 * shows: `/settings/<tab>`. `/settings` and every old `/settings#section` link are rewritten in
 * place to the tab that section lives on now, and an in-page `#anchor` that belongs to another tab
 * (the webhook card's "Notifications →", a "Set it up in Connections" link) switches to that tab.
 */
export function SettingsTabs({ content }: { content: Record<SettingsTab, ReactNode> }) {
  const tab = useSettingsTab();
  const { tab: named } = useParams();
  const { hash, key, search } = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    const id = safeDecode(hash.slice(1));
    const target = id ? locate(id) : null;
    const wantTab = target?.tab ?? tab;
    const wantHash = target ? `#${target.anchor}` : hash;
    if (named !== wantTab || wantHash !== hash) {
      void navigate({ pathname: `/settings/${wantTab}`, search, hash: wantHash }, { replace: true });
      return;
    }
    if (!target) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    // The same anchor may be followed again after manual scrolling. Each navigation has a new
    // location key even when its hash is unchanged, so the jump repeats.
    document.getElementById(target.anchor)?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }, [hash, key, named, navigate, search, tab]);

  return (
    <>
      <Tabs<SettingsTab>
        id="settings"
        ariaLabel="Settings"
        value={tab}
        onChange={(next) => void navigate({ pathname: `/settings/${next}`, search })}
        options={SETTINGS_TABS}
        className="mb-6"
      />
      {SETTINGS_TABS.map(({ value }) => (
        <TabPanel
          key={value}
          id="settings"
          value={value}
          hidden={value !== tab}
          data-settings-tab={value}
          className="min-w-0"
        >
          {content[value]}
        </TabPanel>
      ))}
    </>
  );
}

/**
 * A tab's sections with a jump list beside them: a sticky column on a wide screen, a row of chips
 * that scrolls sideways on a phone.
 */
export function SectionsWithJumps({
  tab,
  label,
  sections,
  children,
}: {
  tab: SettingsTab;
  label: string;
  sections: readonly { id: string; label: string }[];
  children: ReactNode;
}) {
  const { hash, search } = useLocation();
  const ids = sections.map((section) => section.id);
  const fromHash = hash.slice(1);
  const active = useActiveSection(ids, ids.includes(fromHash) ? fromHash : (ids[0] ?? ""));

  return (
    <div className="md:grid md:grid-cols-[11rem_minmax(0,1fr)] md:gap-8">
      <nav
        aria-label={label}
        className="sticky top-14 z-10 -mx-4 mb-6 flex gap-1.5 overflow-x-auto border-b bg-background/95 px-4 py-2 backdrop-blur-sm md:top-6 md:mx-0 md:mb-0 md:flex-col md:self-start md:overflow-visible md:border-0 md:bg-transparent md:p-0 md:backdrop-blur-none"
      >
        {sections.map((section) => {
          const current = active === section.id;
          return (
            <Link
              key={section.id}
              to={{ pathname: `/settings/${tab}`, search, hash: `#${section.id}` }}
              aria-current={current ? "true" : undefined}
              className={cn(
                "shrink-0 whitespace-nowrap rounded-md border px-2.5 py-1.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:text-[13px]",
                // Chips on a phone (edge along the bottom), a list on a wide screen (edge down the left).
                current
                  ? cn(selectedClass, "font-medium md:shadow-selected-y")
                  : cn(unselectedClass, "md:border-transparent md:bg-transparent"),
              )}
            >
              {section.label}
            </Link>
          );
        })}
      </nav>
      <div className="min-w-0 space-y-10">{children}</div>
    </div>
  );
}

/** One section of a tab: its heading, one line under it, and its rows in a single panel. */
export function SettingsSection({
  id,
  title,
  description,
  children,
  className,
}: {
  id: string;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className={cn("scroll-mt-32 space-y-3 md:scroll-mt-8", className)}>
      <div className="space-y-1">
        <h2 id={`${id}-title`} className="text-base font-semibold tracking-tight">
          {title}
        </h2>
        {description && <p className="max-w-prose text-sm text-muted-foreground">{description}</p>}
      </div>
      {children}
    </section>
  );
}

/** The single panel a section's rows sit in. One card depth: nothing inside it is a card. */
export function SettingsPanel({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("divide-y overflow-hidden rounded-lg border bg-card", className)}>{children}</div>;
}

/** A setting whose control sits beside it: name and what it does on the left, the control on the right. */
export function SettingRow({
  title,
  description,
  control,
  children,
  id,
}: {
  title: ReactNode;
  description?: ReactNode;
  control?: ReactNode;
  /** Further lines under the description: an effect, a link, an inline fix. */
  children?: ReactNode;
  id?: string;
}) {
  return (
    <div id={id} className="scroll-mt-32 px-4 py-4 sm:px-5 md:scroll-mt-8">
      <div className="flex items-start justify-between gap-5">
        <div className="min-w-0 space-y-1">
          <p className="text-[13px] font-medium">{title}</p>
          {description && <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">{description}</p>}
        </div>
        {control && <div className="shrink-0">{control}</div>}
      </div>
      {children && <div className="mt-2 space-y-2">{children}</div>}
    </div>
  );
}

/** A setting whose control is wide (presets, a slider, a chart): heading on top, control below. */
export function SettingBlock({
  title,
  htmlFor,
  description,
  value,
  children,
}: {
  title: ReactNode;
  htmlFor?: string;
  description?: ReactNode;
  /** The current value, said in words at the right of the heading. */
  value?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="space-y-3 px-4 py-4 sm:px-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 space-y-1">
          {htmlFor ? (
            <label htmlFor={htmlFor} className="block text-[13px] font-medium">
              {title}
            </label>
          ) : (
            <p className="text-[13px] font-medium">{title}</p>
          )}
          {description && <p className="max-w-prose text-xs leading-relaxed text-muted-foreground">{description}</p>}
        </div>
        {value && <span className="shrink-0 text-xs font-medium tabular-nums">{value}</span>}
      </div>
      {children}
    </div>
  );
}
