import {
  Clapperboard,
  ExternalLink,
  Inbox,
  RotateCcw,
  Send,
  Star,
  Trash2,
  Users,
  X,
} from "lucide-react";
import { type ReactNode, useId, useMemo, useState } from "react";

import {
  ImdbGlyph,
  RadarrGlyph,
  SonarrGlyph,
  TmdbGlyph,
  TraktGlyph,
} from "@/components/brand-glyphs";
import { PageHeader } from "@/components/page-header";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Link, useSearchParams } from "react-router";

import { apiErrorMessage } from "@/lib/api";
import { formatDate, settingBool, settingString } from "@/lib/format";
import {
  useArrStatus,
  useClearRequests,
  useDeleteRequests,
  useRejectRequests,
  useRequests,
  useRestoreRequests,
  useSendRequests,
  useSettings,
  useUsers,
} from "@/lib/queries";
import { sourceShortLabel } from "@/lib/sources";
import type { RequestCandidate } from "@/lib/types";
import { type DisplayNameLookup, displayNameLookup } from "@/lib/user-names";
import { cn } from "@/lib/utils";

/** Everything this page sends the owner to Settings for lives in one card — "Fill in the gaps
 *  automatically", under the Requests section. The hash lands them on it (`use-hash-scroll.ts`). */
const SETTINGS_LINK = "/settings#requests";

/** The server caps one inbox read at 500 rows (`api/requests.py` MAX_INBOX), sorted waiting → sent →
 *  rejected, so filtering here narrows what was loaded rather than the whole history. Kept in step by
 *  hand: the count is only used to decide whether to say so. */
const MAX_LOADED = 500;

function RequestsSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 5 }, (_, i) => (
        <Skeleton key={i} className="h-16 w-full" />
      ))}
    </div>
  );
}

/**
 * The title's artwork — the whole point of the inbox being visual rather than a wall of names.
 *
 * TMDB's image CDN, built from the stored path: `w154` is the smallest bucket that still looks sharp
 * at this size on a 2x display, so a 40-title inbox costs a few hundred KB rather than megabytes.
 * `loading="lazy"` keeps the off-screen ones off the wire entirely. A title with no artwork (TMDB
 * has none, or the row predates 0044) gets a placeholder tile of the same size, so rows never jump.
 */
function Poster({ item }: { item: RequestCandidate }) {
  // TMDB's CDN is a third-party host this app never checks: a server behind a restrictive network,
  // an ad-blocker, or a title whose artwork was pulled all fail at load time, long after the path
  // looked fine. Falling back on error keeps that as a tidy placeholder instead of a broken-image
  // icon in every row.
  const [failed, setFailed] = useState(false);

  if (!item.poster_path || failed) {
    return (
      <div
        className="flex h-[87px] w-[58px] shrink-0 items-center justify-center rounded border bg-muted"
        aria-hidden="true"
      >
        <Clapperboard className="h-5 w-5 text-muted-foreground/60" />
      </div>
    );
  }
  return (
    <img
      src={`https://image.tmdb.org/t/p/w154${item.poster_path}`}
      // Decorative: the title is right beside it as real text, so announcing it twice is noise.
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
      className="h-[87px] w-[58px] shrink-0 rounded border object-cover"
    />
  );
}

function TypeBadge({
  mediaType,
}: {
  mediaType: RequestCandidate["media_type"];
}) {
  return (
    <Badge variant="outline" className="gap-1">
      <Clapperboard className="h-3 w-3" aria-hidden="true" />
      {mediaType === "movie" ? "Movie" : "Show"}
    </Badge>
  );
}

/** "Wanted by …" — the actual names when a run recorded them, up to three then "+N more"; falls
 *  back to the bare count for rows queued before who-wanted-it was tracked. `wanters` holds bare
 *  Plex usernames, so every name goes through the lookup to read the same as it does on Users. */
function wantedByLabel(
  item: RequestCandidate,
  nameOf: DisplayNameLookup,
): string {
  const names = (item.wanters ?? []).map(nameOf);
  if (names.length === 0) {
    return `Wanted by ${item.demand} ${item.demand === 1 ? "person" : "people"}`;
  }
  if (names.length <= 3) return `Wanted by ${names.join(", ")}`;
  return `Wanted by ${names.slice(0, 3).join(", ")} +${names.length - 3} more`;
}

type QuickLink = {
  label: string;
  icon: ReactNode;
  href: string;
  strong?: boolean;
};

/** Quick look-it-up links: TMDB and Trakt jump straight to the title by its TMDB id; IMDb is a
 *  title search (Shortlist doesn't store an IMDb id). `lead` prepends extra links (e.g. the sent
 *  log's "Open in Sonarr/Radarr") so they sit in the same row. All open in a new tab. */
