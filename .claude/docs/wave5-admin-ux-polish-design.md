# Wave 5 admin-UX polish — design

Covers four Wave 5 items from `.claude/docs/audit-2026-09-programme.md`: `healthstrip`,
`progressive`, `emptystates`, `accent`. (`posters`, `dryrun-preview`, `restriction-status`, and
`bulk3state` — the other four Wave 5 items — are not covered here.)

Method note up front, because it changes three of the four designs: every item's brief assumed a
gap that turned out to be smaller, or different, than stated once checked against the actual code.
None of the four premises were entirely true. Each section says so before designing anything.

---

## 1. `healthstrip` — independent status chips on the dashboard

### 1.1 Current state (verified)

**The brief's premise ("one blended OK") is false in two different ways.**

First, `GET /api/system/health` (`shortlist/server/api/system.py:88-92`) is not a blended health
signal at all — it is pure process liveness for Docker's `HEALTHCHECK`:

```python
@_public.get("/health", response_model=HealthOut)
async def health() -> dict:
    """Liveness only — ... The UI reads it from `/system/version`, which is owner-gated."""
    return {"status": "ok"}
```

It carries no subsystem information and the React app doesn't even call it (confirmed: no
reference to `/system/health` anywhere in `web/src`). It exists only for the container orchestrator.
Building a strip "from" this endpoint, as the brief's wording implies, isn't possible — there's
nothing in the payload to split into chips.

Second, the Dashboard is **not** the wireframe in `shortlist-design.md` §4 (that doc describes a
per-user card grid; the doc predates the current build). The live `DashboardPage`
(`web/src/pages/dashboard.tsx:10-21`) renders one component, `ImpactReport`
(`web/src/components/dashboard/impact-report.tsx`), and that report **already has** a small
multi-signal status line — not one blended OK, but not full subsystem coverage either. Inside the
`Verdict` card (`impact-report.tsx:279-312`):

```tsx
<div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-1 border-t pt-4 text-xs text-muted-foreground">
  <span className="flex items-center gap-1.5">
    <span
      className={cn(
        "h-1.5 w-1.5 rounded-full",
        runs.errors_last ? "bg-destructive" : "bg-success",
      )}
      aria-hidden="true"
    />
    {runs.last_finished
      ? `Last run ${timeAgo(runs.last_finished)}${runs.errors_last ? ", with errors" : ", no errors"}`
      : "No run yet"}
  </span>
  <span>
    Watch status {sync.last ? `synced ${timeAgo(sync.last)}` : "not synced yet"}
  </span>
  <span className="flex items-center gap-1.5">
    <span
      className={cn(
        "h-1.5 w-1.5 rounded-full",
        sync.live_down_since ? "bg-destructive" : "bg-success",
      )}
      aria-hidden="true"
    />
    {sync.live_down_since
      ? `Live tracking down ${timeAgo(sync.live_down_since)}`
      : sync.live_since
        ? "Live tracking on"
        : "Live tracking not started"}
  </span>
  ...
</div>
```

So two subsystems (run execution, live watch-tracking) already get a dot each, sourced from raw
ground truth in `EffectivenessReport` (`shortlist/server/services/report_service.py:978-984,
1101-1109`) — not from the dismissable notification feed. This line answers "can I trust the
numbers on this card," which is a narrower question than "which subsystem is unhealthy." It says
nothing about **Privacy** (the one `plex-safety.md` says has no post-write verification — see
§1.5), nor Jobs, Requests, or Rows-delivery.

**The real, richer signal already exists, just not on the Dashboard**: `shortlist/server/
notifications.py` — 13 builder functions, aggregated by `build_notifications()`
(`notifications.py:683-708`) and served at `GET /api/notifications`
(`shortlist/server/api/notifications.py:46-54`), already polled app-wide every 60s
(`web/src/lib/queries.ts:717-724`, `useNotifications`) and rendered as a flat, ungrouped list by
the bell (`web/src/components/layout/notification-bell.tsx`). Every builder recomputes current
state on every request — exactly the "check now, don't persist a verdict" pattern a health strip
needs — and each already carries `{id, severity: info|warning|error, title, body, action_url,
action_label, dismissable}`. Verified id / severity / destination for all 13:

| builder                            | id (prefix)                      | severity | action_url             |
| ---------------------------------- | -------------------------------- | -------- | ---------------------- |
| `_update_available`                | `update-<version>`               | info     | release URL (external) |
| `_runs_paused`                     | `runs-paused`                    | warning  | `/settings`            |
| `_last_run_problem` (whole-run)    | `run-failed-<id>`                | error    | `/runs/<id>`           |
| `_last_run_problem` (partial)      | `run-partial-<id>`               | warning  | `/runs/<id>`           |
| `_recent_service_errors`           | `recent-errors-<id>`             | warning  | `/runs`                |
| `_rows_with_no_name_for_newcomers` | `rows-unnamed-<slugs>`           | info     | `/rows`                |
| `_mdblist_quota`                   | `mdblist-quota-<date>`           | warning  | `/settings#requests`   |
| `_requests_found_nothing`          | `requests-none-qualified-<date>` | warning  | `/settings#requests`   |
| `_owner_sees_all_rows`             | `owner-sees-all-rows`            | info     | `/watching-account`    |
| `_failed_jobs`                     | `failed-jobs-<id>`               | error    | `/jobs`                |
| `_rows_we_cannot_hide`             | `unhideable-rows-<run id>`       | error    | `/users`               |
| `_shelf_contention`                | `shelf-contention-<date>`        | warning  | `/settings#placement`  |
| `_filters_not_enforced`            | `filters-not-enforced-<run id>`  | error    | `/runs/<id>`           |

This is not a hypothetical source — it's already correct, already tested (`server/tests` cover
each builder), and it is exactly what the brief tells me to prefer: **no new backend health check
is needed.** The gap is presentation only: the bell blends all 13 into one undifferentiated list
with a single count badge (`notification-bell.tsx:92-118`), so "which subsystem" still requires
opening the panel and reading titles.

### 1.2 The design

A pure grouping function over data the app already fetches, plus a small dashboard component. No
new endpoint, no new poll.

**`web/src/lib/health.ts`** (new file):

