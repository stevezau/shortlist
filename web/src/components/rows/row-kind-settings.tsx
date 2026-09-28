import type { ReactNode } from "react";
import { useId, useState } from "react";
import { Link } from "react-router";

import { BasedOnField } from "@/components/rows/row-based-on-field";
import { ColdStartFields } from "@/components/rows/row-cold-start-field";
import { RowFillPicker } from "@/components/rows/row-kind-picker";
import { RowMaxSeedsSetting } from "@/components/rows/row-max-seeds-setting";
import { MinWatchersField } from "@/components/rows/row-min-watchers-field";
import { RowSeasonsField } from "@/components/rows/row-seasons-field";
import { TakeTurnsSetting } from "@/components/rows/row-take-turns-setting";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useRequestRowSources } from "@/lib/queries";
import {
  CONNECTIONS_SETTINGS,
  FILL_META,
  followsAWatch,
  isNarrowGlobal,
  namesASeed,
  NIGHTLY_LINE,
  NO_REQUEST_SOURCE,
  noRequestSource,
  takeTurnsEnabled,
  type RowFill,
  type RowKindChoice,
  type RowKindContext,
  type RowSettingKey,
} from "@/lib/row-kinds";
import type {
  CollectionInput,
  RowSources,
  SeasonStatus,
  Settings,
  User,
} from "@/lib/types";

/** What every kind block reads. `shown` and `hidden` are `visibleSettings` / `hiddenButRead`. */
export type KindBlockProps = {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
  ctx: RowKindContext;
  shown: ReadonlySet<RowSettingKey>;
  hidden: readonly RowSettingKey[];
  settings: Settings | undefined;
  users: User[];
};

function KindBlock({ title, children }: { title: string; children: ReactNode }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="space-y-4 border-t pt-4">
      <h3 id={headingId} className="text-sm font-semibold">
        {title}
      </h3>
      {children}
    </section>
  );
}

/** "Take turns", for every fill that has it. Watch it again's sits under its fill-up settings. */
export function TakeTurns({ fill, ...props }: Omit<KindBlockProps, "users"> & { fill: RowFill }) {
  return (
    <TakeTurnsSetting
      input={props.input}
      set={props.set}
      shown={props.shown}
      hidden={props.hidden}
      enabled={takeTurnsEnabled(props.input, props.ctx)}
      hiddenWhy={
        fill === "again"
          ? "Its new picks only take turns while they match 1 or 2 watches"
          : `${FILL_META[fill].title} rows don't take turns`
      }
    />
  );
}

function ColdStart(props: KindBlockProps) {
  return (
    <ColdStartFields
      input={props.input}
      set={props.set}
      shown={props.shown}
      settings={props.settings}
      namesASeed={namesASeed(props.input, props.ctx)}
    />
  );
}

function PickedBlock(props: KindBlockProps) {
  return (
    <KindBlock title="How picks are chosen">
      {props.shown.has("max_seeds") && (
        // 3 and up: 1 or 2 would make it a Because you watched row, which is the kind picker's call.
        // A global of 1 or 2 would do the same, so following it is closed off too.
        <RowMaxSeedsSetting
          input={props.input}
          set={props.set}
          settings={props.settings}
          min={3}
          inheritBlockedReason={
            isNarrowGlobal(props.ctx)
              ? `Following the global default (${props.ctx.globalMaxSeeds}) would make this a Because you watched row.`
              : null
          }
        />
      )}
      <TakeTurns {...props} fill="picked" />
      <ColdStart {...props} />
    </KindBlock>
  );
}

function BecauseYouWatchedBlock(props: KindBlockProps) {
  return (
    <KindBlock title="Which watch it’s based on">
      {props.shown.has("based_on") && (
        <BasedOnField
          input={props.input}
          set={props.set}
          ctx={props.ctx}
          settings={props.settings}
        />
      )}
      <TakeTurns {...props} fill="byw" />
      <ColdStart {...props} />
      {followsAWatch(props.input, props.ctx) && (
        <p className="text-sm text-muted-foreground">{NIGHTLY_LINE}</p>
      )}
    </KindBlock>
  );
}