function ExternalLinks({
  item,
  lead = [],
}: {
  item: RequestCandidate;
  lead?: QuickLink[];
}) {
  const tmdbPath = item.media_type === "movie" ? "movie" : "tv";
  const traktType = item.media_type === "movie" ? "movie" : "show";
  const links: QuickLink[] = [
    ...lead,
    {
      label: "TMDB",
      icon: <TmdbGlyph className="h-3.5 w-3.5 rounded-[2px]" />,
      href: `https://www.themoviedb.org/${tmdbPath}/${item.tmdb_id}`,
    },
    {
      label: "IMDb",
      icon: <ImdbGlyph className="h-3.5 w-3.5 rounded-[2px]" />,
      // Deep-link straight to the title when we resolved its id; otherwise fall back to a search.
      href: item.imdb_id
        ? `https://www.imdb.com/title/${item.imdb_id}/`
        : `https://www.imdb.com/find/?q=${encodeURIComponent(item.title)}&s=tt`,
    },
    {
      label: "Trakt",
      icon: <TraktGlyph className="h-3.5 w-3.5" />,
      href: `https://trakt.tv/search/tmdb/${item.tmdb_id}?id_type=${traktType}`,
    },
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      {links.map((link) => (
        <a
          key={link.label}
          href={link.href}
          target="_blank"
          rel="noopener noreferrer"
          className={
            link.strong
              ? "inline-flex items-center gap-1 font-medium text-foreground hover:underline focus-visible:underline"
              : "inline-flex items-center gap-1 text-muted-foreground hover:text-foreground hover:underline focus-visible:text-foreground"
          }
        >
          {link.icon}
          {link.label}
          <ExternalLink className="h-3 w-3" aria-hidden="true" />
        </a>
      ))}
    </div>
  );
}

/**
 * The provenance behind a request: one line per (person, row) that wanted it, with the reason —
 * the seed ("because they watched …") or, for a seedless source, how it was suggested. This is the
 * answer to "where did this come from and why", not just a count.
 */
function WhyBreakdown({
  why,
  nameOf,
}: {
  why: RequestCandidate["why"];
  nameOf: DisplayNameLookup;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!why || why.length === 0) return null;
  // A popular title can have dozens of wanters — showing every reason is a wall. Show a few, then
  // let the owner expand the rest on demand.
  const LIMIT = 3;
  const shown = expanded ? why : why.slice(0, LIMIT);
  const hidden = why.length - shown.length;
  return (
    <ul className="space-y-0.5 border-l-2 border-muted pl-3 text-xs text-muted-foreground">
      {shown.map((w, i) => (
        <li key={`${w.user}-${w.row}-${i}`}>
          <span className="font-medium text-foreground/80">
            {nameOf(w.user)}
          </span>{" "}
          · <span>{w.row}</span>
          {w.seed ? (
            <span> · because they watched {w.seed}</span>
          ) : w.source ? (
            <span> · via {sourceShortLabel(w.source)}</span>
          ) : null}
        </li>
      ))}
      {why.length > LIMIT && (
        <li>
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className="text-primary underline-offset-4 hover:underline"
          >
            {expanded
              ? "Show fewer"
              : `+${hidden} more ${hidden === 1 ? "reason" : "reasons"}`}
          </button>
        </li>
      )}
    </ul>
  );
}

/** The facts that let the owner judge a title at a glance: type, rating, and who wanted it. The
 *  "wanted by …" list gets its own line — on a popular title it runs to three names plus "+18 more",
 *  and inline it pushed the rating and tags off the end of a scannable row. */
function TitleMeta({
  item,
  globalTag,
  nameOf,
}: {
  item: RequestCandidate;
  globalTag: string;
  nameOf: DisplayNameLookup;
}) {
  // The global tag is applied at send time and never stored on the candidate, so add it here to
  // show the full set of tags this title will actually get (deduped against the per-user/row tags).
  const tags = [...new Set([...(globalTag ? [globalTag] : []), ...item.tags])];
  return (
    <div className="space-y-1 text-sm text-muted-foreground">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <TypeBadge mediaType={item.media_type} />
        {item.year ? <span>{item.year}</span> : null}
        <span className="inline-flex items-center gap-1">
          <Star
            className="h-3.5 w-3.5 fill-current text-amber-500"
            aria-hidden="true"
          />
          <span className="font-medium text-foreground">
            {item.rating.toFixed(1)}
          </span>
          {item.vote_count > 0 && (
            <span className="text-xs">
              ({item.vote_count.toLocaleString()} votes)
            </span>
          )}
        </span>
        {tags.map((tag) => (
          <Badge key={tag} variant="secondary" className="font-normal">
            {tag}
          </Badge>
        ))}
      </div>
      <p
        className="flex items-start gap-1.5 text-xs"
        title={(item.wanters ?? []).map(nameOf).join(", ") || undefined}
      >
        <Users className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
        {wantedByLabel(item, nameOf)}
      </p>
    </div>
  );
}

/**
 * Requests are off, but titles queued before that are still on file. The inbox stays readable —
 * hiding it would lose them — but nothing here can be acted on, and it has to say so: the live
 * "Send to Sonarr/Radarr" button used to render exactly as it does when the feature is on.
 */
function RequestsOffBanner() {
  return (
    <div className="space-y-2 rounded-lg border border-dashed bg-muted/30 p-4">
      <p className="text-sm font-medium">Requests are off</p>
      <p className="text-sm text-muted-foreground">
        These titles were found before you turned requests off. Nothing new is
        added while it stays off, Shortlist isn&rsquo;t asking Radarr or Sonarr
        for anything, and nothing here can be sent or rejected until you turn it
        back on.
      </p>
      <Button asChild variant="outline" size="sm">
        <Link to={SETTINGS_LINK}>Go to Settings &rarr; Requests</Link>
      </Button>
    </div>
  );
}

/** What Sonarr/Radarr has for a title right now, in one word. Absent when neither app tracks it —
 *  which for a waiting title is the normal case, so nothing is drawn rather than "not found". */
const ARR_STATUS_LABELS: Record<
  string,
  { label: string; variant: "success" | "default" | "secondary" | "warning" }
> = {
  downloaded: { label: "Downloaded", variant: "success" },
  downloading: { label: "Downloading", variant: "default" },
  queued: { label: "Searching", variant: "secondary" },
  unmonitored: { label: "Not monitored", variant: "warning" },
};