```ts
import type { AppNotification } from "./types";

export type HealthState = "ok" | "warning" | "error";

export interface HealthChip {
  category: string;
  label: string;
  state: HealthState;
  href: string;
  /** The worst live notification's title in this category. Undefined when healthy. */
  detail?: string;
}

interface CategoryDef {
  category: string;
  label: string;
  href: string;
}

// Keyed on the STABLE id prefix each notifications.py builder emits, never on `title` (copy can
// change without warning). This file and notifications.py are two hand-maintained lists of the
// same ids — nothing fails a build if they drift, which is why anything unmatched below still
// surfaces (see `deriveHealthChips`) instead of silently vanishing.
const CATEGORY_BY_ID_PREFIX: [prefix: string, def: CategoryDef][] = [
  ["run-failed-", { category: "runs", label: "Runs", href: "/runs" }],
  ["run-partial-", { category: "runs", label: "Runs", href: "/runs" }],
  ["runs-paused", { category: "runs", label: "Runs", href: "/settings" }],
  ["recent-errors-", { category: "runs", label: "Runs", href: "/runs" }],
  [
    "unhideable-rows-",
    { category: "privacy", label: "Privacy", href: "/users" },
  ],
  [
    "filters-not-enforced-",
    { category: "privacy", label: "Privacy", href: "/runs" },
  ],
  // `owner-sees-all-rows` is deliberately NOT here — see "excluded on purpose" below.
  ["rows-unnamed-", { category: "rows", label: "Rows", href: "/rows" }],
  [
    "shelf-contention-",
    { category: "rows", label: "Rows", href: "/settings#placement" },
  ],
  [
    "mdblist-quota-",
    { category: "requests", label: "Requests", href: "/settings#requests" },
  ],
  [
    "requests-none-qualified-",
    { category: "requests", label: "Requests", href: "/settings#requests" },
  ],
  ["failed-jobs-", { category: "jobs", label: "Jobs", href: "/jobs" }],
  [
    "playback-listener-down",
    { category: "watch", label: "Watch tracking", href: "/logs" },
  ],
];

// `update-<version>` is excluded on the same grounds: an available update isn't a subsystem
// unhealthy, it's an FYI, and the bell already carries it.
export const CORE_CATEGORIES: CategoryDef[] = [
  { category: "runs", label: "Runs", href: "/runs" },
  { category: "privacy", label: "Privacy", href: "/users" },
  { category: "rows", label: "Rows", href: "/rows" },
  { category: "requests", label: "Requests", href: "/settings#requests" },
  { category: "jobs", label: "Jobs", href: "/jobs" },
  { category: "watch", label: "Watch tracking", href: "/logs" },
];

function categoryFor(id: string): CategoryDef | undefined {
  return CATEGORY_BY_ID_PREFIX.find(([prefix]) => id.startsWith(prefix))?.[1];
}

const RANK: Record<"warning" | "error", number> = { error: 0, warning: 1 };

/**
 * Groups the SAME notifications the bell shows into one chip per subsystem, so the dashboard reads
 * as "which of these six things is unhealthy" instead of one flat, ordered list.
 *
 * Ignores `severity: "info"` on purpose. An available update, or "you see everyone's rows because
 * you own the server" (`owner-sees-all-rows` — true on almost every multi-user install, by design,
 * per its own docstring), are not unhealthiness; folding them in would make a chip amber for a
 * condition that isn't a problem.
 *
 * Any id this function doesn't recognise still surfaces, under "Other" — never silently dropped.
 * A 14th builder added to notifications.py later and never taught to this file fails LOUDLY here
 * (see health.test.ts's fallback-category test) instead of just not showing up.
 */
export function deriveHealthChips(
  notifications: AppNotification[],
): HealthChip[] {
  const worst = new Map<string, AppNotification>();
  const uncategorized: AppNotification[] = [];

  for (const n of notifications) {
    if (n.severity !== "warning" && n.severity !== "error") continue;
    const def = categoryFor(n.id);
    if (!def) {
      uncategorized.push(n);
      continue;
    }
    const current = worst.get(def.category);
    if (
      !current ||
      RANK[n.severity] < RANK[current.severity as "warning" | "error"]
    ) {
      worst.set(def.category, n);
    }
  }

  const chips = CORE_CATEGORIES.map(({ category, label, href }): HealthChip => {
    const hit = worst.get(category);
    return hit
      ? {
          category,
          label,
          state: hit.severity as HealthState,
          href: hit.action_url || href,
          detail: hit.title,
        }
      : { category, label, state: "ok", href };
  });

  if (uncategorized.length > 0) {
    const hit = [...uncategorized].sort(
      (a, b) =>
        RANK[a.severity as "warning" | "error"] -
        RANK[b.severity as "warning" | "error"],
    )[0];
    chips.push({
      category: "other",
      label: "Other",
      state: hit.severity as HealthState,
      href: hit.action_url,
      detail: hit.title,
    });
  }

  return chips;
}
```

**`web/src/components/dashboard/health-strip.tsx`** (new file):

```tsx
import { CircleAlert, CircleCheck, TriangleAlert } from "lucide-react";
import { Link } from "react-router";

import { QueryBoundary } from "@/components/query-boundary";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CORE_CATEGORIES,
  deriveHealthChips,
  type HealthState,
} from "@/lib/health";
import { useNotifications } from "@/lib/queries";
import { cn } from "@/lib/utils";

const TONE: Record<
  HealthState,
  { icon: typeof CircleCheck; className: string }
> = {
  ok: {
    icon: CircleCheck,
    className: "border-success/30 bg-success/10 text-success",
  },
  warning: {
    icon: TriangleAlert,
    className: "border-warning/30 bg-warning/10 text-warning",
  },
  error: {
    icon: CircleAlert,
    className: "border-destructive/30 bg-destructive/10 text-destructive-text",
  },
};

/** One chip per subsystem the notification registry already watches, so the dashboard reads as
 *  "which of these is unhealthy" at a glance rather than requiring the bell to be opened. */
export function HealthStrip() {
  const notifications = useNotifications();
  return (
    <QueryBoundary
      query={notifications}
      skeleton={
        <ul className="flex flex-wrap gap-2" aria-label="Subsystem health">
          {CORE_CATEGORIES.map(({ category }) => (
            <li key={category}>
              <Skeleton className="h-6 w-20 rounded-full" />
            </li>
          ))}
        </ul>
      }
    >
      {(data) => {
        const chips = deriveHealthChips(data.notifications);
        return (
          <ul className="flex flex-wrap gap-2" aria-label="Subsystem health">
            {chips.map((chip) => {
              const { icon: Icon, className } = TONE[chip.state];
              return (
                <li key={chip.category}>
                  <Link
                    to={chip.href}
                    title={chip.detail ?? `${chip.label}: healthy`}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors hover:brightness-110",
                      className,
                    )}
                  >
                    <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    {chip.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        );
      }}
    </QueryBoundary>
  );
}
```