function WatchItAgainBlock(props: KindBlockProps) {
  const { input, set } = props;
  return (
    <KindBlock title="Which titles come back">
      {/* Last night's film is not an old favourite, so the default keeps a month of recent watches
          out of the shelf. */}
      {props.shown.has("rewatch_cooldown_days") && (
        <div
          data-setting="rewatch_cooldown_days"
          className="space-y-2"
        >
          <div className="flex flex-wrap items-center gap-2">
            <Label htmlFor="row-rewatch-cooldown">
              Skip titles finished in the last
            </Label>
            <Input
              id="row-rewatch-cooldown"
              type="number"
              min={0}
              max={365}
              value={input.rewatch_cooldown_days}
              onChange={(event) =>
                set({
                  rewatch_cooldown_days: Math.min(
                    365,
                    Math.max(0, Math.round(Number(event.target.value) || 0)),
                  ),
                })
              }
              className="w-20"
            />
            <span className="text-sm font-medium">days</span>
          </div>
          <p className="text-sm text-muted-foreground">
            Keeps something they watched last night out of the row. 0 lets
            anything they&rsquo;ve finished back in.
          </p>
        </div>
      )}
      <ColdStart {...props} />
      <p className="text-sm text-muted-foreground">
        If they run out of finished titles, the rest of the row is filled with
        new picks. How those are found is under What goes in it.
      </p>
    </KindBlock>
  );
}

type SourceState = RowSources["overseerr"];

const SOURCE_BADGE: Record<SourceState, { label: string; variant: "success" | "warning" | "secondary" }> = {
  connected: { label: "Connected", variant: "success" },
  unreachable: { label: "Unreachable", variant: "warning" },
  off: { label: "Off", variant: "secondary" },
};

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** One line under a source's badge: what it found, in numbers the owner can check against the app. */
function sourceDetail(sources: RowSources, source: "overseerr" | "radarr" | "sonarr"): string | null {
  if (sources[source] !== "connected") return null;
  if (source === "overseerr") {
    return `${plural(sources.seerr_requests, "request")} from ${plural(sources.seerr_requesters, "person")}, ${sources.seerr_linked} linked to someone here`;
  }
  const servers = sources.servers.filter((server) => server.kind === source);
  const tagging = servers.filter((server) => server.tag_requests).length;
  const tagged =
    source === "radarr"
      ? plural(sources.tagged_movies, "tagged movie")
      : plural(sources.tagged_shows, "tagged show");
  if (servers.length === 0) return tagged;
  const names = servers.map((server) => `${server.name}${server.is4k ? " (4K)" : ""}`).join(", ");
  const taggingWords =
    tagging === servers.length
      ? "tagging requests"
      : tagging === 0
        ? "not tagging requests"
        : `${tagging} of ${servers.length} tagging requests`;
  return `${names}: ${taggingWords}, ${tagged}`;
}

function SourceRow({ name, badge, detail }: { name: string; badge: ReactNode; detail: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 py-1.5 text-sm">
      <span className="font-medium">{name}</span>
      <div className="flex flex-col items-end gap-0.5 text-right">
        {badge}
        {detail && <span className="text-xs text-muted-foreground">{detail}</span>}
      </div>
    </div>
  );
}