function ArrStatusBadge({ status }: { status?: string | null }) {
  const shown = status ? ARR_STATUS_LABELS[status] : undefined;
  if (!shown) return null;
  return <Badge variant={shown.variant}>{shown.label}</Badge>;
}

function PendingRow({
  item,
  checked,
  onToggle,
  globalTag,
  disabled,
  arrStatus,
  nameOf,
}: {
  item: RequestCandidate;
  checked: boolean;
  onToggle: (id: number) => void;
  globalTag: string;
  /** Requests are off — the row is still readable, but it cannot be selected for sending. */
  disabled: boolean;
  arrStatus?: string | null;
  nameOf: DisplayNameLookup;
}) {
  const app = item.media_type === "movie" ? "Radarr" : "Sonarr";
  return (
    <label
      className={cn(
        "flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors",
        // Selection is what the whole toolbar acts on, so a picked card says so on the card itself —
        // a 4px checkbox was the only difference between "will be sent" and "won't".
        checked
          ? "border-primary/60 bg-primary/5"
          : "hover:border-border hover:bg-muted/50",
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={() => onToggle(item.id)}
        className="mt-1.5 h-4 w-4 shrink-0 accent-primary disabled:cursor-not-allowed disabled:opacity-50"
      />
      <Poster item={item} />
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <p className="text-base font-semibold leading-tight">{item.title}</p>
          <ArrStatusBadge status={arrStatus} />
        </div>
        <TitleMeta item={item} globalTag={globalTag} nameOf={nameOf} />
        <WhyBreakdown why={item.why} nameOf={nameOf} />
        <ExternalLinks item={item} />
        {/* Deliberately does NOT promise the row disappears next run: the tidy-up matches shows by
            the TMDB id Sonarr v4 reports, and Sonarr v3 doesn't report one at all (`_apply_arr_state`,
            `arr_present`), so on v3 a show sitting in Sonarr stays in this list. */}
        {arrStatus ? (
          <p className="text-xs text-muted-foreground">
            Already in {app} &mdash; it was added there after it landed here, so
            you don&rsquo;t need to send it again.
          </p>
        ) : null}
        {/* Weaker than it used to be, on purpose: nothing here proves the Arr refuses a hand-made
            add, only that `request_missing` never auto-sends an excluded title. */}
        {item.excluded ? (
          <p className="text-xs text-warning">
            {app} was told never to add this again &mdash; usually left behind
            by deleting it there ({app} calls it an import exclusion). Shortlist
            never sends it for you; clear it in {app} if you want it back.
          </p>
        ) : null}
        {item.detail ? (
          <p className="text-xs text-muted-foreground">
            Last attempt: {item.detail}
          </p>
        ) : null}
      </div>
    </label>
  );
}

/** The send log: a title that went to Sonarr or Radarr — which app, when it went, the app's answer,
 *  a link straight into that app, and why it was wanted. */
function SentRow({
  item,
  radarrUrl,
  sonarrUrl,
  onClear,
  clearing,
  arrStatus,
  nameOf,
}: {
  item: RequestCandidate;
  radarrUrl: string;
  sonarrUrl: string;
  onClear: (id: number) => void;
  clearing: boolean;
  arrStatus?: string | null;
  nameOf: DisplayNameLookup;
}) {
  const isMovie = item.media_type === "movie";
  const app = isMovie ? "Radarr" : "Sonarr";
  const ArrGlyph = isMovie ? RadarrGlyph : SonarrGlyph;
  const base = (isMovie ? radarrUrl : sonarrUrl).replace(/\/+$/, "");
  // Deep-link straight to the title's arr page. Radarr accepts its TMDB id; Sonarr has NO id URL —
  // only /series/<titleSlug> — so it needs the slug captured at send time. Without a slug (a title
  // sent before we recorded it) fall back to the app's home page rather than a dead link.
  const arrPath = isMovie
    ? `movie/${item.arr_slug ?? item.tmdb_id}`
    : item.arr_slug
      ? `series/${item.arr_slug}`
      : "";
  const arrLink = base ? `${base}/${arrPath}` : "";
  const lead = arrLink
    ? [
        {
          label: `Open in ${app}`,
          icon: <ArrGlyph className="h-3.5 w-3.5 rounded-[2px]" />,
          href: arrLink,
          strong: true,
        },
      ]
    : [];
  return (
    <div className="flex items-start gap-3 rounded-lg border p-3">
      <Poster item={item} />
      <div className="min-w-0 flex-1 space-y-1.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-medium">{item.title}</p>
          <div className="flex items-center gap-2">
            <Badge variant="success" className="gap-1">
              <ArrGlyph className="h-3.5 w-3.5 rounded-[2px]" />
              Sent to {app}
            </Badge>
            <ArrStatusBadge status={arrStatus} />
            <Button
              variant="ghost"
              size="sm"
              disabled={clearing}
              onClick={() => onClear(item.id)}
              title={`Remove from the send log. ${item.title} stays in ${app} — this only clears the entry here, and it won't be re-requested.`}
            >
              <X aria-hidden="true" />
              Clear
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <TypeBadge mediaType={item.media_type} />
          {item.year ? <span>{item.year}</span> : null}
          {item.updated_at ? (
            <span>Sent {formatDate(item.updated_at)}</span>
          ) : null}
          {item.detail ? <span>· {item.detail}</span> : null}
        </div>
        <WhyBreakdown why={item.why} nameOf={nameOf} />
        {/* The "Open in Sonarr/Radarr" link now sits with the TMDB/IMDb/Trakt look-ups, not up top. */}
        <ExternalLinks item={item} lead={lead} />
      </div>
    </div>
  );
}

/** A rejected title — no run will ask Radarr or Sonarr for it again (`_handled_requests` feeds every
 *  rejected row into the engine's skip set). "Allow again" un-rejects it, moving it straight back to
 *  Waiting (metadata intact) so it can be sent. */
function RejectedRow({
  item,
  onAllowAgain,
  disabled,
  nameOf,
}: {
  item: RequestCandidate;
  onAllowAgain: (id: number) => void;
  disabled: boolean;
  nameOf: DisplayNameLookup;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed px-3 py-2 text-sm">
      <div className="min-w-0">
        <span className="font-medium">{item.title}</span>{" "}
        <span className="text-muted-foreground">
          {item.year ? `· ${item.year} ` : ""}· {wantedByLabel(item, nameOf)}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Badge variant="secondary">rejected</Badge>
        <Button
          variant="ghost"
          size="sm"
          disabled={disabled}
          onClick={() => onAllowAgain(item.id)}
          title="Move this back to Waiting so you can send it."
        >
          <RotateCcw aria-hidden="true" />
          Allow again
        </Button>
      </div>
    </div>
  );
}

/** Which slice of the inbox is on screen: the actionable queue, the send log, or rejected titles. */
type RequestView = "waiting" | "sent" | "rejected";

/** A missing title is exactly one media type, so the list can be split by the library it'd land in. */
type MediaFilter = "all" | "movie" | "show";

/** How the on-screen list is ordered: newest activity, best rated, or most-wanted first. */
type RequestSort = "recent" | "rating" | "demand";

const SORT_OPTIONS: { value: RequestSort; label: string }[] = [
  { value: "recent", label: "Recent" },
  { value: "rating", label: "Top rated" },
  { value: "demand", label: "Most wanted" },
];

/** A rating floor to hide weaker titles. Every queued title already cleared the request min-rating
 *  gate, so the useful thresholds sit above it — these narrow a crowded inbox to the strongest. */
const RATING_OPTIONS: { value: string; label: string }[] = [
  { value: "0", label: "Any" },
  { value: "7", label: "7+" },
  { value: "8", label: "8+" },
  { value: "9", label: "9+" },
];

/** A vote-count floor — a high rating on a handful of votes is noise; this keeps only well-attested
 *  titles. */
const VOTES_OPTIONS: { value: string; label: string }[] = [
  { value: "0", label: "Any" },
  { value: "100", label: "100+" },
  { value: "500", label: "500+" },
  { value: "1000", label: "1k+" },
];

/**
 * One refinement control in the filter bar. These are deliberately NOT `Segmented`: sort + rating +
 * votes as chip groups put eleven buttons next to the Waiting/Sent tabs, four of them highlighted
 * (their own defaults), so the tab strip — the only control that changes what you're looking at —
 * was indistinguishable from a rating floor. A labelled dropdown is one quiet control per choice,
 * and a default reads as neutral.
 */
function FilterSelect<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as T)}
        className="rounded-md border bg-background px-2 py-1 text-xs font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/** One person offered by the "Wanted by" filter, and how many titles on this tab they wanted.
 *  `name` is the Plex username stored in `wanters` — the key everything filters on, so two people
 *  who happen to answer to the same display name stay separate; `label` is what the chip shows. */
