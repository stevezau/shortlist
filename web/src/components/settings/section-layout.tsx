import { type ReactNode, useCallback, useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { useActiveSection } from "@/components/settings/active-section";
import { type Modified, ModifiedBadge, ModifiedCountContext, ModifiedDefault } from "@/components/settings/modified";
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
 * The Settings tabs, all mounted, one shown.
 *
 * Every tab stays mounted so an unsaved connection edit or a half-typed row name survives a trip to
 * another tab — the same promise the single continuous page made. The address decides which one
 * shows: `/settings/<tab>`. `/settings` and every old `/settings#section` link are rewritten in
 * place to the tab that section lives on now, and an in-page `#anchor` that belongs to another tab
 * (the webhook card's "Notifications →", a "Set it up in Connections" link) switches to that tab.
 */
export function SettingsTabs({ content }: { content: Partial<Record<SettingsTab, ReactNode>> }) {
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
 * A tab's sections with a jump list beside them: a sticky column on a wide screen, a sticky "Jump to"
 * select on a phone (a row of chips ran off the edge and clipped its last label).
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
  const navigate = useNavigate();
  const ids = sections.map((section) => section.id);
  const fromHash = hash.slice(1);
  const active = useActiveSection(ids, ids.includes(fromHash) ? fromHash : (ids[0] ?? ""));
  const [modifiedCounts, setModifiedCounts] = useState<Record<string, number>>({});
  const reportModified = useCallback(
    (id: string, count: number) => setModifiedCounts((current) => (current[id] === count ? current : { ...current, [id]: count })),
    [],
  );

  return (
    <ModifiedCountContext.Provider value={reportModified}>
      <div className="md:grid md:grid-cols-[11rem_minmax(0,1fr)] md:gap-8">
        <div className="sticky top-14 z-10 -mx-4 mb-6 border-b bg-background/95 px-4 py-2 backdrop-blur-sm md:top-6 md:mx-0 md:mb-0 md:self-start md:border-0 md:bg-transparent md:p-0 md:backdrop-blur-none">
          <label className="flex items-center gap-2 rounded-md border bg-card px-3 py-2 text-sm md:hidden">
            <span className="shrink-0 text-muted-foreground">Jump to:</span>
            <select
              aria-label={label}
              value={active}
              onChange={(event) => void navigate({ pathname: `/settings/${tab}`, search, hash: `#${event.target.value}` })}
              className="min-w-0 flex-1 bg-transparent font-medium focus-visible:outline-none"
            >
              {sections.map((section) => (
                <option key={section.id} value={section.id}>
                  {section.label}
                </option>
              ))}
            </select>
          </label>
          <nav aria-label={label} className="hidden md:flex md:flex-col md:gap-1.5">
            {sections.map((section) => {
              const current = active === section.id;
              const modified = modifiedCounts[section.id] ?? 0;
              return (
                <Link
                  key={section.id}
                  to={{ pathname: `/settings/${tab}`, search, hash: `#${section.id}` }}
                  aria-current={current ? "true" : undefined}
                  className={cn(
                    "flex items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-[13px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    current ? cn(selectedClass, "font-medium shadow-selected-y") : cn(unselectedClass, "border-transparent bg-transparent"),
                  )}
                >
                  {section.label}
                  {modified > 0 && <span className="shrink-0 text-xs font-normal text-muted-foreground">{modified} modified</span>}
                </Link>
              );
            })}
          </nav>
        </div>
        <div className="min-w-0 space-y-10">{children}</div>
      </div>
    </ModifiedCountContext.Provider>
  );
}

/** One section of a tab: its heading, one line under it, and its rows in a single panel. */
export function SettingsSection({
  id,
  title,
  description,
  modifiedCount = 0,
  children,
  className,
}: {
  id: string;
  title: ReactNode;
  description?: ReactNode;
  /** How many of this section's settings differ from their defaults; said beside the heading. */
  modifiedCount?: number;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className={cn("scroll-mt-32 space-y-3 md:scroll-mt-8", className)}>
      <div className="space-y-1">
        <h2 id={`${id}-title`} className="text-lg font-semibold tracking-tight">
          {title}
          {modifiedCount > 0 && (
            <span className="ml-2 text-sm font-normal text-muted-foreground">· {modifiedCount} modified</span>
          )}
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
  modified,
}: {
  title: ReactNode;
  description?: ReactNode;
  control?: ReactNode;
  /** Further lines under the description: an effect, a link, an inline fix. */
  children?: ReactNode;
  id?: string;
  /** Set when the value differs from its default: marks it and offers the way back. */
  modified?: Modified;
}) {
  return (
    <div id={id} className="scroll-mt-32 px-4 py-4 sm:px-5 md:scroll-mt-8">
      <div className="flex items-start justify-between gap-5">
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-medium">
            {title}
            <ModifiedBadge modified={modified} />
          </p>
          {description && <p className="max-w-prose text-sm text-muted-foreground">{description}</p>}
          <ModifiedDefault modified={modified} name={typeof title === "string" ? title : "this setting"} />
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
  modified,
  children,
}: {
  title: ReactNode;
  htmlFor?: string;
  /** Set when the value differs from its default: marks it and offers the way back. */
  modified?: Modified;
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
            <label htmlFor={htmlFor} className="block text-sm font-medium">
              {title}
              <ModifiedBadge modified={modified} />
            </label>
          ) : (
            <p className="text-sm font-medium">
              {title}
              <ModifiedBadge modified={modified} />
            </p>
          )}
          {description && <p className="max-w-prose text-sm text-muted-foreground">{description}</p>}
          <ModifiedDefault modified={modified} name={typeof title === "string" ? title : "this setting"} />
        </div>
        {value && <span className="shrink-0 text-sm font-medium tabular-nums">{value}</span>}
      </div>
      {children}
    </div>
  );
}