function RequestSourcesPanel({ sources }: { sources: RowSources }) {
  const linked = sources.people.filter((person) => person.linked).length;
  const ready = sources.people.filter((person) => person.ready > 0).length;
  const nobody = noRequestSource(sources);
  return (
    <div className="divide-y rounded-md border px-3">
      {(["overseerr", "radarr", "sonarr"] as const).map((source) => {
        const badge = SOURCE_BADGE[sources[source]];
        return (
          <SourceRow
            key={source}
            name={source === "overseerr" ? "Overseerr" : source === "radarr" ? "Radarr" : "Sonarr"}
            badge={<Badge variant={badge.variant}>{badge.label}</Badge>}
            detail={sourceDetail(sources, source)}
          />
        );
      })}
      <SourceRow
        name="People"
        badge={
          <Badge variant={sources.people.length > 0 && linked === sources.people.length ? "success" : "warning"}>
            {linked} of {sources.people.length} linked
          </Badge>
        }
        detail={
          sources.people.length === 0
            ? "Nobody's requests could be matched to a person here yet"
            : `${plural(ready, "person")} with something ready on Plex`
        }
      />
      {sources.problems.length > 0 && (
        <ul className="space-y-1 py-2 text-xs text-muted-foreground">
          {sources.problems.map((problem, index) => (
            <li key={index}>{problem}</li>
          ))}
        </ul>
      )}
      {nobody && (
        <p className="py-2 text-sm text-muted-foreground">
          {NO_REQUEST_SOURCE} Connect one under{" "}
          <Link
            to={CONNECTIONS_SETTINGS}
            className="underline underline-offset-2 hover:text-foreground"
          >
            Settings &rsaquo; Connections
          </Link>
          .
        </p>
      )}
    </div>
  );
}