Wired into `web/src/pages/dashboard.tsx`, between `PageHeader` and `ImpactReport`:

```tsx
<PageHeader ... />
<HealthStrip />
<ImpactReport />
```

Uses `QueryBoundary` (`web/src/components/query-boundary.tsx`) rather than hand-rolled
loading/error branches — the same component the rest of the app uses for the four-states rule, so
this new element is compliant with that rule by construction rather than by separate effort (it
does not need its own `four-states` audit line item). There is no distinct "empty" case: zero live
notifications is a normal success render (six green chips), not an empty state.

Six colour-blind-safe icon shapes (check / triangle+! / circle+!) carry the state, not colour alone
— color is reinforcement, not the only channel.

### 1.3 Tests written first

`web/src/test/health.test.ts` (pure function, no rendering):

- `returns all six categories as "ok" when notifications is empty`
- `marks the Runs chip "error" when a run-failed-* notification is present`
- `keeps the Runs chip "error" over "warning" when both a run-failed-* and a run-partial-* are live at once` (rank tie-break)
- `ignores severity "info" — an owner-sees-all-rows notification leaves the Privacy chip "ok"`
- `ignores severity "info" — an update-available notification produces no chip at all`
- `never drops an unrecognised id — it surfaces under "Other" with that notification's severity`
- `uses the worst live notification's action_url as the chip's href, and its title as the detail`
- `falls back to the category's default href when the chip is healthy`

`web/src/test/health-strip.test.tsx` (testing-library, mocking `useNotifications`):

- `renders one chip per core category from a mocked empty response`
- `renders the Privacy chip as an error-styled link to /users when unhideable-rows-* is present`
- `renders a loading skeleton with six placeholders while the query is pending`
- `renders QueryBoundary's error state with retry when the notifications query fails`

### 1.4 What could regress, and which test catches it

- A 14th `notifications.py` builder ships with a new id prefix nobody teaches to `health.ts` →
  caught by `health.test.ts`'s "never drops an unrecognised id" test, which asserts an "Other" chip
  appears rather than the notification silently vanishing from the strip.
- Someone "simplifies" `deriveHealthChips` to stop excluding `info` → caught by the two
  info-severity tests; regression would turn `owner-sees-all-rows` (true on almost every real
  install) into a permanently-amber Privacy chip, training the owner to ignore it.
- A future edit ports the strip to hand-rolled `isPending`/`isError` branches instead of
  `QueryBoundary` → no dedicated test for this by name, but `health-strip.test.tsx`'s skeleton and
  error-state tests would fail if the branches stop matching `QueryBoundary`'s contract.

### 1.5 API / `pnpm gen:api` — none needed

No backend change. `GET /api/notifications` already exists, already returns everything above, and
is already fetched by `useNotifications()`. This is a pure frontend addition.

### 1.6 Open questions

1. **Redundancy with the Verdict card's own status line.** Once this strip ships, `impact-report.
tsx`'s two dots (Runs, Watch tracking) report overlapping facts through a **different**
   mechanism: raw `EffectivenessReport` fields, not filtered by dismissal. A run-failure
   notification dismissed via the bell turns the new Runs chip green while the Verdict dot (which
   has no dismissal concept) stays red — the two can visibly disagree. Options: (a) leave both,
   accept that a dismissed chip and an un-dismissable raw-fact dot can differ (the same tradeoff
   the bell already makes for every dismissable alert), and say so in the chip's tooltip; (b) drop
   Verdict's two dots once the strip ships, since the strip now covers both facts plus four more.
   Not decided here — it's a call about removing existing UI, not a new-code decision.
2. **Chips for unconfigured subsystems.** An install with acquisition off will show "Requests: OK"
   forever, since the two Requests notifications only fire when the feature is in active use. Is an
   always-green chip for a feature nobody turned on useful, or clutter that should hide itself
   behind a `useSettings()` check? Left as "always show OK" for v1 for simplicity; flagging rather
   than deciding, since it trades one more query dependency against one less thing to reason about.
3. **`restriction-status` (separate Wave 5 item) will likely want the Privacy chip's `href`.** Once
   that page exists, `/users` should probably become `/restriction-status` (or whatever it's named)
   in `CORE_CATEGORIES` and in `_rows_we_cannot_hide`/`_filters_not_enforced`'s own `action_url` —
   sequencing note for whoever picks up that item, not something to guess at here.

### Files read for this design (read-only, no edits)

`shortlist/server/api/system.py` (1-95), `shortlist/server/notifications.py` (full file, 711
lines), `shortlist/server/api/notifications.py` (full file), `shortlist/server/services/
report_service.py` (960-1135), `web/src/pages/dashboard.tsx`, `web/src/components/dashboard/
impact-report.tsx` (1-320), `web/src/components/layout/notification-bell.tsx`, `web/src/lib/
queries.ts` (700-730), `web/src/lib/types.ts` (380-390), `web/src/components/query-boundary.tsx`,
`web/openapi.snapshot.json` (`NotificationOut`, `ReportRunsOut` schemas), `.claude/docs/
shortlist-design.md` §4.

---

## 2. `progressive` — disabled controls that say what's missing

### 2.1 Current state (verified)

**The brief's premise ("disabled switches currently give no reason") is mostly false.** I searched
every `<Switch` usage in `web/src` (20 files) for a `disabled`/`aria-disabled` prop. There are
exactly **two** switches in the entire app that are ever gated on a precondition, and the dominant
existing pattern for "this needs something to actually work" is a **third**, deliberately
different approach that never disables the control at all. All three are real, shipped code:

**Pattern A — accessible disable-with-reason (the good example), `web/src/components/rows/
placement-toggles.tsx:127-155`.** A placement grid cell can be structurally impossible for a
row type (e.g. a shared row can't split its single Home flag by audience). The existing `cell()`
closure:

```tsx
const cell = (checked, label, onToggle, unavailable?: string) => {
  const describedBy = unavailable ? `${slugify(label)}-why` : undefined;
  return (
    <div className="flex justify-center">
      <Switch
        aria-label={label}
        checked={checked}
        onCheckedChange={unavailable ? () => {} : onToggle}
        aria-disabled={unavailable ? true : undefined}
        title={unavailable}
        aria-describedby={describedBy}
        className={unavailable ? "cursor-not-allowed opacity-40" : undefined}
      />
      {unavailable && (
        <span id={describedBy} className="sr-only">
          {unavailable}
        </span>
      )}
    </div>
  );
};
```

The code comment explains why `aria-disabled` and not the native `disabled` attribute: "a native
`disabled` switch drops out of the tab order entirely, so its explanation ... would never be
reached by keyboard or screen reader." This is exactly the pattern the brief is asking for, already
shipped, already correct.

**Pattern B — the accessibility gap the brief is actually pointing at, `web/src/pages/
users.tsx:424-434`.** The row-enable switch for a managed user with a Plex parental Restriction
Profile:

```tsx
<Switch
  checked={user.enabled && !user.restriction_profile}
  disabled={Boolean(user.restriction_profile)}
  title={
    user.restriction_profile
      ? `Plex's ${profileName(user)} restriction profile is set on this account — Plex refuses the privacy filters Shortlist writes for it, so set the profile to None in Plex to enable`
      : undefined
  }
  onCheckedChange={(enabled) => toggleUser(user, enabled)}
  aria-label={`Shortlist row for ${user.username}`}
