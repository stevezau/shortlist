import {
  ChevronRight,
  Clapperboard,
  ExternalLink,
  Inbox,
  Loader2,
  MoreHorizontal,
  RotateCcw,
  Search,
  Send,
  Star,
  TriangleAlert,
  Users,
  X,
} from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import {
  ImdbGlyph,
  RadarrGlyph,
  SonarrGlyph,
  TmdbGlyph,
  TraktGlyph,
} from "@/components/brand-glyphs";
import { TitlePoster } from "@/components/title-poster";
import { Why } from "@/components/why";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Link } from "react-router";

import { formatDate } from "@/lib/format";
import { type TitleLink, titleLinks } from "@/lib/title-links";
import { languageName } from "@/lib/request-language";
import { sourceShortLabel } from "@/lib/sources";
import type { ArrStatus, RequestCandidate } from "@/lib/types";
import type { DisplayNameLookup } from "@/lib/user-names";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/** Everything this page sends the owner to Settings for lives in one card — "Fill in the gaps
 *  automatically", under the Requests section. The hash lands them on it (`use-hash-scroll.ts`). */
export const SETTINGS_LINK = "/settings#requests";

/** The server caps one inbox read at 500 rows (`api/requests.py` MAX_INBOX), sorted waiting → sent →
 *  rejected. The rating/vote/media refinements below narrow those 500; the "Wanted by" names do not
 *  — the server applies those BEFORE its cap, so a picked name reaches the whole history. Kept in
 *  step by hand: the count is only used to decide which of those two things to say. */
export const MAX_LOADED = 500;

export function RequestsSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 5 }, (_, i) => (
        <Skeleton key={i} className="h-16 w-full" />
      ))}
    </div>
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