/** The own-tag preview: every tag the pattern (or Overseerr) matched, and who it belongs to. */
function TagPreview({ tags }: { tags: RowSources["tags"] }) {
  if (tags.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No tags matched yet. Press Check after changing the pattern to see what it finds.
      </p>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Tag</TableHead>
          <TableHead>Belongs to</TableHead>
          <TableHead className="text-right">Titles</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tags.map((tag) => (
          <TableRow key={`${tag.source}:${tag.label}`}>
            <TableCell className="font-mono text-xs">{tag.label}</TableCell>
            <TableCell>
              {tag.ambiguous ? (
                <div className="flex flex-col items-start gap-0.5">
                  <Badge variant="warning">No one</Badge>
                  <span className="text-xs text-muted-foreground">Fits more than one person</span>
                </div>
              ) : (
                tag.display_name || <Badge variant="secondary">No one</Badge>
              )}
            </TableCell>
            <TableCell className="text-right">{tag.titles}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/**
 * The Your requests row's own settings. The sources panel reads the setup check ONCE with the saved
 * pattern, and again only on Check: the endpoint reads Overseerr and every Arr in turn, so a fetch
 * per keystroke would hammer the owner's other apps.
 */
export function YourRequestsBlock(props: KindBlockProps) {
  const { input, set } = props;
  const [checkedPattern, setCheckedPattern] = useState(input.requests_tag_pattern);
  const sources = useRequestRowSources(checkedPattern, true);
  const patternId = useId();
  const check = () => {
    if (input.requests_tag_pattern === checkedPattern) void sources.refetch();
    else setCheckedPattern(input.requests_tag_pattern);
  };
  return (
    <KindBlock title="Which requests show up">
      {props.shown.has("requests_window_days") && (
        <div data-setting="requests_window_days" className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Label htmlFor="row-requests-window">
              Show titles that landed in the last
            </Label>
            <Input
              id="row-requests-window"
              type="number"
              min={0}
              max={3650}
              value={input.requests_window_days}
              onChange={(event) =>
                set({
                  requests_window_days: Math.min(
                    3650,
                    Math.max(0, Math.round(Number(event.target.value) || 0)),
                  ),
                })
              }
              className="w-24"
            />
            <span className="text-sm font-medium">days</span>
          </div>
          <p className="text-sm text-muted-foreground">
            Older arrivals drop off, so a request they&rsquo;ve lost interest in
            doesn&rsquo;t sit there for good. 0 keeps every title until
            they&rsquo;ve watched it.
          </p>
        </div>
      )}

      {props.shown.has("requests_sources") && (
        <div data-setting="requests_sources" className="space-y-2">
          <p className="text-sm font-medium">Where requests are read from</p>
          {sources.isPending ? (
            <div className="space-y-2" aria-busy="true">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : sources.isError ? (
            <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
              <span title={sources.error instanceof Error ? sources.error.message : String(sources.error)}>
                Couldn&rsquo;t check the sources.
              </span>{" "}
              Press Check, under Use my own tags, to try again.
            </p>
          ) : (
            <RequestSourcesPanel sources={sources.data} />
          )}
        </div>
      )}

      {props.shown.has("requests_tag_pattern") && (
        <details
          data-setting="requests_tag_pattern"
          // Open while there is a pattern to see, and when the retry (Check) is needed.
          open={input.requests_tag_pattern || sources.isError ? true : undefined}
          className="group space-y-3"
        >
          <summary className="cursor-pointer text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            Use my own tags
          </summary>
          <div className="space-y-2">
            <Label htmlFor="row-requests-pattern">Tag pattern</Label>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                id="row-requests-pattern"
                value={input.requests_tag_pattern}
                onChange={(event) => set({ requests_tag_pattern: event.target.value })}
                placeholder="req-{username}"
                aria-describedby={patternId}
                className="w-64 max-w-full"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={check}
                disabled={sources.isFetching}
              >
                {sources.isFetching ? "Checking…" : "Check"}
              </Button>
            </div>
            <p id={patternId} className="text-sm text-muted-foreground">
              {"{username}"} is their Plex username, {"{name}"} is their name in
              Shortlist. Matching ignores case, and spaces count as dashes, as
              Radarr/Sonarr store them. Overseerr&rsquo;s own tags are still
              read automatically alongside this.
            </p>
          </div>
          {sources.data && <TagPreview tags={sources.data.tags} />}
        </details>
      )}

      <p className="rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
        A person with nothing ready gets no row. It appears on the first run
        after something they asked for lands, and goes again once they&rsquo;ve
        watched everything in it.
      </p>
    </KindBlock>
  );
}

function PopularBlock(props: KindBlockProps) {
  return (
    <KindBlock title="What counts as popular">
      {props.shown.has("min_watchers") && (
        <MinWatchersField
          input={props.input}
          set={props.set}
          users={props.users}
        />
      )}
      <ColdStart {...props} />
    </KindBlock>
  );
}

function FillBlock({ fill, ...props }: KindBlockProps & { fill: RowFill }) {
  switch (fill) {
    case "picked":
      return <PickedBlock {...props} />;
    case "byw":
      return <BecauseYouWatchedBlock {...props} />;
    case "again":
      return <WatchItAgainBlock {...props} />;
    case "requests":
      return <YourRequestsBlock {...props} />;
    case "popular":
      return <PopularBlock {...props} />;
  }
}

/**
 * The settings that belong to the row's kind, under the kind picker (design §5). A Seasonal row gets
 * "Which seasons" and "How it's filled" first, then exactly what that fill shows on its own — which
 * is what keeps every seasonal combination reachable.
 */
export function RowKindSettings({
  choice,
  onChooseFill,
  seasonStatus,
  ...props
}: KindBlockProps & {
  choice: RowKindChoice;
  onChooseFill: (fill: RowFill) => void;
  /** Where the SAVED row is in its calendar, while the form still matches it; else null. */
  seasonStatus: SeasonStatus | null;
}) {
  if (choice.kind !== "seasonal") return <FillBlock {...props} fill={choice.fill} />;
  const { input, set } = props;
  return (
    <>
      {props.shown.has("seasons") && (
        <div data-setting="seasons" className="border-t pt-4">
          <RowSeasonsField
            value={{
              seasons: input.seasons,
              season_lead_days: input.season_lead_days,
              season_after_days: input.season_after_days,
            }}
            onChange={set}
            schedule={input.schedule}
            name={input.name_template || input.name}
            status={seasonStatus}
          />
        </div>
      )}
      <div className="border-t pt-4">
        <RowFillPicker value={choice.fill} onChange={onChooseFill} />
      </div>
      <FillBlock {...props} fill={choice.fill} />
    </>
  );
}