/>
```

This DOES say what's missing — but only to a mouse. Native `disabled` removes the element from the
tab order, so a keyboard user or screen reader never reaches the `title` at all; there's no
`aria-describedby`/`sr-only` fallback the way Pattern A has one. This is the one real, verified
instance of "a disabled control gives no _reachable_ reason" in the app.

**Pattern C — the app's actual dominant philosophy, and it's NOT disabling.** Every other
"needs something set up" toggle in the app (`web/src/components/settings/
ai-web-search-card.tsx:82-119`, `web/src/components/settings/recommendations-section.tsx:190-240`)
leaves the switch always interactive and shows an inline warning **after** it's turned on if the
precondition isn't met:

```tsx
// ai-web-search-card.tsx
<Switch
  checked={enabled}
  onCheckedChange={onToggle}
  aria-label="Enable AI web search"
/>;
{
  enabled && problem && (
    <p className="text-sm text-warning">
      {problem}{" "}
      <a href="#connections" className="font-medium underline">
        Set it up in Connections
      </a>
      .
    </p>
  );
}
```

```tsx
// recommendations-section.tsx — InlineFix even lets you fix it without leaving the page
if (sourceId === "trakt" && !hasTrakt(settings)) {
  return (
    <InlineKeyField
      settingKey="trakt.client_id"
      service="trakt"
      label="Trakt API key"
      hint="Paste your Trakt app client id to switch this source on — no trip to Connections."
      helpUrl="https://trakt.tv/oauth/applications"
      settings={settings}
    />
  );
}
```

This is a **deliberate, different, arguably better** design for _optional_ features: disabling the
switch would hide the option to prepare for it, while leaving it on with a warning lets someone
flip it in advance and fix the precondition in place. It is not a bug, and I am not proposing to
convert it to Pattern A.

**The real distinction the brief's framing collapses**: Patterns A and B gate something that is
categorically impossible right now, no matter what else you configure on this screen (a shared
row's placement math; a Plex-side setting Shortlist cannot touch). Pattern C gates something that
would simply run in a degraded/no-op way (a switch that's on with the wrong backend selected). The
app already treats these two situations differently in 3 of its 4 instances; only Pattern B — a
genuine "impossible right now" case — got the disabling right in principle but wrong in
accessibility.

### 2.2 The design

Extract Pattern A into a reusable primitive, and use it to fix Pattern B. Leave Pattern C alone.

**`web/src/components/ui/gated-switch.tsx`** (new file):

```tsx
import { useId } from "react";
import type { ComponentPropsWithoutRef } from "react";

import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

/**
 * A `Switch` that can be turned off for a REASON, and says it to a mouse, a keyboard user, and a
 * screen reader alike — not just to a hover tooltip.
 *
 * Uses `aria-disabled`, never the native `disabled` attribute: a natively disabled control drops
 * out of the tab order, so its `title` and `aria-describedby` are never reached by keyboard or
 * screen reader (the gap found in `users.tsx`'s restriction-profile switch — see progressive
 * item's design doc). `onCheckedChange` still needs a no-op guard for the same reason:
 * `aria-disabled` is advisory only and does not stop Radix from firing the real event.
 *
 * Only for a precondition that's categorically impossible right now (a Plex-side setting Shortlist
 * can't change, an incompatible combination). A feature that would simply run in a degraded way
 * once configured should stay switchable with an inline explanation instead — see
 * `AiWebSearchCard` / `RecommendationsSection`'s `InlineFix` for that pattern; don't route those
 * through this component.
 */
export function GatedSwitch({
  checked,
  onCheckedChange,
  reason,
  className,
  ...props
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  /** Why this can't be turned on right now. Renders as a normal, enabled switch when omitted. */
  reason?: string;
} & Omit<
  ComponentPropsWithoutRef<typeof Switch>,
  "checked" | "onCheckedChange"
>) {
  const reasonId = useId();
  return (
    <>
      <Switch
        checked={checked}
        onCheckedChange={reason ? () => {} : onCheckedChange}
        aria-disabled={reason ? true : undefined}
        aria-describedby={reason ? reasonId : undefined}
        title={reason}
        className={cn(reason && "cursor-not-allowed opacity-40", className)}
        {...props}
      />
      {reason && (
        <span id={reasonId} className="sr-only">
          {reason}
        </span>
      )}
    </>
  );
}
```

`web/src/pages/users.tsx:424-434` becomes:

```tsx
<GatedSwitch
  checked={user.enabled && !user.restriction_profile}
  reason={
    user.restriction_profile
      ? `Plex's ${profileName(user)} restriction profile is set on this account — Shortlist can't write the privacy filters this row needs while it's set. Clear the profile in Plex (Settings → Users & Sharing) to enable.`
      : undefined
  }
  onCheckedChange={(enabled) => toggleUser(user, enabled)}
  aria-label={`Shortlist row for ${user.username}`}
