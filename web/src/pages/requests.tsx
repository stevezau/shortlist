import {
  Clapperboard,
  ExternalLink,
  Inbox,
  Loader2,
  RotateCcw,
  Send,
  Star,
  Trash2,
  TriangleAlert,
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
import { languageName } from "@/lib/request-language";
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
import type { ArrStatus, RequestCandidate } from "@/lib/types";
import { type DisplayNameLookup, displayNameLookup } from "@/lib/user-names";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/** Everything this page sends the owner to Settings for lives in one card — "Fill in the gaps
 *  automatically", under the Requests section. The hash lands them on it (`use-hash-scroll.ts`). */
const SETTINGS_LINK = "/settings#requests";

/** The server caps one inbox read at 500 rows (`api/requests.py` MAX_INBOX), sorted waiting → sent →
 *  rejected. The rating/vote/media refinements below narrow those 500; the "Wanted by" names do not
 *  — the server applies those BEFORE its cap, so a picked name reaches the whole history. Kept in
 *  step by hand: the count is only used to decide which of those two things to say. */
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
  preferredLanguages,
  languageModeOn,
}: {
  item: RequestCandidate;
  globalTag: string;
  nameOf: DisplayNameLookup;
  preferredLanguages: string[];
  languageModeOn: boolean;
}) {
  // The global tag is applied at send time and never stored on the candidate, so add it here to
  // show the full set of tags this title will actually get (deduped against the per-user/row tags).
  const tags = [...new Set([...(globalTag ? [globalTag] : []), ...item.tags])];
  return (
    <div className="space-y-1 text-sm text-muted-foreground">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <TypeBadge mediaType={item.media_type} />
        {item.year ? <span>{item.year}</span> : null}
        {/* Only shown for a title that is NOT in a preferred language, and only when a language mode
            is actually on: the chip's job is to explain why a title is being held back, and on the
            default "any" server nothing is, so it would be pure noise on every foreign tile.
            "" (unknown) draws nothing — see the `language` column note. */}
        {languageModeOn &&
        item.language &&
        !preferredLanguages.includes(item.language) ? (
          <Badge
            variant="outline"
            className="font-normal"
            data-testid="language-chip"
            title={`Original language: ${languageName(item.language)}`}
          >
            {languageName(item.language)}
          </Badge>
        ) : null}
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

/** TMDB's synopsis, clamped to three lines — enough to decide on a title you've never heard of,
 *  which is the whole reason it's here (discussion #87). No expander: if three lines don't settle
 *  it, the TMDB link directly below is the better next step than more text in a triage list.
 *
 *  Renders nothing at all when there's no synopsis (a pre-0071 row awaiting its next run, or a
 *  title TMDB has none for) — an empty paragraph would leave a gap that reads like a loading state.
 */
function Synopsis({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <p className="line-clamp-3 text-sm text-muted-foreground" title={text}>
      {text}
    </p>
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
  {
    label: string;
    variant: "success" | "default" | "secondary" | "warning";
    hint?: string;
  }
> = {
  downloaded: { label: "Downloaded", variant: "success" },
  downloading: { label: "Downloading", variant: "default" },
  queued: { label: "Searching", variant: "secondary" },
  // Amber, because nothing is coming and only a person can change that. It used to have exactly one
  // cause — somebody unmonitored it by hand — so the colour was the whole message. "How much of a
  // show to grab" set to None now produces the same state on purpose, so the badge has to say which
  // it might be rather than leaving a warning colour to imply something went wrong.
  unmonitored: {
    label: "Not monitored",
    variant: "warning",
    hint: "Sonarr or Radarr has this title but isn't looking for it, so nothing will download until it's monitored there. If you set “How much of a show to grab” to None — either in Settings › Requests or on the row itself — this is that working as asked.",
  },
};

/**
 * Which of the four things the Arr column can be saying about one title.
 *
 * Three of these used to render as the SAME nothing. The badge only knew a status string, so a
 * lookup still in flight, an Arr that never answered, and a title genuinely absent from both apps
 * were indistinguishable on screen — and since the query fetched once with no polling, "in flight"
 * and "never answered" were both states you could sit in indefinitely with no way to tell.
 */
type ArrView =
  | { kind: "checking" }
  | { kind: "unreachable"; app: string }
  | { kind: "status"; status: string }
  | { kind: "none" };

function ArrStatusBadge({ view }: { view: ArrView }) {
  if (view.kind === "checking") {
    return (
      <Badge variant="secondary" className="gap-1.5 font-normal">
        {/* `motion-reduce:animate-none` — a spinner is decoration, and the word carries the meaning
            on its own for anyone who has asked for less movement. */}
        <Loader2
          aria-hidden="true"
          className="h-3 w-3 animate-spin motion-reduce:animate-none"
        />
        Checking…
      </Badge>
    );
  }
  if (view.kind === "unreachable") {
    return (
      <Badge
        variant="warning"
        className="gap-1.5"
        title={`Shortlist couldn't reach ${view.app}, so it can't say what state this title is in there. Check ${view.app} is running and that its URL and API key are right in Settings → Requests.`}
      >
        <TriangleAlert aria-hidden="true" className="h-3 w-3" />
        Can&rsquo;t reach {view.app}
      </Badge>
    );
  }
  if (view.kind === "none") return null;
  const shown = ARR_STATUS_LABELS[view.status];
  if (!shown) return null;
  return (
    <Badge variant={shown.variant} title={shown.hint}>
      {shown.label}
    </Badge>
  );
}

function PendingRow({
  item,
  checked,
  onToggle,
  globalTag,
  preferredLanguages,
  languageModeOn,
  disabled,
  arrView,
  nameOf,
  onSend,
  onDelete,
  onReject,
  busy,
  sending,
}: {
  item: RequestCandidate;
  checked: boolean;
  onToggle: (id: number) => void;
  globalTag: string;
  preferredLanguages: string[];
  languageModeOn: boolean;
  /** Requests are off — the row is still readable, but it cannot be selected for sending. */
  disabled: boolean;
  arrView: ArrView;
  nameOf: DisplayNameLookup;
  /** Decide this one title without touching the selection — the toolbar stays for batches. */
  onSend: (id: number) => void;
  onDelete: (id: number) => void;
  onReject: (id: number) => void;
  /** A mutation is in flight somewhere on the page; every row's buttons wait it out. */
  busy: boolean;
  /** THIS row's Send is the one in flight — the spinner belongs on the button that was clicked, not
   *  on the toolbar's, which may be scrolled off the top of a long queue. */
  sending: boolean;
}) {
  const app = item.media_type === "movie" ? "Radarr" : "Sonarr";
  return (
    // A div, not the <label> this used to be: a <button> is a labelable element, so a label may not
    // contain one — the row now has three.
    //
    // Re-creating click-anywhere-to-select by hand means re-creating the rule the label gave us for
    // free: "the activation behavior of a label element for events targeted at interactive content
    // descendants … must be to do nothing" (HTML spec). Without the guard below, opening TMDB to
    // read up on an unfamiliar title silently ticks that row, and the next toolbar Reject takes a
    // title nobody chose. Filter on the target, not per-child `stopPropagation` — every link, badge
    // and expander added to this card later would each have to remember to opt out.
    <div
      onClick={(e) => {
        if ((e.target as HTMLElement).closest("a,button,input,select,textarea"))
          return;
        if (!disabled) onToggle(item.id);
      }}
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
        aria-label={`Select ${item.title}`}
        onChange={() => onToggle(item.id)}
        className="mt-1.5 h-4 w-4 shrink-0 accent-primary disabled:cursor-not-allowed disabled:opacity-50"
      />
      <Poster item={item} />
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <p className="text-base font-semibold leading-tight">{item.title}</p>
          <ArrStatusBadge view={arrView} />
        </div>
        <TitleMeta
          item={item}
          globalTag={globalTag}
          nameOf={nameOf}
          preferredLanguages={preferredLanguages}
          languageModeOn={languageModeOn}
        />
        <Synopsis text={item.overview} />
        <WhyBreakdown why={item.why} nameOf={nameOf} />
        <ExternalLinks item={item} />
        {/* Deliberately does NOT promise the row disappears next run: the tidy-up matches shows by
            the TMDB id Sonarr v4 reports, and Sonarr v3 doesn't report one at all (`_apply_arr_state`,
            `arr_present`), so on v3 a show sitting in Sonarr stays in this list. */}
        {/* Only for a real status — "checking" and "couldn't reach it" are not evidence the title
            is already there, and this sentence tells you not to send it. */}
        {arrView.kind === "status" ? (
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
          // This component only ever renders PENDING rows, so there is no "last attempt" branch to
          // take. The detail holds either the threshold keeping a title waiting or the failure of a
          // send that was tried — "last recorded reason" is true of both, and does not assert that a
          // stale failure is still the current cause.
          <p className="text-xs text-muted-foreground">
            Last recorded reason: {item.detail}
          </p>
        ) : null}
        {/* Decide this title on its own. The toolbar above still handles batches — these exist for
            the other way through the list, one unfamiliar title at a time, which is what the inbox
            actually looks like on most nights. Same variants and the same dividing rule as the
            toolbar, so Send-vs-the-destructive-pair reads identically in both places. */}
        <div
          role="group"
          aria-label={`Actions for ${item.title}`}
          className="flex flex-wrap items-center justify-end gap-1 pt-1"
        >
          <Button
            size="sm"
            variant="outline"
            loading={sending}
            disabled={disabled || busy}
            onClick={() => onSend(item.id)}
            title={`Add ${item.title} to ${app} and start searching for it now.`}
          >
            {!sending && <Send aria-hidden="true" />}
            Send
          </Button>
          <span aria-hidden="true" className="mx-1 h-5 w-px bg-border" />
          <Button
            size="sm"
            variant="ghost"
            disabled={disabled || busy}
            onClick={() => onDelete(item.id)}
            title="Take this off the list for now. If a later run turns it up again, it comes back."
          >
            <Trash2 aria-hidden="true" />
            Delete
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={disabled || busy}
            onClick={() => onReject(item.id)}
            title={`Never ask ${app} for this again. It won't come back to this list.`}
          >
            <X aria-hidden="true" />
            Reject
          </Button>
        </div>
      </div>
    </div>
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
  arrView,
  nameOf,
}: {
  item: RequestCandidate;
  radarrUrl: string;
  sonarrUrl: string;
  onClear: (id: number) => void;
  clearing: boolean;
  arrView: ArrView;
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
            <ArrStatusBadge view={arrView} />
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
/** `count` is how many titles ON THIS TAB they wanted, and 0 means "none here", NOT "none ever" —
 *  which is why the picker shows a count only when there is one. */
type PersonOption = { name: string; label: string; count: number };

/**
 * Everyone you could filter by: your whole Plex roster, plus anyone named on a title who is no
 * longer on it (someone since removed still explains why a title is in the inbox).
 *
 * Built from the users list rather than inferred from the titles on screen. Inferring it meant a
 * person whose requests were all older than the newest `MAX_LOADED` never appeared as an option —
 * and the server-side filter would have found their titles perfectly well if only you could pick
 * their name. The list you choose from must not be limited by the page you happen to be looking at.
 *
 * Sorted by titles on this tab so the people you are most likely to want are at the top, then
 * alphabetically — which is also the order the whole roster falls into once counts run out.
 */
function peopleOn(
  list: RequestCandidate[],
  nameOf: DisplayNameLookup,
  usernames: string[],
): PersonOption[] {
  const counts = new Map<string, number>();
  for (const name of usernames) counts.set(name, 0);
  for (const item of list) {
    for (const name of item.wanters ?? []) {
      counts.set(name, (counts.get(name) ?? 0) + 1);
    }
  }
  return [...counts]
    .map(([name, count]) => ({ name, label: nameOf(name), count }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

/** How many matches the list shows at once. Past this you type another letter rather than scroll —
 *  a list long enough to scroll is the wall this control replaced. */
const PEOPLE_RESULTS = 8;

/**
 * "Wanted by": pick one person, or several, to see only what they asked for — the answer to "what do
 * I need to grab to get this new person up and running" (issue #61).
 *
 * Type to find someone; picked people become chips you can take off again. It was a row of chips for
 * everybody, which is fine for four sharers and unusable for forty: a real server showed eight names
 * and "+35 more people", so finding one person meant expanding the wall and reading all forty-three.
 *
 * One control for both sizes rather than two: with the box empty and focused it lists everyone (up to
 * PEOPLE_RESULTS), so a small server sees its whole roster the moment it clicks, and a large one
 * narrows by typing. Nobody picked still means everybody, so the filter starts out of the way.
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
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const labelId = useId();
  const listId = useId();

  const chosen = people.filter((p) => selected.has(p.name));
  const q = query.trim().toLowerCase();
  // Matches on the username as well as the shown name: someone searching for the Plex login they
  // invited the person under should still find them.
  const matches = people.filter(
    (p) =>
      !q ||
      p.label.toLowerCase().includes(q) ||
      p.name.toLowerCase().includes(q),
  );
  const visible = matches.slice(0, PEOPLE_RESULTS);
  const hidden = matches.length - visible.length;

  const pick = (name: string) => {
    onToggle(name);
    setQuery("");
    setActive(0);
  };

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <span id={labelId} className="text-xs text-muted-foreground">
        Wanted by
      </span>

      {chosen.map((person) => (
        <span
          key={person.name}
          className="inline-flex items-center gap-1 rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground"
        >
          {person.count ? `${person.label} (${person.count})` : person.label}
          <button
            type="button"
            onClick={() => onToggle(person.name)}
            aria-label={`Stop filtering by ${person.label}`}
            className="rounded-sm opacity-70 hover:opacity-100 focus-visible:opacity-100"
          >
            <X className="h-3 w-3" aria-hidden="true" />
          </button>
        </span>
      ))}

      <div className="relative">
        <Input
          type="search"
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-labelledby={labelId}
          aria-autocomplete="list"
          aria-activedescendant={
            open && visible[active] ? `${listId}-${active}` : undefined
          }
          autoComplete="off"
          className="h-8 w-48 text-sm"
          placeholder={
            chosen.length ? "Add another person…" : "Search for a person…"
          }
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          // A blur that lands on an option must not close the list before the click registers.
          onBlur={(e) => {
            if (!e.currentTarget.parentElement?.contains(e.relatedTarget)) {
              setOpen(false);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              setOpen(true);
              setActive((i) => {
                const step = e.key === "ArrowDown" ? 1 : -1;
                const next = i + step;
                return next < 0 ? visible.length - 1 : next % visible.length;
              });
            } else if (e.key === "Enter" && open && visible[active]) {
              e.preventDefault();
              pick(visible[active].name);
            } else if (e.key === "Escape") {
              setOpen(false);
              setQuery("");
            } else if (e.key === "Backspace" && !query) {
              // The usual token-field behaviour: backspace on an empty box takes the last one off.
              const last = chosen[chosen.length - 1];
              if (last) onToggle(last.name);
            }
          }}
        />

        {open && (
          <ul
            id={listId}
            role="listbox"
            className="absolute z-20 mt-1 max-h-72 w-64 overflow-y-auto rounded-md border bg-popover p-1 shadow-lg"
          >
            {visible.length === 0 ? (
              <li className="px-2 py-1.5 text-sm text-muted-foreground">
                Nobody here matches &ldquo;{query.trim()}&rdquo;.
              </li>
            ) : (
              visible.map((person, i) => (
                // The option IS the list item. A `<button role="option">` nested inside a plain
                // `<li>` breaks the listbox's ownership of its options, so assistive tech — and
                // `getByRole("option")` — stop seeing them.
                <li
                  key={person.name}
                  id={`${listId}-${i}`}
                  role="option"
                  // Spelled out rather than left to the contents: the name and the count are
                  // adjacent spans, so the computed name came out as "Sarah2" — which is what a
                  // screen reader would have said, too.
                  // No count for somebody with nothing on this tab: "0" would read as "has never
                  // asked for anything", when it only means "nothing of theirs is on this tab".
                  aria-label={
                    person.count
                      ? `${person.label}, ${person.count} ${person.count === 1 ? "title" : "titles"}`
                      : person.label
                  }
                  aria-selected={selected.has(person.name)}
                  onMouseEnter={() => setActive(i)}
                  onMouseDown={(e) => e.preventDefault()} // keep focus in the box
                  onClick={() => pick(person.name)}
                  className={cn(
                    "flex cursor-pointer items-center justify-between gap-3 rounded-sm px-2 py-1.5 text-sm",
                    i === active && "bg-accent text-accent-foreground",
                  )}
                >
                  <span className="min-w-0 truncate">
                    {selected.has(person.name) && "✓ "}
                    {person.label}
                  </span>
                  {person.count > 0 && (
                    <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                      {person.count}
                    </span>
                  )}
                </li>
              ))
            )}
            {hidden > 0 && (
              <li className="px-2 py-1.5 text-xs text-muted-foreground">
                {hidden} more &mdash; keep typing to narrow it down.
              </li>
            )}
          </ul>
        )}
      </div>
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

/**
 * What one title's Arr column should say, from the live query and that title's kind.
 *
 * Keyed on media type because the apps fail independently: a dead Sonarr must not put "can't reach
 * Radarr" on a film, which is the same one-app-must-not-blank-the-other rule the endpoint follows.
 */
function arrViewFor(
  item: RequestCandidate,
  status: ArrStatus | undefined,
  isPending: boolean,
): ArrView {
  const isMovie = item.media_type === "movie";
  if (isPending) return { kind: "checking" };
  if (!status) return { kind: "none" }; // the fetch failed outright; the page says so elsewhere
  const reach = isMovie ? status.radarr : status.sonarr;
  if (reach === "unreachable") {
    return { kind: "unreachable", app: isMovie ? "Radarr" : "Sonarr" };
  }
  const found = status.statuses[String(item.id)];
  return found ? { kind: "status", status: found } : { kind: "none" };
}

export function RequestsPage() {
  const requestsQuery = useRequests();
  const settingsQuery = useSettings();
  const arrStatusQuery = useArrStatus();
  // `isPending` is the FIRST load only — a background refetch keeps the last answer on screen, so a
  // settled badge never flickers back to "Checking…" every time the poll comes round.
  const arrView = (item: RequestCandidate): ArrView =>
    arrViewFor(item, arrStatusQuery.data, arrStatusQuery.isPending);
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

  // Everyone on the server is offered; the counts beside them describe the tab you're on. Offering
  // only the names found on the page meant somebody whose requests were all older than the 500 this
  // page loads could never be picked — see `peopleOn`.
  const usernames = useMemo(
    () => (usersQuery.data ?? []).map((u) => u.username).filter(Boolean),
    [usersQuery.data],
  );
  const peopleOptions = useMemo(
    () => peopleOn(activeFull, nameOf, usernames),
    [activeFull, nameOf, usernames],
  );
  // A name whose titles have all been sent since you ticked it is no longer offered — filtering on
  // it would empty the list with no visible chip to un-tick. Dropping it is the same self-healing
  // the stale media filter does below.
  const activePeople = useMemo(() => {
    const offered = new Set(peopleOptions.map((p) => p.name));
    return new Set([...people].filter((name) => offered.has(name)));
  }, [people, peopleOptions]);

  // The names go to the SERVER, which applies them before its 500-row cap — so picking someone
  // reaches titles the read above never loaded, which is the whole point of the filter ("what does
  // this new person still need?"). With nobody picked this is the same query key as `requestsQuery`,
  // so the page still makes one request, not two.
  const filteredQuery = useRequests([...activePeople]);
  // What the lists on screen are built from. Until that answer lands — and if it fails — this stays
  // the loaded page, which `applyPeople` below narrows client-side exactly as it did before, so a
  // chip never blanks the list while it waits and never leaks someone else's titles into it.
  const listRows = filteredQuery.data ?? rows;

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
  // Built from the server's answer, not from `pending`/`sent`/`rejected` — those stay the loaded
  // page, because the tab counts and the "Wanted by" roster have to keep describing the whole inbox
  // rather than the slice a picked name narrowed it to.
  const pendingRows = listRows.filter((r) => r.status === "pending");
  const sentRows = listRows.filter((r) => r.status === "sent");
  const rejectedRows = listRows.filter((r) => r.status === "rejected");
  const pendingShown = narrow(pendingRows);
  const sentShown = narrow(sentRows);
  const rejectedShown = narrow(rejectedRows);

  // The count beside a PICKED name is re-read from the server's answer, which isn't capped to this
  // page — otherwise the chip could say "(12)" beside a list of forty of that person's titles.
  // Unpicked names keep the loaded page's count; nothing better exists until they're picked.
  const activeShown =
    active === "waiting"
      ? pendingRows
      : active === "sent"
        ? sentRows
        : rejectedRows;
  const exactCounts = new Map(
    peopleOn(activeShown, nameOf, usernames).map((p) => [p.name, p.count]),
  );
  const peopleChips = peopleOptions.map((p) =>
    activePeople.has(p.name)
      ? { ...p, count: exactCounts.get(p.name) ?? p.count }
      : p,
  );
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

  // A per-row decision keeps the batch someone is assembling, but must still drop the title it just
  // decided. `useSendRequests.onSuccess` doesn't return its invalidation, so `busy` goes false when
  // the POST resolves — before the refetched list arrives. In that window the row is still listed as
  // pending, so leaving its id in `selected` lets the toolbar's Reject land on a title that was just
  // sent (unlike /delete, /reject doesn't exclude sent rows), stamping it rejected with a sent_at.
  const decide = (id: number, mutate: () => void) => {
    mutate();
    setSelected((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  };

  return (
    <div>
      <PageHeader
        icon={Inbox}
        title="Requests"
        subtitle="Titles your people wanted that aren’t in your library yet. Send the ones you want to Radarr or Sonarr."
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
          // Which languages count as "the owner's". Read once here rather than per tile, so one bad
          // stored value cannot make every card render a chip it shouldn't.
          const rawLanguages = settings?.["requests.preferred_languages"];
          const preferredLanguages = Array.isArray(rawLanguages)
            ? rawLanguages
                .filter((c): c is string => typeof c === "string")
                .map((c) => c.trim().toLowerCase())
            : ["en"];
          // The chip explains a HOLD, so it only earns its place when a mode is actually holding
          // things back. On the default "any" server nothing is.
          const languageModeOn =
            settingString(settings, "requests.language_mode", "any") !== "any";
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
                  // The hint used to restate the page subtitle directly above it in different
                  // words. An empty state's job is to say what to do next, not to re-introduce the
                  // feature the reader just read about.
                  <EmptyState
                    title="Requests are off"
                    hint="Switch them on and missing titles start collecting here."
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

                // The page limit says one of two different things, and must not say the wrong one.
                // Unpicked, the read really is capped and some history is off the page. Once a name
                // is picked the server re-reads the whole history for those people BEFORE capping,
                // so the limit no longer describes what's on screen — unless their own titles fill
                // it. Until that answer arrives (or if it failed) the list is still the loaded page
                // narrowed here, so the unfiltered note stands.
                const namesServed =
                  activePeople.size > 0 && filteredQuery.data !== undefined;
                const picked =
                  activePeople.size === 1 ? "the name" : "the names";
                const capNote =
                  rows.length < MAX_LOADED
                    ? null
                    : !namesServed
                      ? `This page loads the first ${MAX_LOADED} titles — waiting ones first, then sent, then rejected — so some sent or rejected titles may not be on it at all. Pick a name under “Wanted by” to search every title on file for that person instead.`
                      : listRows.length >= MAX_LOADED
                        ? `Showing the first ${MAX_LOADED} titles for ${picked} you picked.`
                        : `Showing every title on file for ${picked} you picked — not just the ${MAX_LOADED} this page loads.`;

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
                                people={peopleChips}
                                selected={activePeople}
                                onToggle={togglePerson}
                              />
                            )}
                        </div>
                      )}
                      {/* Only when the cap is actually in play — otherwise it is a note about a
                          limit nobody has hit. Wording decided above. */}
                      {capNote && (
                        <p className="text-xs text-muted-foreground">
                          {capNote}
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
                            {/* Named, because the page now has two Delete buttons and two Rejects —
                                this one acts on the ticked rows, the one on each card acts on that
                                card. Reading "Reject" alone, they are indistinguishable. */}
                            <div
                              role="group"
                              aria-label="Actions for the selected titles"
                              className="flex items-center gap-2"
                            >
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
                                  preferredLanguages={preferredLanguages}
                                  languageModeOn={languageModeOn}
                                  disabled={!requestsEnabled}
                                  arrView={arrView(item)}
                                  nameOf={nameOf}
                                  busy={busy}
                                  // `decide`, not `act`: a per-row decision must not clear a batch
                                  // the owner had half-assembled in the checkboxes.
                                  onSend={(id) =>
                                    decide(id, () => send.mutate({ ids: [id] }))
                                  }
                                  onDelete={(id) =>
                                    decide(id, () => del.mutate([id]))
                                  }
                                  onReject={(id) =>
                                    decide(id, () => reject.mutate([id]))
                                  }
                                  sending={
                                    send.isPending &&
                                    send.variables?.ids?.length === 1 &&
                                    send.variables.ids[0] === item.id
                                  }
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
                          <p
                            role="alert"
                            className="text-sm text-destructive-text"
                          >
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
                                arrView={arrView(item)}
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
                          <p
                            role="alert"
                            className="text-sm text-destructive-text"
                          >
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
