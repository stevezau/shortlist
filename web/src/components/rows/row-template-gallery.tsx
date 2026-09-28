import {
  ArrowRight,
  Check,
  Clock,
  Film,
  Layers,
  Leaf,
  Link,
  Mail,
  Plus,
  RotateCcw,
  Search,
  Sparkles,
  Star,
  TrendingUp,
  Tv,
  Users,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { Link as RouterLink } from "react-router";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { renderRowName, sampleLibraryName } from "@/lib/format";
import { useRequestRowSources } from "@/lib/queries";
import type { RowKind } from "@/lib/row-kind-meta";
import {
  CONNECTIONS_SETTINGS,
  NO_REQUEST_SOURCE,
  noRequestSource,
} from "@/lib/row-kinds";
import {
  ROW_TEMPLATE_GROUPS,
  ROW_TEMPLATES,
  type RowTemplate,
} from "@/lib/row-templates";
import { cn } from "@/lib/utils";

const FILTERS: { label: string; kinds?: RowKind[] }[] = [
  { label: "All templates" },
  { label: "Discover", kinds: ["picked", "byw"] },
  { label: "Rewatch", kinds: ["again"] },
  { label: "Requests", kinds: ["requests"] },
  { label: "Seasonal", kinds: ["seasonal"] },
  { label: "Popular", kinds: ["popular"] },
];

const TEMPLATE_ICONS: Record<string, LucideIcon> = {
  "picked-for-you": Sparkles,
  "fresh-finds": Leaf,
  "from-the-vault": Clock,
  "movie-night": Film,
  "more-tv": Tv,
  "because-you-watched": Link,
  "seen-it-already": RotateCcw,
  "your-requests": Mail,
  seasonal: Star,
  "popular-here": TrendingUp,
};

const ORDERED_TEMPLATES = ROW_TEMPLATE_GROUPS.flatMap((group) =>
  ROW_TEMPLATES.filter((template) => template.kind === group.kind),
);

function TemplatePreview({ template }: { template: RowTemplate }) {
  const name = renderRowName(
    template.values.name ?? template.title,
    "Orbit",
    "Sarah",
    sampleLibraryName(template.values.media ?? "both"),
  );

  return (
    <div className="hidden rounded-lg border bg-background p-3 md:block">
      <div className="mb-3 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
        <span className="uppercase tracking-wider">On their Plex home</span>
        <span>Illustrative preview</span>
      </div>
      <p className="mb-2 text-xs font-medium">{name}</p>
      <div className="grid grid-cols-3 gap-2" aria-hidden="true">
        <div className="relative flex h-24 items-end overflow-hidden rounded bg-gradient-to-br from-success/40 via-success/10 to-background p-2">
          <div className="absolute -left-4 top-5 h-16 w-32 -rotate-45 border-t border-success/40 bg-success/10" />
          <span className="relative text-[10px] font-semibold uppercase leading-tight tracking-widest">The quiet<br />tide</span>
        </div>
        <div className="relative flex h-24 items-end overflow-hidden rounded bg-gradient-to-br from-muted via-background to-primary/10 p-2">
          <div className="absolute left-1/2 top-5 h-7 w-7 -translate-x-1/2 rounded-full bg-primary/60" />
          <div className="absolute left-1/2 top-5 h-7 w-20 -translate-x-1/2 -rotate-45 rounded-[50%] border border-primary/30" />
          <span className="relative text-[10px] font-semibold uppercase tracking-widest">Orbit</span>
        </div>
        <div className="relative flex h-24 items-end overflow-hidden rounded bg-gradient-to-b from-primary/30 via-muted to-background p-2">
          <div className="absolute -right-4 top-9 h-20 w-20 rotate-45 bg-success/30" />
          <div className="absolute -left-5 top-12 h-20 w-20 rotate-45 bg-background/50" />
          <span className="relative text-[10px] font-semibold uppercase leading-tight tracking-widest">Wild<br />coast</span>
        </div>
      </div>
      <p className="mt-2 text-[10px] text-muted-foreground">
        {template.values.requests_row
          ? "Their requests appear once they’re on Plex."
          : "Titles will be picked from your libraries."}
      </p>
    </div>
  );
}

function refreshLabel(template: RowTemplate): string {
  if (template.values.build === "shared" || template.values.requests_row) return "Every run";
  const days = template.values.refresh_days;
  if (days == null) return "Your defaults";
  if (days === 0) return "No scheduled refresh";
  if (days === 1) return "Every night";
  if (days === 7) return "Every week";
  return `Every ${days} days`;
}

function TemplateDetails({ template, needsRequestSource }: {
  template: RowTemplate;
  needsRequestSource: boolean;
}) {
  const Icon = TEMPLATE_ICONS[template.id] ?? Sparkles;
  const settings = [
    { label: "Audience", value: template.values.build === "shared" ? "One shared row" : "One row per person", icon: Users },
    { label: "Row size", value: `${template.values.size} titles`, icon: Layers },
    { label: "Refresh", value: refreshLabel(template), icon: Clock },
  ];

  return (
    <aside
      aria-label="Selected template details"
      aria-live="polite"
      className="space-y-4 border-t bg-card p-5 md:overflow-y-auto md:border-l md:border-t-0 md:p-6"
    >
      <div>
        <div className="mb-3 flex items-center gap-3">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-primary/20 bg-primary/10 text-primary">
            <Icon className="size-5" aria-hidden />
          </span>
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Selected template
          </span>
        </div>
        <h2 className="text-xl font-semibold tracking-tight">{template.title}</h2>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{template.blurb}</p>
        <ul className="mt-3 flex flex-wrap gap-1.5" aria-label="Template highlights">
          {template.highlights.map((highlight) => (
            <li key={highlight} className="rounded-md bg-muted px-2 py-1 text-[10px] text-muted-foreground">
              {highlight}
            </li>
          ))}
        </ul>
      </div>
      {needsRequestSource && (
        <p className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs leading-relaxed text-muted-foreground">
          {NO_REQUEST_SOURCE}{" "}
          <RouterLink to={CONNECTIONS_SETTINGS} className="underline underline-offset-2 hover:text-foreground">
            Settings &rsaquo; Connections
          </RouterLink>
        </p>
      )}
      <TemplatePreview template={template} />
      <dl className="divide-y">
        {settings.map(({ label, value, icon: SettingIcon }) => (
          <div key={label} className="flex items-center justify-between gap-3 py-2.5 text-xs">
            <dt className="flex items-center gap-2 text-muted-foreground">
              <SettingIcon className="size-3.5" aria-hidden />
              {label}
            </dt>
            <dd className="text-right">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs text-muted-foreground">You can change every setting in the next step.</p>
    </aside>
  );
}

function TemplatePicker({ onPick, onClose, noSource }: {
  onPick: (template: RowTemplate | null) => void;
  onClose: () => void;
  noSource: boolean;
}) {
  const [selected, setSelected] = useState(ROW_TEMPLATES[0]!);
  const [filter, setFilter] = useState(FILTERS[0]!);
  const [search, setSearch] = useState("");
  const needsRequestSource = Boolean(selected.values.requests_row && noSource);
  const query = search.trim().toLowerCase();
  const visible = ORDERED_TEMPLATES.filter((template) =>
    (!filter.kinds || filter.kinds.includes(template.kind)) &&
    [template.title, template.summary, template.blurb, ...template.highlights]
      .join(" ").toLowerCase().includes(query),
  );

  return (
    <>
      <DialogHeader className="shrink-0 border-b px-5 py-5 pr-12 text-left md:px-7">
        <DialogTitle className="text-2xl">Add a row</DialogTitle>
        <DialogDescription>Start with a template. Make it your own.</DialogDescription>
      </DialogHeader>
      <div className="min-h-0 overflow-y-auto md:grid md:grid-cols-[minmax(0,1fr)_340px] md:overflow-hidden">
        <div className="min-w-0 p-5 md:overflow-y-auto md:px-7">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" aria-hidden />
            <Input
              type="search"
              aria-label="Find a template"
              placeholder="Find a template…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              className="bg-card pl-9 text-xs"
            />
          </div>
          <div className="my-3 flex flex-wrap gap-1" role="group" aria-label="Filter templates">
            {FILTERS.map((option) => (
              <Button
                key={option.label}
                type="button"
                variant={filter === option ? "secondary" : "ghost"}
                size="sm"
                aria-pressed={filter === option}
                onClick={() => setFilter(option)}
                className="px-2.5 text-xs motion-reduce:transition-none"
              >
                {option.label}
              </Button>
            ))}
          </div>
          <div className="mb-3 flex items-center justify-between gap-3 text-[10px] text-muted-foreground">
            <p role="status" className="uppercase tracking-widest">
              {visible.length} {visible.length === 1 ? "template" : "templates"}
            </p>
            <span>Choose a starting point</span>
          </div>
          <div className="grid grid-cols-1 gap-2 min-[400px]:grid-cols-2 md:grid-cols-1 lg:grid-cols-2" role="group" aria-label="Templates">
            {visible.map((template) => {
              const Icon = TEMPLATE_ICONS[template.id] ?? Sparkles;
              const active = selected.id === template.id;
              return (
                <button
                  key={template.id}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setSelected(template)}
                  className={cn(
                    "relative flex min-h-[68px] items-center gap-3 rounded-lg border bg-card p-3 text-left transition-colors hover:border-primary/50 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none",
                    active && "border-primary/70 bg-primary/10 hover:bg-primary/10",
                  )}
                >
                  <span className={cn(
                    "flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground",
                    active && "bg-primary/10 text-primary",
                  )}>
                    <Icon className="size-4" aria-hidden />
                  </span>
                  <span className="min-w-0">
                    <span className="block text-xs font-medium">{template.title}</span>
                    <span className="mt-1 block text-[11px] leading-snug text-muted-foreground">{template.summary}</span>
                  </span>
                  {active && <Check className="absolute right-1.5 top-1.5 size-3 text-primary" aria-hidden />}
                </button>
              );
            })}
          </div>
          {visible.length === 0 && (
            <div className="py-10 text-center">
              <p className="text-sm font-medium">No templates found</p>
              <p className="mt-1 text-xs text-muted-foreground">Try another search or clear the filters.</p>
              <Button type="button" variant="outline" size="sm" className="mt-4" onClick={() => {
                setSearch("");
                setFilter(FILTERS[0]!);
              }}>Clear filters</Button>
            </div>
          )}
        </div>
        <TemplateDetails template={selected} needsRequestSource={needsRequestSource} />
      </div>
      <div className="flex shrink-0 items-center justify-between gap-2 border-t bg-card px-3 py-4 md:px-7">
        <Button type="button" variant="ghost" size="sm" onClick={() => onPick(null)}>
          <Plus aria-hidden />
          Start from scratch
        </Button>
        <div className="flex items-center gap-2">
          <Button type="button" variant="outline" onClick={onClose} className="hidden sm:inline-flex">Cancel</Button>
          <Button type="button" disabled={needsRequestSource} onClick={() => {
            if (!needsRequestSource) onPick(selected);
          }} className="text-xs sm:text-sm">
            Use template
            <ArrowRight aria-hidden />
          </Button>
        </div>
      </div>
    </>
  );
}

export function RowTemplateGallery({ open, onPick, onClose }: {
  open: boolean;
  /** Null = start from scratch. */
  onPick: (template: RowTemplate | null) => void;
  onClose: () => void;
}) {
  // Source checks run only while browsing; the editor handles unresolved checks in more detail.
  const sources = useRequestRowSources("", open);
  const noSource = sources.data !== undefined && noRequestSource(sources.data);
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-xl p-0 motion-reduce:animate-none sm:max-w-[1080px]">
        {/* Each opening starts a new choice, even when the parent keeps this dialog mounted. */}
        {open && <TemplatePicker onPick={onPick} onClose={onClose} noSource={noSource} />}
      </DialogContent>
    </Dialog>
  );
}