/>
```

The copy also picks up the more specific, already-established navigation path
("Settings → Users & Sharing") from `_rows_we_cannot_hide`'s notification body
(`shortlist/server/notifications.py:527-534`) — the same underlying Plex fact is described in two
places today, and they'd said it with different specificity. Aligning them is a one-line change,
not a rewrite of either voice.

`placement-toggles.tsx`'s own `cell()` is **not** touched by this change — refactoring it onto
`GatedSwitch` is a legitimate follow-up (it would remove the now-duplicated `slugify`/describedby
logic) but is not required to fix the one real gap, and folding it in here would touch a
known-working, well-tested file for cosmetic reasons alone.

### 2.3 Tests written first

`web/src/test/gated-switch.test.tsx` (new):

- `renders as a normal, clickable switch when reason is undefined`
- `renders aria-disabled (not the native disabled attribute) when reason is set` — the regression
  this whole component exists to prevent; asserts `switch.disabled` is falsy while
  `getAttribute("aria-disabled")` is `"true"`.
- `exposes the reason to assistive tech via aria-describedby pointing at a visible-to-AT-only span`
- `does not call onCheckedChange when clicked while a reason is set`
- `calls onCheckedChange normally when reason is undefined`

`web/src/test/users-page.test.tsx` (extend the existing `managed()` fixture block, §2.1's Pattern
B file — no such assertion exists there today, confirmed by grep):

- `a managed account with a restriction profile set exposes why its row switch is unavailable to
assistive tech` — render with `managed("adult")`, assert the switch has `aria-disabled="true"`
  and that `screen.getByText(/Plex's Adult restriction profile/)` exists (the sr-only span), not
  just that a `title` attribute is present (a `title`-only assertion is exactly the gap that let
  Pattern B ship inaccessible in the first place).

### 2.4 What could regress, and which test catches it

- A future edit reaches for the native `disabled` attribute on `GatedSwitch` "to be safe" → caught
  by `gated-switch.test.tsx`'s "renders aria-disabled (not the native disabled attribute)" test.
- The `users.tsx` port silently drops the `reason` text or narrows it back to a `title`-only
  attribute → caught by the new `users-page.test.tsx` case asserting the sr-only text is queryable,
  which a `title`-only implementation fails.
- Someone converts `AiWebSearchCard`'s or `RecommendationsSection`'s switch to `GatedSwitch` on the
  theory that "this is the disabled-switch component now" → no dedicated regression test for this
  (it's a design-intent boundary, not a type-level one), which is why §2.2 states explicitly which
  pattern each case belongs to; flagged here rather than silently risking it.

### 2.5 API / `pnpm gen:api` — none needed

Pure frontend; no backend or schema involvement.

### 2.6 Open questions

1. Should `placement-toggles.tsx` be refactored onto `GatedSwitch` for de-duplication? Not required
   for this fix (§2.2); worth doing opportunistically, not as its own task.
2. Is there a third "impossible right now" case elsewhere in the app that isn't a `<Switch>` at all
   (e.g. a disabled `<Button>` or a disabled row-editor field) that deserves the same treatment?
   Out of scope here — the brief and the enumeration above are switch-specific — but the same
   `aria-disabled` + `aria-describedby` shape would generalize to a button if one turns up.

### Files read for this design (read-only, no edits)

Every `<Switch` usage in `web/src` (20 files, listed via `grep -rl`), full read of
`web/src/components/rows/placement-toggles.tsx`, `web/src/pages/users.tsx` (390-440),
`web/src/components/settings/ai-web-search-card.tsx` (full file),
`web/src/components/settings/recommendations-section.tsx` (60-240),
`web/src/lib/user-profile.ts` (`profileName`), `shortlist/server/notifications.py` (479-535,
`_rows_we_cannot_hide`), `web/src/test/users-page.test.tsx` (300-360).

---

## 3. `emptystates` — copy and call-to-action for real empty screens

### 3.1 Scope boundary and current state (verified)

`.claude/docs/wave2-frontend-design.md` does not exist yet (checked before starting; the
`four-states` item — 16 views missing a loading/error/empty state altogether — hasn't been
designed). This item is **copy and call-to-action only**, for empty states that already exist. It
does not add an empty state to any view that lacks one; that's `four-states`'s job, and duplicating
it here would make the two designs disagree about which of the 16 views is whose.

**The brief's premise ("empty screens just say 'nothing here'") is false almost everywhere.** I
found and read all 21 `<EmptyState` usages in `web/src` (the shared component is
`web/src/components/query-boundary.tsx:33-58`, `{icon, title, hint, action}`). The large majority
already name the next action in prose, and several already carry a real `action` button:
`rows.tsx` ("Add a row" button), `user-row-card.tsx` ("Go to Rows" button, plus a note that shared
rows aren't listed here), `user-detail.tsx` / `run-detail.tsx` ("Back to Users" / "Back to all
runs"), `requests.tsx` ("Go to Settings → Requests"), `watching-account.tsx` (exact Plex menu
path: "Add one in Plex under Settings → Home"). These are already at, or above, the bar the brief
is asking for; I'm not touching them.

Two genuine gaps, both verified by reading the surrounding page, not assumed:

**Gap 1 — `web/src/App.tsx:150-157`, the 404 route.** No `action` at all, and the hint is actively
wrong on mobile:

```tsx
<EmptyState
  title="Page not found"
  hint="That address doesn't exist. Use the navigation on the left."
/>
```

The left nav is not always on the left: `web/src/components/layout/app-shell.tsx` renders it as a
slide-in drawer on narrow viewports (`"slide-in-left"` keyframe, `menuOpen` state, hamburger
trigger) — confirmed by reading `app-shell.tsx` in full. Someone who lands on a dead link on a
phone is told to look at navigation that is, at that moment, off-screen behind a menu button.

**Gap 2 — `web/src/pages/logs.tsx:183-190`, the filtered-empty case.** The hint recommends actions
that exist on the page (clear the search, or drop to a quieter level) but doesn't offer them as
controls in the empty state itself:

```tsx
<EmptyState
  title={search ? "Nothing matches that filter" : "No log lines yet"}
  hint={
    search
      ? `No ${level}-or-louder lines contain "${search}".${quieter ? ` Try ${quieter}, or clear the filter.` : " Try clearing the filter."}`
      : `Nothing has been logged at ${level} or louder yet.${quieter ? ` Try ${quieter}, or run something first.` : " Run something first."}`
  }
/>
```

`search`/`setSearch` and `level`/`setLevel`/`quieter` are all already in scope in this component
(`logs.tsx:86` for `quieter`) — the fix is wiring existing state setters into buttons, not adding
new state.

One case checked and confirmed **not** a gap: `web/src/pages/users.tsx:341-344`'s "No users yet"
hint says "Press 'Sync users' above" — I checked, and that button really is rendered just above
this region on the same page (`users.tsx:157`), so the instruction is accurate and the pattern
(reference a control already visible on the same screen, rather than duplicating it) matches
`runs.tsx`'s "No runs yet" empty state, which does the same for its own "Run now" button. Neither
needed fixing; recorded here so this isn't silently "missed."

### 3.2 The design

**Gap 1 fix — `web/src/App.tsx`:**

```tsx
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
// ...
<Route
  path="*"
  element={
    <EmptyState
      title="Page not found"
      hint="That page doesn't exist. It may have moved, or the link was wrong."
      action={
        <Button asChild variant="outline" size="sm">
          <Link to="/">Go to Dashboard</Link>
        </Button>
      }
    />
  }
/>;
```

Dropped "Use the navigation on the left" rather than fixing it to say "menu" — it's redundant once
there's a real button, and a route-specific fix for a copy line that's wrong specifically on
mobile is more brittle than not needing it to be right about layout at all.

**Gap 2 fix — `web/src/pages/logs.tsx`:**

```tsx
<EmptyState
  title={search ? "Nothing matches that filter" : "No log lines yet"}
  hint={
    search
      ? `No ${level}-or-louder lines contain "${search}".`
      : `Nothing has been logged at ${level} or louder yet.`
  }
  action={
    search || quieter ? (
      <div className="flex flex-wrap justify-center gap-2">
        {search && (
          <Button variant="outline" size="sm" onClick={() => setSearch("")}>
            Clear filter
          </Button>
        )}
        {quieter && (
          <Button variant="outline" size="sm" onClick={() => setLevel(quieter)}>
            Show {quieter} and louder
          </Button>
        )}
      </div>
    ) : undefined
  }
/>
```

The hint keeps only the fact (what didn't match); the two remedies move from prose into real
buttons, matching the shape `rows.tsx`/`requests.tsx` already use for their own `action` props. The
"run something first" line is dropped for the same reason as Gap 1's nav line — there's no button
to attach it to (a log page can't trigger a run), and it was advisory filler once the search/level
buttons exist beside it. On the truly-empty-with-no-filters case (`!search && !quieter`, i.e.
already at DEBUG with no search text), `action` is `undefined` and the empty state renders exactly
as it does today, just with the trailing sentence trimmed.

### 3.3 Tests written first

`web/src/test/app-not-found.test.tsx` (new, or added to an existing routing test if one covers
`App.tsx`):

- `renders a "Go to Dashboard" link on an unknown route`
- `the link navigates to the app root, not to /dashboard or any other path`

`web/src/test/logs.test.tsx` (extend existing file — confirm it exists and covers filtering
first):

- `shows a "Clear filter" button in the empty state when a search filter matches nothing, and
clicking it clears the search input`
- `shows a "Show <quieter> and louder" button when the current level has no matches and a quieter
level exists, and clicking it changes the level filter`
- `shows neither button, and no dangling "or clear the filter" sentence, when there are no log
lines at all and no filter is active` — pins that the trimmed copy doesn't leave an orphaned
  clause when `action` is `undefined`.

### 3.4 What could regress, and which test catches it

- A future edit re-adds "Try X, or clear the filter" prose alongside the new buttons (saying the
  same thing twice) → caught by the "shows neither button ... no dangling sentence" test asserting
  the exact hint text, which would fail if the sentence returns.
- The 404 fix ships with the link pointing at a stale path or wrapped in something that isn't
  focusable (a `<div onClick>` instead of a real `<Link>`) → caught by the not-found test's
  navigation assertion and by `frontend.md`'s existing "real `<button>`/`<label>`" convention
  being testable at all (a non-interactive element wouldn't have an accessible role to query).

### 3.5 API / `pnpm gen:api` — none needed

Both fixes are prose/JSX only; no new data is fetched.

### 3.6 Open questions

1. When `four-states` (separate item) adds empty states to the 16 currently-missing views, that
   design should follow the same "name the action, not just the absence" bar set here — I'd
   recommend cross-linking rather than re-deriving it, but I'm not asserting a bar for a design
   that doesn't exist yet.
2. Is `users.tsx`'s and `runs.tsx`'s "reference the button above" pattern (vs. duplicating the
   button inside the empty state, as `rows.tsx` does) something to standardize one way? Both read
   fine today; flagging the inconsistency rather than picking a side with no complaint driving it.

### Files read for this design (read-only, no edits)

All 21 `<EmptyState` call sites (14 files, via `grep -rl`), full read of
`web/src/components/query-boundary.tsx`, `web/src/App.tsx` (1-20, 130-161),
`web/src/components/layout/app-shell.tsx` (full file, for the mobile-drawer claim),
`web/src/pages/logs.tsx` (1-40, 150-192), `web/src/pages/users.tsx` (330-350),
`web/src/pages/runs.tsx` (395-412).

---

## 4. `accent` — where amber should carry data, not just chrome

### 4.1 Current state (verified)

Theme tokens, read from `web/tailwind.config.ts` and `web/src/index.css` (not assumed):
`--primary: 43 92% 55%` is the warm amber/gold "the accent is a warm amber/gold" per the file's own
comment (`index.css:6-8`) — used for buttons, links, focus rings, and (per `page-header.tsx:38`)
every page's header icon tile, unconditionally. `--accent` (`43 55% 15%`, a separate, darker,
muted token — confirmed 13 usages via `grep`) is used **only** for hover/active chrome: nav-link
hover, dropdown-item hover, segmented-control active state (`button.tsx:17,20`,
`app-shell.tsx:73,165`, `audience-picker.tsx:71,102`). `--warning: 38 92% 52%` is a third,
closely-related amber/gold token (5° of hue from `--primary`) already reserved for the
warning severity tier.

**The brief's premise holds for the clearest example, `header icon tiles`** — every `PageHeader`
across all 8 top-level pages renders the same `text-primary` icon regardless of that page's state
(`page-header.tsx:36-41`): 100% brand chrome, 0% information. It does **not** hold uniformly for
"borders" — `border-primary`/`bg-primary` already appears **conditionally**, keyed to UI _selection_
state, in several places (`settings/idle-hold-field.tsx:132`, `settings/refresh-days-field.tsx:91`,
`run-user-trace.tsx:715-718` — "is this preset/tab the one currently chosen"). That's real
conditionality, just not _domain_-data (how good/urgent/notable something is) — it answers "which
one is selected," not "is this one worth noticing." I'm treating that distinction as the actual gap:
plenty of amber already varies with UI state; none of it varies with the recommendation engine's or
the run's own data.

Two concrete, verified gaps where amber (or its `--warning` sibling) currently ignores real data it
could reflect, found by reading the components rather than guessing where they might be:

**Gap 1 — `web/src/components/pick-list.tsx:35-38`.** The rank number ahead of every pick is
amber, always, regardless of rank:

```tsx
<span className="w-5 shrink-0 font-semibold text-primary">#{pick.rank}</span>
```

`#1` and `#15` render identically. This is the PickList shared by the user-row card and the
run-detail breakdown — it's the one place the engine's actual ranking output reaches the UI, and
color currently says nothing about it.

**Gap 2 — `web/src/components/dashboard/impact-report.tsx:279-289`, the Verdict card's run-status
dot** (part of the same status line examined in §1.1):

```tsx
<span
  className={cn(
    "h-1.5 w-1.5 rounded-full",
    runs.errors_last ? "bg-destructive" : "bg-success",
  )}
  aria-hidden="true"
/>;
{
  runs.last_finished
    ? `Last run ${timeAgo(runs.last_finished)}${runs.errors_last ? ", with errors" : ", no errors"}`
    : "No run yet";
}
```

This is a hard binary — any `errors_last > 0` renders full destructive red, identical to a whole
run dying. But the severity taxonomy already established by `notifications.py` distinguishes these
two exactly: `_last_run_problem` (`shortlist/server/notifications.py:122-147`) returns
`severity: "error"` only when `last.status == "error"` (the whole run failed) and
`severity: "warning"` when it's `users_error` > 0 on an otherwise-`ok` run (some people failed,
most were fine). `EffectivenessReport.runs` already carries both facts needed to draw that same
distinction — `last_status` and `errors_last` (`shortlist/server/services/
report_service.py:978-985`, confirmed present in the OpenAPI schema, `ReportRunsOut`, as a
`required` field) — the Verdict card just never reads `last_status`. **This is not a new signal**,
it's an existing one sitting unread one line away, and today's binary collapses a distinction the
rest of the app already draws and relies on (the bell would show this same run as a `warning`
while the dashboard shows it as `destructive`, for the identical run).

Checked and deliberately **not** proposing: recoloring badges (`badges` is a separate Wave 6 item —
"recolour 8 default-coloured badges to the amber scheme" is about giving badges the brand's amber
_skin_, a cosmetic pass; this item is about color _encoding information_, a different axis, and
conflating the two would duplicate Wave 6's work under a different name). Also checked and
declining: giving the Verdict card's headline "landing rate" a data-driven amber/warning threshold
(e.g. "amber below 20%") — there is no validated baseline for what a good hit rate is yet; that's
exactly what Wave 0's `evaluation` harness (`.claude/docs/eval-harness-design.md`) exists to
establish, and inventing a threshold here would be an unfounded guess of the kind the audit
programme is already careful to avoid elsewhere (engine-scoring-design.md's "every numeric constant
... is reasoned, not validated" caveat applies just as much to a UI threshold as an engine one).

### 4.2 The design

**Gap 1 — `pick-list.tsx`: amber marks the single best pick, not every rank.**

```tsx
<span
  className={cn(
    "w-5 shrink-0 font-semibold",
    pick.rank === 1 ? "text-primary" : "text-muted-foreground",
  )}
>
  #{pick.rank}
</span>
```

Amber now means "the row's top pick," a fact about the engine's own output, everywhere `PickList`
renders (user-row card, run-detail breakdown) — for free, with no new prop, since `rank` is already
on every `Pick`.

**Gap 2 — `impact-report.tsx`: a three-tier dot using the same severity vocabulary the bell uses.**

```tsx
const RUN_DOT_CLASS: Record<"success" | "warning" | "destructive", string> = {
  success: "bg-success",
  warning: "bg-warning",
  destructive: "bg-destructive",
};

function runTone(
  runs: EffectivenessReport["runs"],
): "success" | "warning" | "destructive" {
  if (runs.last_status === "error") return "destructive";
  if (runs.errors_last > 0) return "warning";
  return "success";
}

function runStatusWord(runs: EffectivenessReport["runs"]): string {
  if (runs.last_status === "error") return ", failed";
  if (runs.errors_last > 0) {
    return `, ${runs.errors_last} ${runs.errors_last === 1 ? "person" : "people"} failed`;
  }
  return ", no errors";
}
```

```tsx
<span className="flex items-center gap-1.5">
  <span
    className={cn("h-1.5 w-1.5 rounded-full", RUN_DOT_CLASS[runTone(runs)])}
    aria-hidden="true"
  />
  {runs.last_finished
    ? `Last run ${timeAgo(runs.last_finished)}${runStatusWord(runs)}`
    : "No run yet"}
</span>
```

A run where every user succeeded is still green; a run whose `Run` row itself errored (PMS
unreachable, etc.) is still red; a run that finished `ok` but left some people un-rebuilt is now
amber (`--warning`) and says how many, instead of reading identically to a total failure. This
reuses `--warning`, an existing token, not `--primary` directly — both are amber-family (38° vs.
43° hue) and the choice is deliberate: `--warning` already carries this exact meaning everywhere
else in the app (the bell, `StatTile`'s `warning` tone), so this change is "use the established
warning color for a fact that already meets the established definition of a warning," not a new
color decision.

### 4.3 Tests written first

`web/src/test/pick-list.test.tsx` (extend if it exists, else new):

- `renders the #1 rank in the primary/amber color and every other rank in muted text`
- `renders #1 in amber even when picks are passed out of rank order` (guards the existing
  `sort((a,b) => a.rank - b.rank)` combined with the new conditional — the sort must still run
  before the rank check is meaningful)

`web/src/test/impact-report.test.tsx` (this file already has the test that pins today's binary
behavior and **must change**, confirmed by running it: `pytest`-equivalent here is
`./node_modules/.bin/vitest run src/test/impact-report.test.tsx`, 45/45 passing today against the
current binary dot):

- **Update**, don't just add to, `"says a run had errors when it did, and colours the dot for it"`
  (currently asserts `dot?.className` matches `/destructive/` for `errors_last: 2` against the
  fixture's `last_status: "ok"`). Under this design that exact input is the **warning** case, not
  destructive — the test's own fixture is the proof the current code conflates them. Split it into:
  - `"colours the dot amber and names the count when some people failed but the run itself succeeded"`
    — `{last_status: "ok", errors_last: 2}` → dot class matches `/warning/`, text matches
    `/2 people failed/`.
  - `"colours the dot destructive when the run itself failed, regardless of the per-user count"` —
    `{last_status: "error", errors_last: 0}` → dot class matches `/destructive/`, text matches
    `/failed/` (not "0 people failed").
  - Keep the existing clean-run case (`errors_last: 0, last_status: "ok"` → success/"no errors")
    unchanged — it isn't affected by this design.

### 4.4 What could regress, and which test catches it

- Someone reverts to `errors_last ? destructive : success` "for simplicity," re-collapsing the
  three tiers → caught directly by the new warning-tier test, which fails against the old binary
  logic (the whole point of writing it against today's code first).
- A whole-run failure with `errors_last` also non-zero (plausible: some users errored before the
  run itself died) renders as amber instead of red because a future edit checks `errors_last`
  before `last_status` → caught by the destructive-tier test's `errors_last: 0` fixture being
  swapped for a non-zero one in review, or by adding that as an explicit second destructive case if
  this ordering risk is taken seriously (recommended, not written above to avoid asserting a test
  I haven't executed).
- `pick-list.tsx`'s amber-for-rank-1 regresses to "amber for all" via a careless `cn()` edit →
  caught by the "every other rank in muted text" assertion, which a blanket `text-primary` fails.

### 4.5 API / `pnpm gen:api` — none needed

`runs.last_status` is already in the generated `EffectivenessReportOut` → `ReportRunsOut` schema
and already flows into `Verdict`'s `runs` prop (confirmed via `web/openapi.snapshot.json` and the
`Verdict` component's existing prop type, `EffectivenessReport["runs"]`) — it's simply unread today.
No backend change, no regeneration.

### 4.6 Light/dark — the brief's premise here is also false, verified

The task brief says "the app must work in both light and dark." **There is no light theme.**
Checked directly: `web/src/index.css:9-46` defines `:root, .dark { ... }` — the same values under
both selectors — and a repo-wide search for `prefers-color-scheme`, `data-theme`, `ThemeProvider`,
`useTheme`, or any theme-toggle UI returns nothing. `shortlist-design.md` §4 says "Dark theme
default" and nothing ships a light variant. This design doesn't need to (and doesn't) special-case
anything for light mode, since none exists to special-case for; it only needs to not hard-code a
hex value in place of a token, which it doesn't (`text-primary`, `text-muted-foreground`,
`bg-warning`, `bg-destructive`, `bg-success` are all pre-existing Tailwind tokens backed by CSS
custom properties). If a light theme is ever added, every color used above updates automatically
because none of it is a literal color — that's the actual guarantee "tokens only" is for, and it
holds today whether or not light mode exists yet.

### 4.7 Open questions

1. Should `PageHeader`'s icon tile (the clearest pure-chrome example, §4.1) become state-aware —
   e.g. tinted by that page's own `healthstrip` category from item 1? Deliberately **not**
   proposed: the same visual slot would then mean "brand" on some pages and "health" on others
   depending on which page you're on, which fights item 1's whole point (one clear place — the
   strip — for "is this subsystem OK," not one signal smeared across every page's icon). Recording
   the idea and the reason it's rejected, rather than silently not considering it.
2. Top-1-only in `pick-list.tsx` vs. top-3: I chose rank 1 specifically because the design doc's own
   language treats the headline reason as singular ("Because you watched Fargo" — one sentence, one
   pick). Top-3 is a defensible alternative with no data behind either choice; flagging rather than
   asserting rank 1 is provably correct.

### Files read for this design (read-only, no edits)

`web/tailwind.config.ts` (full file), `web/src/index.css` (full file), `web/src/components/
ui/badge.tsx`, `web/src/components/ui/switch.tsx`, `web/src/components/stat-tile.tsx`,
`web/src/components/page-header.tsx` (full file), `web/src/components/layout/app-shell.tsx`
(1-220), `web/src/components/pick-list.tsx` (full file), `web/src/components/dashboard/
impact-report.tsx` (full status-line section, 260-320), `web/src/lib/dashboard-stats.ts` (found to
be dead code — only referenced by its own test, not by any page; noted, not fixed, out of scope),
`shortlist/server/services/report_service.py` (960-1135), `shortlist/server/notifications.py`
(122-147), `web/openapi.snapshot.json` (`ReportRunsOut`), `web/src/test/impact-report.test.tsx`
(370-400, and a live `vitest run` confirming 45/45 pass today), `grep` across `web/src` for
`border-primary`, `bg-accent`/`text-accent`, `prefers-color-scheme`/`data-theme`/`ThemeProvider`.