type PersonOption = { name: string; label: string; count: number };

/** The names on a tab, most titles first, so the busiest people are the easiest to reach. Ties break
 *  alphabetically on the shown name rather than by whatever order the payload arrived in. */
function peopleOn(
  list: RequestCandidate[],
  nameOf: DisplayNameLookup,
): PersonOption[] {
  const counts = new Map<string, number>();
  for (const item of list) {
    for (const name of item.wanters ?? []) {
      counts.set(name, (counts.get(name) ?? 0) + 1);
    }
  }
  return [...counts]
    .map(([name, count]) => ({ name, label: nameOf(name), count }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

/**
 * "Wanted by": pick one person, or several, to see only what they asked for — the answer to "what do
 * I need to grab to get this new person up and running" (issue #61).
 *
 * Chips rather than a dropdown because the page's other pick-from-a-set control (Movies/Shows) is
 * already chips, and several can be on at once — which no `<select>` on this page does. Nobody
 * picked means everybody, so the filter starts out of the way.
 *
 * Long rosters collapse to the first few, exactly like the why-list above: a server with forty
 * sharers would otherwise open on forty buttons. A picked name is never collapsed away, or it could
 * be filtering with no way to see or undo it.
 */
function PeopleFilter({
  people,
  selected,
  onToggle,
}: {
  people: PersonOption[];
  selected: Set<string>;
  onToggle: (name: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const labelId = useId();
  const LIMIT = 8;
  const shown = expanded
    ? people
    : people.filter((p, i) => i < LIMIT || selected.has(p.name));
  const hidden = people.length - shown.length;
  return (
    <div
      role="group"
      aria-labelledby={labelId}
      className="flex flex-wrap items-center gap-x-3 gap-y-2"
    >
      <span id={labelId} className="text-xs text-muted-foreground">
        Wanted by
      </span>
      {shown.map((person) => (
        <Button
          key={person.name}
          type="button"
          size="sm"
          variant={selected.has(person.name) ? "default" : "outline"}
          aria-pressed={selected.has(person.name)}
          onClick={() => onToggle(person.name)}
        >
          {person.label} ({person.count})
        </Button>
      ))}
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="text-xs text-primary underline-offset-4 hover:underline focus-visible:underline"
        >
          +{hidden} more {hidden === 1 ? "person" : "people"}
        </button>
      )}
    </div>
  );
}

/** Filtered down to nothing. A narrowed list must never read as an empty one, so this says how many
 *  are really on the tab and which control brings them back. Only reachable when a rating, vote or
 *  people filter is set — the Movies/Shows split only ever renders when both types are present, so
 *  it can never empty a list on its own — which is what makes "Clear filters" a safe thing to name. */
function NoMatches({ label, total }: { label: string; total: number }) {
  return (
    <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
      No {label} title clears these filters. {total}{" "}
      {total === 1 ? "is" : "are"} on this tab in total &mdash; use{" "}
      <strong className="font-medium text-foreground">Clear filters</strong>{" "}
      above to see {total === 1 ? "it" : "them"}.
    </p>
  );
}

/** Order a list by the chosen sort. `recent` = newest state change first, falling back to queue order
 *  (id) for items that were queued but never sent, so a sent log reads newest-first and a waiting
 *  queue keeps its arrival order. */
function sortRequests(
  list: RequestCandidate[],
  sort: RequestSort,
): RequestCandidate[] {
  const copy = [...list];
  if (sort === "rating") {
    copy.sort((a, b) => b.rating - a.rating || b.demand - a.demand);
  } else if (sort === "demand") {
    copy.sort((a, b) => b.demand - a.demand || b.rating - a.rating);
  } else {
    copy.sort((a, b) => {
      const ta = a.updated_at ? Date.parse(a.updated_at) : 0;
      const tb = b.updated_at ? Date.parse(b.updated_at) : 0;
      return tb - ta || b.id - a.id;
    });
  }
  return copy;
}

export function RequestsPage() {
  const requestsQuery = useRequests();
  const settingsQuery = useSettings();
  const arrStatusQuery = useArrStatus();
  // `wanters` and `why[].user` are bare Plex usernames; the users list is what turns them into the
  // names the Users page shows. Nothing here waits on it — until it arrives (or if it fails) every
  // name resolves to itself, which is exactly what this page showed before.
  const usersQuery = useUsers();
  const send = useSendRequests();
  const reject = useRejectRequests();
  const del = useDeleteRequests();
  const restore = useRestoreRequests();
  const clear = useClearRequests();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Opens on Waiting, but a `?tab=sent` deep-link (e.g. the dashboard's "View the full send log")
  // lands straight on that view. `?tab=dismissed` is an accepted alias for the renamed Rejected tab.
  const [searchParams] = useSearchParams();
  const initialTab = searchParams.get("tab");
  const [view, setView] = useState<RequestView>(
    initialTab === "sent"
      ? "sent"
      : initialTab === "rejected" || initialTab === "dismissed"
        ? "rejected"
        : "waiting",
  );
  const [media, setMedia] = useState<MediaFilter>("all");
  const [sort, setSort] = useState<RequestSort>("recent");
  const [minRating, setMinRating] = useState("0");
  const [minVotes, setMinVotes] = useState("0");
  // Whose requests to show. Empty = everyone's, so the page opens unfiltered.
  const [people, setPeople] = useState<Set<string>>(new Set());

  const togglePerson = (name: string) =>
    setPeople((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const clearFilters = () => {
    setMinRating("0");
    setMinVotes("0");
    setPeople(new Set());
  };

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const nameOf = useMemo(
    () => displayNameLookup(usersQuery.data),
    [usersQuery.data],
  );

  // `?? []` inline would be a fresh array every render, so both memos below would recompute on
  // every render (and eslint says so).
  const rows = useMemo(() => requestsQuery.data ?? [], [requestsQuery.data]);
  const pending = useMemo(
    () => rows.filter((r) => r.status === "pending"),
    [rows],
  );
  const sent = useMemo(() => rows.filter((r) => r.status === "sent"), [rows]);
  const rejected = useMemo(
    () => rows.filter((r) => r.status === "rejected"),
    [rows],
  );

  // Which tab is really on screen. Waiting and Sent are always offered; Rejected only exists once
  // something has been rejected, so a stale `?tab=rejected` — or the last rejected title being
  // allowed again while you look at it — falls back to Waiting rather than leaving the view blank.
  const active: RequestView =
    view === "rejected" && rejected.length === 0 ? "waiting" : view;
  const activeFull =
    active === "waiting" ? pending : active === "sent" ? sent : rejected;

  // The people offered by the "Wanted by" filter come from the tab you're on, so the counts beside
  // each name describe the list in front of you.
  const peopleOptions = useMemo(
    () => peopleOn(activeFull, nameOf),
    [activeFull, nameOf],
  );
  // A name whose titles have all been sent since you ticked it is no longer offered — filtering on
  // it would empty the list with no visible chip to un-tick. Dropping it is the same self-healing
  // the stale media filter does below.
  const activePeople = useMemo(() => {
    const offered = new Set(peopleOptions.map((p) => p.name));
    return new Set([...people].filter((name) => offered.has(name)));
  }, [people, peopleOptions]);
  const applyPeople = <T extends { wanters: string[] }>(list: T[]): T[] =>
    activePeople.size === 0
      ? list
      : list.filter((r) => (r.wanters ?? []).some((w) => activePeople.has(w)));

  // The media filter (Movies/Shows) narrows whichever list is on screen — and select-all/counts
  // follow what's visible, not the whole queue. It only applies to a list that actually mixes both
  // types: once a list is single-type its filter control is hidden, so a stale "Shows" (e.g. after
  // the shows were all sent) must fall back to "all" rather than strand the remaining movies.
  const applyMedia = <T extends { media_type: string }>(list: T[]): T[] => {
    if (media === "all") return list;
    const mixed =
      list.some((r) => r.media_type === "movie") &&
      list.some((r) => r.media_type === "show");
    return mixed ? list.filter((r) => r.media_type === media) : list;
  };
  // The rating and vote floors hide weaker titles; like the media filter they narrow what's on
  // screen (and so what select-all/counts act on).
  const ratingFloor = Number(minRating) || 0;
  const votesFloor = Number(minVotes) || 0;
  const applyThresholds = <T extends { rating: number; vote_count: number }>(
    list: T[],
  ): T[] =>
    list.filter((r) => r.rating >= ratingFloor && r.vote_count >= votesFloor);
  const narrow = (list: RequestCandidate[]) =>
    sortRequests(applyThresholds(applyPeople(applyMedia(list))), sort);
  const pendingShown = narrow(pending);
  const sentShown = narrow(sent);
  const rejectedShown = narrow(rejected);
  // Whether anything is hiding titles right now — drives the "Clear filters" control and the
  // "nothing clears these filters" note. The media split is excluded on purpose: it only renders
  // when both types are present, so it can never be the reason a list is empty.
  const filtered =
    minRating !== "0" || minVotes !== "0" || activePeople.size > 0;

  // Only visible pending rows are selectable, so an id lingering in the set after a send/reject or a
  // filter change is harmless, but scoping to what's shown keeps the count honest.
  const selectedPending = pendingShown
    .filter((r) => selected.has(r.id))
    .map((r) => r.id);
  const allChecked =
    pendingShown.length > 0 && selectedPending.length === pendingShown.length;
  const busy =
    send.isPending ||
    reject.isPending ||
    del.isPending ||
    restore.isPending ||
    clear.isPending;

  const toggleAll = () =>
    setSelected(
      allChecked ? new Set() : new Set(pendingShown.map((r) => r.id)),
    );

  const act = (mutate: () => void) => {
    mutate();
    setSelected(new Set());
  };

  return (
    <div>
      <PageHeader
        icon={Inbox}
        title="Requests"
        subtitle={
          <>
            Titles your people wanted that aren&rsquo;t in your library yet.
            Send the ones you want to Radarr or Sonarr &mdash; the apps that
            fetch films and TV for your library.
          </>
        }
      />

      {/* Whether requests are ON is a fact about the SETTING, never about whether the inbox happens
          to be empty — with the feature off and stale candidates on file, this page used to render
          the full inbox with a live Send button. Settings gets its own boundary so a cold load
          shows a skeleton rather than flashing "Requests are off" before the answer arrives. */}
      <QueryBoundary query={settingsQuery} skeleton={<RequestsSkeleton />}>
        {(settings) => {
          const requestsEnabled = settingBool(settings, "requests.enabled");
          // Whether the strongest picks go out on their own decides what the empty state can
          // promise: with this off, every qualifying title waits here instead (`requests.py`
          // queues them with the reason "auto-send is off").
          const autoSend = settingBool(settings, "requests.auto_send");
          const globalTag = settingString(settings, "requests.tag");
          const radarrUrl = settingString(settings, "requests.radarr.url");
          const sonarrUrl = settingString(settings, "requests.sonarr.url");
          return (
            <QueryBoundary
              query={requestsQuery}
              skeleton={<RequestsSkeleton />}
              isEmpty={(data) => data.length === 0}
              empty={
                requestsEnabled ? (
                  <EmptyState
                    title="Nothing waiting"
                    hint={
                      autoSend
                        ? "When a run turns up a great pick your library doesn't have, it lands here for your approval. The strongest ones are sent for you — Settings → Requests decides where that line sits."
                        : "When a run turns up a great pick your library doesn't have, it lands here for your approval. Nothing is sent without you: automatic sending is off in Settings → Requests."
                    }
                    action={
                      <Button asChild variant="outline" size="sm">
                        <Link to={SETTINGS_LINK}>
                          Go to Settings &rarr; Requests
                        </Link>
                      </Button>
                    }
                  />
                ) : (
                  <EmptyState
                    title="Requests are off"
                    hint="Turn requests on and Shortlist will notice the titles your people would have loved but your library doesn't have, and offer to fetch them through Radarr or Sonarr."
                    action={
                      <Button asChild variant="outline" size="sm">
                        <Link to={SETTINGS_LINK}>
                          Go to Settings &rarr; Requests
                        </Link>
                      </Button>
                    }
                  />
                )
              }
            >
              {() => {
                // Tabs, not a long stack: with a big queue the send log used to sit far below the
                // fold and read as missing. Waiting + Sent are always offered; Rejected appears
                // only once something's been rejected.
                const tabs: { value: RequestView; label: string }[] = [
                  {
                    value: "waiting",
                    label: `Waiting${pending.length ? ` (${pending.length})` : ""}`,
                  },
                  {
                    value: "sent",
                    label: `Sent${sent.length ? ` (${sent.length})` : ""}`,
                  },
                ];
                if (rejected.length > 0) {
                  tabs.push({
                    value: "rejected",
                    label: `Rejected (${rejected.length})`,
                  });
                }

                // The Movies/Shows split, scoped to the active tab's list — only offered when that list
                // actually mixes both types (splitting an all-movies queue helps no one).
                const movieCount = activeFull.filter(
                  (r) => r.media_type === "movie",
                ).length;
                const showCount = activeFull.length - movieCount;
                const showMediaFilter = movieCount > 0 && showCount > 0;

                return (
                  <div className="space-y-6">
                    {!requestsEnabled && <RequestsOffBanner />}

                    {/* Two levels, not one row of fifteen chips: the tab strip decides WHAT you are
                        looking at and keeps the primary highlight; the refinements below it narrow
                        that list and stay quiet. */}
                    <div className="space-y-3">
                      <Segmented
                        value={active}
                        options={tabs}
                        // Switching tabs clears every refinement so a stale "Movies" (or a name
                        // nobody on this tab carries) can't hide the list with no visible control
                        // to reset it.
                        onChange={(next) => {
                          setView(next);
                          setMedia("all");
                          clearFilters();
                        }}
                        ariaLabel="Which requests to show"
                      />
                      {(showMediaFilter || activeFull.length > 1) && (
                        <div className="space-y-2 border-b pb-3">
                          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                            {showMediaFilter && (
                              <Segmented
                                value={media}
                                onChange={setMedia}
                                ariaLabel="Filter by library"
                                options={[
                                  {
                                    value: "all",
                                    label: `All (${activeFull.length})`,
                                  },
                                  {
                                    value: "movie",
                                    label: `Movies (${movieCount})`,
                                  },
                                  {
                                    value: "show",
                                    label: `Shows (${showCount})`,
                                  },
                                ]}
                              />
                            )}
                            {activeFull.length > 1 && (
                              <>
                                <FilterSelect
                                  label="Sort"
                                  value={sort}
                                  onChange={setSort}
                                  options={SORT_OPTIONS}
                                />
                                <FilterSelect
                                  label="Rating"
                                  value={minRating}
                                  onChange={setMinRating}
                                  options={RATING_OPTIONS}
                                />
                                <FilterSelect
                                  label="Votes"
                                  value={minVotes}
                                  onChange={setMinVotes}
                                  options={VOTES_OPTIONS}
                                />
                                {filtered && (
                                  <button
                                    type="button"
                                    onClick={clearFilters}
                                    className="text-xs text-primary underline-offset-4 hover:underline focus-visible:underline"
                                  >
                                    Clear filters
                                  </button>
                                )}
                              </>
                            )}
                          </div>
                          {/* One person wanting everything is no filter at all, so the names only
                              appear once there are at least two to choose between — and only
                              alongside the other refinements, so "Clear filters" is always there
                              to undo them together. */}
                          {activeFull.length > 1 &&
                            peopleOptions.length > 1 && (
                              <PeopleFilter
                                people={peopleOptions}
                                selected={activePeople}
                                onToggle={togglePerson}
                              />
                            )}
                        </div>
                      )}
                      {/* Only when the cap is actually in play — otherwise it is a warning about a
                          limit nobody has hit. Filtering can only narrow what was loaded. */}
                      {rows.length >= MAX_LOADED && (
                        <p className="text-xs text-muted-foreground">
                          This page loads the first {MAX_LOADED} titles &mdash;
                          waiting ones first, then sent, then rejected. Filters
                          narrow those {MAX_LOADED}, so some sent or rejected
                          titles may not be on the page at all.
                        </p>
                      )}
                    </div>

                    {active === "waiting" &&
                      (pending.length > 0 ? (
                        <section className="space-y-3">
                          {/* What the tab strip never said: the tabs are three STATES, and only this
                              one is asking for a decision. Traceable — `persist_request_queue` drops
                              a pending row the library now holds, and a title only leaves Waiting
                              when it is sent, rejected or deleted. */}
                          <p className="text-sm text-muted-foreground">
                            Titles Shortlist wanted for your people that your
                            library doesn&rsquo;t have. Nothing here has been
                            sent &mdash; send the ones you want, or reject the
                            rest.
                          </p>

                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
                              <input
                                type="checkbox"
                                checked={allChecked}
                                disabled={!requestsEnabled}
                                onChange={toggleAll}
                                className="h-4 w-4 accent-primary disabled:cursor-not-allowed disabled:opacity-50"
                              />
                              {selectedPending.length > 0
                                ? `${selectedPending.length} selected`
                                : `${pendingShown.length} waiting`}
                            </label>
                            {/* Send first, and separated: it is the reason the page exists, and the
                                eye used to land on "Delete". The rule (a plain border, not a
                                Separator) keeps the two destructive actions visibly apart from it. */}
                            <div className="flex items-center gap-2">
                              <Button
                                size="sm"
                                loading={send.isPending}
                                disabled={
                                  !requestsEnabled ||
                                  selectedPending.length === 0 ||
                                  busy
                                }
                                onClick={() =>
                                  act(() =>
                                    send.mutate({ ids: selectedPending }),
                                  )
                                }
                                title="Add the selected titles to Radarr or Sonarr and start searching for them now."
                              >
                                {!send.isPending && <Send aria-hidden="true" />}
                                Send{" "}
                                {selectedPending.length > 0
                                  ? selectedPending.length
                                  : ""}{" "}
                                to Radarr/Sonarr
                              </Button>
                              <span
                                aria-hidden="true"
                                className="mx-1 h-5 w-px bg-border"
                              />
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled={
                                  !requestsEnabled ||
                                  selectedPending.length === 0 ||
                                  busy
                                }
                                onClick={() =>
                                  act(() => del.mutate(selectedPending))
                                }
                                title="Take these off the list for now. If a later run turns one up again, it comes back."
                              >
                                <Trash2 aria-hidden="true" />
                                Delete
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled={
                                  !requestsEnabled ||
                                  selectedPending.length === 0 ||
                                  busy
                                }
                                onClick={() =>
                                  act(() => reject.mutate(selectedPending))
                                }
                                title="Never ask Radarr or Sonarr for these again. They won't come back to this list."
                              >
                                <X aria-hidden="true" />
                                Reject
                              </Button>
                            </div>
                          </div>

                          {/* Always visible (not just on hover) so the Delete-vs-Reject difference is
                              never a guess — the two both clear the list but do opposite things next run. */}
                          <p className="text-xs text-muted-foreground">
                            <strong className="font-medium text-foreground">
                              Delete
                            </strong>{" "}
                            removes a title for now &mdash; it can return on a
                            later run if it&rsquo;s still wanted.{" "}
                            <strong className="font-medium text-foreground">
                              Reject
                            </strong>{" "}
                            blocks it for good &mdash; it won&rsquo;t come back.
                          </p>

                          {(send.isError || reject.isError || del.isError) && (
                            <p
                              role="alert"
                              className="text-sm text-destructive-text"
                            >
                              {apiErrorMessage(
                                send.error ?? reject.error ?? del.error,
                                "That didn't go through. Check the server log and try again.",
                              )}
                            </p>
                          )}

                          {pendingShown.length > 0 ? (
                            <div className="space-y-2">
                              {pendingShown.map((item) => (
                                <PendingRow
                                  key={item.id}
                                  item={item}
                                  checked={selected.has(item.id)}
                                  onToggle={toggle}
                                  globalTag={globalTag}
                                  disabled={!requestsEnabled}
                                  arrStatus={arrStatusQuery.data?.[item.id]}
                                  nameOf={nameOf}
                                />
                              ))}
                            </div>
                          ) : (
                            // Filtered down to nothing — say so, or the queue reads as empty when
                            // {pending.length} titles are one click away.
                            <NoMatches label="waiting" total={pending.length} />
                          )}
                        </section>
                      ) : (
                        <EmptyState
                          title="Inbox clear"
                          hint="Nothing is waiting on you right now. Titles your people would have loved, but your library doesn't have, will show up here after the next run."
                        />
                      ))}

                    {active === "sent" && (
                      <section className="space-y-3">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <h2 className="text-sm font-medium text-muted-foreground">
                            Sent to Radarr &amp; Sonarr
                          </h2>
                          {sentShown.length > 0 && (
                            <Button
                              variant="outline"
                              size="sm"
                              loading={clear.isPending}
                              disabled={busy}
                              onClick={() =>
                                clear.mutate(sentShown.map((r) => r.id))
                              }
                              title="Clear every entry shown here from the send log. The titles stay in Radarr/Sonarr and won't be asked for again."
                            >
                              {!clear.isPending && (
                                <Trash2 aria-hidden="true" />
                              )}
                              Clear all ({sentShown.length})
                            </Button>
                          )}
                        </div>
                        {clear.isError && (
                          <p role="alert" className="text-sm text-destructive-text">
                            {apiErrorMessage(
                              clear.error,
                              "That didn't go through. Check the server log and try again.",
                            )}
                          </p>
                        )}
                        {sentShown.length > 0 ? (
                          <div className="space-y-2">
                            {sentShown.map((item) => (
                              <SentRow
                                key={item.id}
                                item={item}
                                radarrUrl={radarrUrl}
                                sonarrUrl={sonarrUrl}
                                onClear={(id) => clear.mutate([id])}
                                clearing={clear.isPending}
                                arrStatus={arrStatusQuery.data?.[item.id]}
                                nameOf={nameOf}
                              />
                            ))}
                          </div>
                        ) : sent.length > 0 ? (
                          // Sent titles ARE on file, the filters are just hiding them all — saying
                          // "nothing sent yet" here would be plainly false.
                          <NoMatches label="sent" total={sent.length} />
                        ) : (
                          <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
                            Nothing sent yet. Whenever a title goes out &mdash;
                            because you approved it in Waiting, or because
                            Shortlist sent it for you &mdash; it&rsquo;s logged
                            here: the title, when it went, what the app said,
                            and who wanted it.
                          </p>
                        )}
                      </section>
                    )}

                    {active === "rejected" && rejected.length > 0 && (
                      <section className="space-y-3">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <p className="text-sm text-muted-foreground">
                            These are blocked: no run will ask Radarr or Sonarr
                            for them again.{" "}
                            <strong className="font-medium text-foreground">
                              Allow again
                            </strong>{" "}
                            moves one straight back to Waiting so you can send
                            it.
                          </p>
                          {rejectedShown.length > 0 && (
                            <Button
                              variant="outline"
                              size="sm"
                              loading={restore.isPending}
                              disabled={busy}
                              onClick={() =>
                                restore.mutate(rejectedShown.map((r) => r.id))
                              }
                              title="Move every rejected title shown here back to Waiting."
                            >
                              {!restore.isPending && (
                                <RotateCcw aria-hidden="true" />
                              )}
                              Allow all again ({rejectedShown.length})
                            </Button>
                          )}
                        </div>
                        {restore.isError && (
                          <p role="alert" className="text-sm text-destructive-text">
                            {apiErrorMessage(
                              restore.error,
                              "That didn't go through. Check the server log and try again.",
                            )}
                          </p>
                        )}
                        {rejectedShown.length > 0 ? (
                          <div className="space-y-2">
                            {rejectedShown.map((item) => (
                              <RejectedRow
                                key={item.id}
                                item={item}
                                onAllowAgain={(id) => restore.mutate([id])}
                                disabled={busy}
                                nameOf={nameOf}
                              />
                            ))}
                          </div>
                        ) : (
                          <NoMatches label="rejected" total={rejected.length} />
                        )}
                      </section>
                    )}
                  </div>
                );
              }}
            </QueryBoundary>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