const LINK_GLYPHS: Record<TitleLink["label"], ReactNode> = {
  TMDB: <TmdbGlyph className="h-3.5 w-3.5 rounded-[2px]" />,
  IMDb: <ImdbGlyph className="h-3.5 w-3.5 rounded-[2px]" />,
  Trakt: <TraktGlyph className="h-3.5 w-3.5" />,
};

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
  const links: QuickLink[] = [
    ...lead,
    ...titleLinks(item).map((link) => ({ label: link.label, icon: LINK_GLYPHS[link.label], href: link.href })),
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
export function WhyBreakdown({
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

/** Full synopsis inside the title's disclosure; older candidates may not have one recorded. */
function Synopsis({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <p className="text-sm leading-relaxed text-muted-foreground" title={text}>
      {text}
    </p>
  );
}

/**
 * Requests are off, but titles queued before that are still on file. The inbox stays readable —
 * hiding it would lose them — but nothing here can be acted on, and it has to say so, because the live
 * "Send to Sonarr/Radarr" button would otherwise look exactly as it does when the feature is on.
 */
export function RequestsOffBanner() {
  return (
    <div className="space-y-2 rounded-lg border border-dashed bg-muted/30 p-4">
      <p className="text-sm font-medium">Requests are off</p>
      <p className="text-sm text-muted-foreground">
        These titles were found before you turned requests off. Nothing new is
        added while it stays off, Shortlist isn&rsquo;t asking your download
        apps for anything, and nothing here can be sent or rejected until you
        turn it back on.
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
  // Overseerr-route only — no Arr ever reports it. Amber like `unmonitored` and for the same reason:
  // nothing is coming until a person acts, and that person is the one reading this.
  awaiting_approval: {
    label: "Waiting for approval",
    variant: "warning",
    hint: "Shortlist filed this request and Overseerr is holding it for someone to approve. Approve it there and it will go to Radarr or Sonarr — or change who requests go out as, in Settings › Requests, if you would rather they were approved automatically.",
  },
  // Amber, because nothing is coming and only a person can change that. It has two causes —
  // somebody unmonitored it by hand, or "How much of a show to grab" is set to None on purpose — so
  // the badge has to say which it might be rather than leaving a warning colour to imply something went wrong.
  unmonitored: {
    label: "Not monitored",
    variant: "warning",
    hint: "Sonarr or Radarr has this title but isn't looking for it, so nothing will download until it's monitored there. If you set “How much of a show to grab” to None — either in Settings › Requests or on the row itself — this is that working as asked.",
  },
};

/**
 * Which of the four things the Arr column can be saying about one title.
 *
 * Three of these would render as the SAME nothing if the badge only knew a status string: a
 * lookup still in flight, an Arr that never answered, and a title genuinely absent from both apps
 * are indistinguishable on screen, and without polling, "in flight" and "never answered" are both
 * states you could sit in indefinitely with no way to tell.
 */
export type ArrView =
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
    // `max-w-full` + `min-w-0` + `flex-wrap` on the wrapper, because the disclosure's paragraph is
    // a flex item here. MEASURED against the built stylesheet at 320/390/1024/1280: without them
    // it is squeezed into a 111px column and stacks 312px tall at 320px; with them it takes the
    // row's width and is 126px. Neither version scrolls the page sideways.
    return (
      <span className="inline-flex min-w-0 max-w-full flex-wrap items-baseline">
        <Badge variant="warning" className="gap-1.5">
          <TriangleAlert aria-hidden="true" className="h-3 w-3" />
          Can&rsquo;t reach {view.app}
        </Badge>
        <Why
          text={`Shortlist couldn't reach ${view.app}, so it can't say what state this title is in there. Check ${view.app} is running and that its URL and API key are right in Settings → Requests.`}
        />
      </span>
    );
  }
  if (view.kind === "none") return null;
  const shown = ARR_STATUS_LABELS[view.status];
  if (!shown) return null;
  // Downloaded / Downloading / Searching say everything in their label, so they stay a bare badge
  // and the layout around them is untouched.
  if (!shown.hint) return <Badge variant={shown.variant}>{shown.label}</Badge>;
  // The two that DON'T — "Waiting for approval" and "Not monitored", the two statuses an owner most
  // needs explained — carried 246- and 244-character remedies in a `title` and nowhere else:
  // hover-only on a desktop, unreachable on a phone. `Why` is the app's existing answer to a long
  // explanation that must stay reachable without taking a line, and it is a real button, so touch
  // and keyboard both work.
  return (
    <span className="inline-flex min-w-0 max-w-full flex-wrap items-baseline">
      <Badge variant={shown.variant}>{shown.label}</Badge>
      <Why text={shown.hint} />
    </span>
  );
}

/**
 * The "⋯" beside a title's Send button: the two ways to take a title off the list, each saying what
 * it does in the menu itself. A native `<details>` so it works with no script, closed again by a
 * pick, an outside click or Escape.
 */
function RowMoreMenu({
  title,
  disabled,
  onReject,
  onDismiss,
}: {
  title: string;
  disabled: boolean;
  onReject: () => void;
  onDismiss: () => void;
}) {
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (e: Event) => {
      const menu = ref.current;
      if (!menu?.open) return;
      if (e instanceof KeyboardEvent && e.key !== "Escape") return;
      if (e instanceof MouseEvent && menu.contains(e.target as Node)) return;
      menu.open = false;
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, []);
  const pick = (action: () => void) => () => {
    if (ref.current) ref.current.open = false;
    action();
  };
  return (
    <details ref={ref} className="relative">
      <Button
        asChild
        variant="outline"
        size="icon"
        className="cursor-pointer"
      >
        <summary
          aria-label={`More actions for ${title}`}
          className="list-none [&::-webkit-details-marker]:hidden"
        >
          <MoreHorizontal aria-hidden="true" />
        </summary>
      </Button>
      <div className="absolute right-0 top-10 z-20 w-72 rounded-lg border border-border-strong bg-elevated py-1 text-left shadow-elevated">
        <button
          type="button"
          disabled={disabled}
          onClick={pick(onReject)}
          className="block w-full px-3 py-2 text-left text-sm font-medium text-destructive-text hover:bg-raised disabled:cursor-not-allowed disabled:opacity-50"
        >
          Reject &mdash; never suggest it again
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={pick(onDismiss)}
          className="block w-full px-3 py-2 text-left text-sm font-medium hover:bg-raised disabled:cursor-not-allowed disabled:opacity-50"
        >
          Dismiss &mdash; remove it for now, may come back
        </button>
      </div>
    </details>
  );
}

/** The opening words of a hold's reason, as `request_holds.HOLD_REASON_PREFIX` writes them. */
const HELD_PREFIX = "held by your request filter";

export function PendingRow({
  item,
  viaSeerr,
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
  /** True when requests route through Overseerr, which answers for films and shows alike. */
  viaSeerr: boolean;
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
  // Named from the ROUTE, not from the media type: the "already in {app}" line below is an
  // explanation of a live status reading, and on the Overseerr route it was explaining a title's
  // presence in an app nothing had asked.
  const app = viaSeerr
    ? "Overseerr"
    : item.media_type === "movie"
      ? "Radarr"
      : "Sonarr";
  return (
    // A div, not a <label>: a <button> is a labelable element, so a label may not
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
        if ((e.target as HTMLElement).closest("a,button,input,select,textarea,summary,details"))
          return;
        if (!disabled) onToggle(item.id);
      }}
      className={cn(
        "flex cursor-pointer items-start gap-3 px-4 py-4 transition-colors",
        // Selection is what the whole toolbar acts on, so a picked card says so on the card itself —
        // a 4px checkbox was the only difference between "will be sent" and "won't".
        checked
          ? "bg-raised/40"
          : "hover:bg-muted/50",
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
      <TitlePoster posterPath={item.poster_path} className="sm:h-20 sm:w-[54px]" />
      <div className="grid min-w-0 flex-1 grid-cols-1 gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0 space-y-2">
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
        </div>
        <details className="group order-3 sm:col-span-2">
          <summary className="flex w-fit cursor-pointer items-center gap-1.5 rounded-sm text-xs font-medium text-muted-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring list-none [&::-webkit-details-marker]:hidden"><ChevronRight aria-hidden="true" className="h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-90 motion-reduce:transition-none" />Details & title links</summary>
          <div className="mt-3 grid gap-5 border-t pt-4 sm:grid-cols-[minmax(0,1fr)_15rem]">
            <div className="space-y-3"><p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">The story</p><Synopsis text={item.overview} /><ExternalLinks item={item} /></div>
            <div className="space-y-3 sm:border-l sm:pl-5"><p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Why it’s here</p><WhyBreakdown why={item.why} nameOf={nameOf} />{item.detail && <p className="text-xs text-muted-foreground">Last recorded reason: {item.detail}</p>}</div>
          </div>
        </details>
        {/* Deliberately does NOT promise the row disappears next run: the tidy-up matches shows by
            the TMDB id Sonarr v4 reports, and Sonarr v3 doesn't report one at all (`_apply_arr_state`,
            `arr_present`), so on v3 a show sitting in Sonarr stays in this list. */}
        {/* Only for a real status — "checking" and "couldn't reach it" are not evidence the title
            is already there, and this sentence tells you not to send it. */}
        {arrView.kind === "status" ? (
          <p className="order-4 text-xs text-muted-foreground sm:col-span-2">
            Already in {app} &mdash; it was added there after it landed here, so
            you don&rsquo;t need to send it again.
          </p>
        ) : null}
        {/* A weak claim, on purpose: nothing here proves the Arr refuses a hand-made
            add, only that `request_missing` never auto-sends an excluded title. */}
        {item.excluded ? (
          <p className="order-4 text-xs text-warning sm:col-span-2">
            {app} was told never to fetch this again &mdash; usually left behind
            by deleting it there ({app} calls it {viaSeerr ? "a" : "an"}{" "}
            {/* The CONCEPT is route-aware too, not just the app's name. Naming Overseerr and then
                calling its blocklist an "import exclusion" sends the owner looking for a screen it
                does not have — the same failure the app-name fix was made for, one word deeper. */}
            {viaSeerr ? "blocklist" : "import exclusion"}). Shortlist never
            sends it for you; clear it in {app} if you want it back.
          </p>
        ) : null}
        {/* The owner's "don't request these automatically" picks held it (`request_holds`). Sending it from
            here is the way through, so the note says so rather than reading like an error. */}
        {item.detail?.startsWith(HELD_PREFIX) ? (
          <p className="order-4 text-xs text-warning sm:col-span-2">
            Held by your request filter ({item.detail.slice(HELD_PREFIX.length).replace(/^\s*—\s*/, "")}), so it
            won&rsquo;t be sent automatically. Send it yourself if you want it.
          </p>
        ) : null}
        {/* Decide this title on its own. The toolbar above still handles batches — these exist for
            the other way through the list, one unfamiliar title at a time, which is what the inbox
            actually looks like on most nights. Same variants and the same dividing rule as the
            toolbar, so Send-vs-the-destructive-pair reads identically in both places. */}
        <div
          role="group"
          aria-label={`Actions for ${item.title}`}
          className="order-2 flex flex-wrap items-center gap-2 self-center sm:col-start-2 sm:row-start-1 sm:justify-end"
        >
          {/* Outline, not filled: the page's one amber control is the bulk bar's Send. */}
          <Button
            size="sm"
            variant="outline"
            loading={sending}
            disabled={disabled || busy}
            onClick={() => onSend(item.id)}
            title={`Add ${item.title} to ${app} and start searching for it now.`}
          >
            {!sending && <Send aria-hidden="true" />}
            Send to {app}
          </Button>
          <RowMoreMenu
            title={item.title}
            disabled={disabled || busy}
            onReject={() => onReject(item.id)}
            onDismiss={() => onDelete(item.id)}
          />
        </div>
      </div>
    </div>
  );
}

/** The send log: a title that went to Sonarr or Radarr — which app, when it went, the app's answer,
 *  a link straight into that app, and why it was wanted. */
export function SentRow({
  item,
  radarrUrl,
  sonarrUrl,
  overseerrUrl,
  onClear,
  clearing,
  arrView,
  nameOf,
}: {
  item: RequestCandidate;
  radarrUrl: string;
  sonarrUrl: string;
  /** Set only when requests route through Overseerr — which then answers for films AND shows. */
  overseerrUrl: string;
  onClear: (id: number) => void;
  clearing: boolean;
  arrView: ArrView;
  nameOf: DisplayNameLookup;
}) {
  const isMovie = item.media_type === "movie";
  // Naming the app is not cosmetic: the badge is a record of where this title actually went, and
  // on the Overseerr route it read "Sent to Radarr" for every film — an app that was never asked.
  const viaSeerr = Boolean(overseerrUrl);
  const app = viaSeerr ? "Overseerr" : isMovie ? "Radarr" : "Sonarr";
  const ArrGlyph = viaSeerr ? Inbox : isMovie ? RadarrGlyph : SonarrGlyph;
  const base = (
    viaSeerr ? overseerrUrl : isMovie ? radarrUrl : sonarrUrl
  ).replace(/\/+$/, "");
  // Deep-link straight to the title's page. Overseerr needs no slug at all — its own UI addresses
  // both types by TMDB id — which is why a *seerr send records none. Radarr accepts its TMDB id;
  // Sonarr has NO id URL, only /series/<titleSlug>, so it needs the slug captured at send time.
  // Without a slug (a title sent before we recorded it) fall back to the app's home page rather
  // than a dead link.
  const arrPath = viaSeerr
    ? `${isMovie ? "movie" : "tv"}/${item.tmdb_id}`
    : isMovie
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
      <TitlePoster posterPath={item.poster_path} />
      <div className="min-w-0 flex-1 space-y-1.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-medium">{item.title}</p>
          {/* Wraps and shrinks, because the status badge beside these can open an explanation
              underneath itself, and a non-wrapping row leaves it nowhere to go but a narrow
              column (measured: 111px wide, 312px tall at 320px). */}
          <div className="flex min-w-0 flex-wrap items-center gap-2">
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
export function RejectedRow({
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
export type RequestView = "waiting" | "sent" | "rejected";

/** A missing title is exactly one media type, so the list can be split by the library it'd land in. */
export type MediaFilter = "all" | "movie" | "show";

/** How the on-screen list is ordered: newest activity, best rated, or most-wanted first. */
export type RequestSort = "recent" | "rating" | "demand";

export const SORT_OPTIONS: { value: RequestSort; label: string }[] = [
  { value: "recent", label: "Recent" },
  { value: "rating", label: "Top rated" },
  { value: "demand", label: "Most wanted" },
];

/** A rating floor to hide weaker titles. Every queued title already cleared the request min-rating
 *  gate, so the useful thresholds sit above it — these narrow a crowded inbox to the strongest. */
export const RATING_OPTIONS: { value: string; label: string }[] = [
  { value: "0", label: "Any" },
  { value: "7", label: "7+" },
  { value: "8", label: "8+" },
  { value: "9", label: "9+" },
];

/** A vote-count floor — a high rating on a handful of votes is noise; this keeps only well-attested
 *  titles. */
export const VOTES_OPTIONS: { value: string; label: string }[] = [
  { value: "0", label: "Any" },
  { value: "100", label: "100+" },
  { value: "500", label: "500+" },
  { value: "1000", label: "1k+" },
];

/** The Language filter's "no filter" value. Never a language code: TMDB's are two letters. */
export const ANY_LANGUAGE = "any";

/**
 * The Language filter's choices for one tab: every original language a title on it carries, by name,
 * then "Unknown" when some title has none recorded. Unknown is a choice of its own rather than a
 * title that silently matches nothing, so every title on the tab stays reachable through the menu.
 */
export function languageOptions(
  list: { language: string }[],
): { value: string; label: string }[] {
  const codes = new Set(list.map((r) => r.language));
  const named = [...codes]
    .filter((code) => code !== "")
    .map((code) => ({ value: code, label: languageName(code) }))
    .sort((a, b) => a.label.localeCompare(b.label));
  return codes.has("") ? [...named, { value: "", label: "Unknown" }] : named;
}

/**
 * One refinement control in the filter bar. These are deliberately NOT `Segmented`: sort + rating +
 * votes as chip groups put eleven buttons next to the Waiting/Sent tabs, four of them highlighted
 * (their own defaults), so the tab strip — the only control that changes what you're looking at —
 * was indistinguishable from a rating floor. A labelled dropdown is one quiet control per choice,
 * and a default reads as neutral.
 */
export function FilterSelect<T extends string>({
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

/** Waiting / Sent / Rejected. Tabs, not a row of buttons: they are separate lists, and the refinements
 *  under them are what narrow a list. The count is a pill beside the name; the accessible name keeps
 *  it in brackets ("Sent (23)") so it is read as one phrase. */
export function RequestTabs({
  value,
  tabs,
  onChange,
  idBase,
}: {
  value: RequestView;
  tabs: { value: RequestView; label: string; count: number }[];
  onChange: (next: RequestView) => void;
  /** Prefix for each tab's id (`<idBase>-<value>`) and the panel's (`<idBase>-panel`). */
  idBase: string;
}) {
  // The ARIA tabs pattern: one tab in the Tab order (the selected one), arrows move between them.
  const move = (from: number, step: number | "first" | "last") => {
    const next =
      step === "first" ? 0 : step === "last" ? tabs.length - 1 : (from + step + tabs.length) % tabs.length;
    const tab = tabs[next];
    if (!tab) return;
    onChange(tab.value);
    document.getElementById(`${idBase}-${tab.value}`)?.focus();
  };
  const KEY_STEP: Record<string, number | "first" | "last"> = {
    ArrowRight: 1,
    ArrowLeft: -1,
    Home: "first",
    End: "last",
  };
  return (
    <div role="tablist" aria-label="Which requests to show" className="flex gap-6 border-b">
      {tabs.map((tab, index) => {
        const selected = tab.value === value;
        return (
          <button
            key={tab.value}
            id={`${idBase}-${tab.value}`}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={`${idBase}-panel`}
            tabIndex={selected ? 0 : -1}
            aria-label={tab.count ? `${tab.label} (${tab.count})` : tab.label}
            onClick={() => onChange(tab.value)}
            onKeyDown={(e) => {
              const step = KEY_STEP[e.key];
              if (step === undefined) return;
              e.preventDefault();
              move(index, step);
            }}
            className={cn(
              "-mb-px inline-flex items-center gap-2 border-b-2 pb-2.5 pt-1 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              selected
                ? "border-primary font-medium text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {tab.label}
            {tab.count > 0 && (
              <span
                className={cn(
                  "rounded-full px-2 text-xs tabular-nums",
                  selected ? "bg-raised text-foreground" : "bg-muted text-muted-foreground",
                )}
              >
                {tab.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/** One active refinement, removable in place. */
export function FilterChip({
  label,
  removeLabel,
  onRemove,
}: {
  label: string;
  removeLabel: string;
  onRemove: () => void;
}) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-border-strong bg-raised py-0.5 pl-2.5 pr-1 text-xs font-medium text-foreground">
      {label}
      <button
        type="button"
        onClick={onRemove}
        aria-label={removeLabel}
        className="grid h-4 w-4 place-items-center rounded-full hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <X className="h-3 w-3" aria-hidden="true" />
      </button>
    </span>
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
export function peopleOn(
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
export function PeopleFilter({
  people,
  selected,
  onToggle,
  query,
  onQuery,
  offerPeople,
}: {
  people: PersonOption[];
  selected: Set<string>;
  onToggle: (name: string) => void;
  /** The typed text, owned by the page: it also narrows the list to titles, or wanters, matching it. */
  query: string;
  onQuery: (text: string) => void;
  /** False when one person wanted everything — picking them would hide nothing, so no list is offered. */
  offerPeople: boolean;
}) {
  const setQuery = onQuery;
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
    // One search for the two things people look for: a title by its name, or the titles someone
    // wanted. Typing narrows the list at once (see `applyQuery`); picking a name from the list makes it
    // a chip, which asks the SERVER for every title of theirs rather than only this loaded page.
    <div className="relative order-first w-full min-w-[12rem] sm:mr-auto sm:max-w-sm sm:flex-1">
      <span id={labelId} className="sr-only">
        Search titles, or pick who wanted them
      </span>
      <Search
        className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
        aria-hidden="true"
      />
      <div>
        <Input
          type="search"
          role="combobox"
          aria-expanded={open && offerPeople}
          aria-controls={listId}
          aria-labelledby={labelId}
          aria-autocomplete="list"
          aria-activedescendant={
            open && offerPeople && visible[active] ? `${listId}-${active}` : undefined
          }
          autoComplete="off"
          className="h-9 w-full pl-8 text-sm"
          placeholder="Search titles or people"
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
            } else if (e.key === "Enter" && open && offerPeople && visible[active]) {
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

        {open && offerPeople && (
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
 *  are really on the tab and which control brings them back. Only reachable when a rating, vote,
 *  language or people filter is set — the Movies/Shows split only ever renders when both types are present, so
 *  it can never empty a list on its own — which is what makes "Clear filters" a safe thing to name. */
export function NoMatches({ label, total }: { label: string; total: number }) {
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
export function sortRequests(
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
export function arrViewFor(
  item: RequestCandidate,
  status: ArrStatus | undefined,
  isPending: boolean,
): ArrView {
  const isMovie = item.media_type === "movie";
  if (isPending) return { kind: "checking" };
  if (!status) return { kind: "none" }; // the fetch failed outright; the page says so elsewhere
  // Overseerr answers for films and shows alike, so when it is the route there is no per-media-type
  // app to name — and the two Arr fields are always "off", which must not read as "reachable".
  // Tested against the two live values, never `!== "off"`: the field is absent from a response
  // predating it, and `undefined !== "off"` would route every Arr install down the Overseerr branch
  // and blank its badges.
  const viaSeerr =
    status.overseerr === "ok" || status.overseerr === "unreachable";
  const reach = viaSeerr
    ? status.overseerr
    : isMovie
      ? status.radarr
      : status.sonarr;
  if (reach === "unreachable") {
    const app = viaSeerr ? "Overseerr" : isMovie ? "Radarr" : "Sonarr";
    return { kind: "unreachable", app };
  }
  const found = status.statuses[String(item.id)];
  return found ? { kind: "status", status: found } : { kind: "none" };
}
